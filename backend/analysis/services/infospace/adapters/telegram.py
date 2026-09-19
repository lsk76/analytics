"""Telegram-адаптер: полінг історії каналу акаунтом (Telethon).

Публічні канали читаються БЕЗ вступу (менший бот-слід). Акаунт: source.tg_account
або round-robin по авторизованих TelegramAccount. Watermark — last_msg_id (min_id
у Telethon). FloodWait → RateLimited (стадія відсуває полінг без збою).
"""
from __future__ import annotations

import logging
import re
from ..utils import canonical_url
from . import register
from .base import BaseSourceAdapter, RateLimited, RawItem

logger = logging.getLogger(__name__)



def _fetch_history(account, handle, min_id, limit, reverse, peer_sink=None):
    """Обгортка навколо Telethon (винесена для DI у тестах). FloodWait → RateLimited."""
    from accounts.services.telegram_client import TelegramUserClient, run_async
    try:
        from telethon.errors import FloodWaitError
    except Exception:  # noqa: BLE001 — telethon має бути, але не валимо імпорт пакета
        FloodWaitError = ()
    # account.proxy (FK) треба прогріти ТУТ, у sync-коді: _client читає його вже
    # всередині корутини, і лінивий SELECT валить SynchronousOnlyOperation
    TelegramUserClient._prime_proxy(account)
    try:
        return run_async(TelegramUserClient.fetch_history(
            account, handle, min_id=min_id, limit=limit, reverse=reverse,
            peer_sink=peer_sink))
    except FloodWaitError as e:  # type: ignore[misc]
        raise RateLimited(getattr(e, "seconds", 60))


def _remember_peer(handle: str, peer: dict, account_id: int) -> None:
    """Зберегти access_hash каналу ДЛЯ ЦЬОГО АКАУНТА.

    access_hash у Telegram видається під конкретного користувача: хеш, здобутий
    одним акаунтом, для іншого недійсний і дає ChannelInvalidError. Раніше ми
    клали його одним полем на канал — і чат, перевʼязаний на інший акаунт,
    ставав «Invalid channel object» (21 чат стріму саме так і замовк). Тому
    мапа {id акаунта: хеш}, а читач бере ЛИШЕ свій.
    """
    if not peer or not handle or not account_id:
        return
    from django.db import IntegrityError, transaction

    from analysis.models import Channel
    ch = Channel.objects.filter(username__iexact=handle).only(
        "id", "raw_meta", "tg_id").first()
    if ch is None:
        return
    meta = ch.raw_meta or {}
    by_acc = dict(meta.get("access_hash_by_acc") or {})
    key = str(account_id)
    if by_acc.get(key) == peer["access_hash"]:
        return
    by_acc[key] = peer["access_hash"]
    meta["access_hash_by_acc"] = by_acc
    ch.raw_meta = meta
    fields = ["raw_meta"]
    # tg_id пишемо ЛИШЕ якщо він вільний: у довіднику той самий канал буває
    # двічі (під різними юзернеймами), і запис ламав uniq_channel_tgid — а
    # виняток летів з-під збору й гасив УВЕСЬ полінг цього джерела на цикл.
    if not ch.tg_id and not (Channel.objects.filter(tg_id=peer["id"])
                             .exclude(pk=ch.pk).exists()):
        ch.tg_id = peer["id"]
        fields.append("tg_id")
    try:
        # savepoint: без нього перехоплений IntegrityError лишає транзакцію
        # збору «отруєною» і падає вже наступний запит
        with transaction.atomic():
            ch.save(update_fields=fields)
    except IntegrityError:
        logger.debug("_remember_peer: %s — конфлікт унікальності, пропускаю", handle)


@register
class TelegramAdapter(BaseSourceAdapter):
    kind = "telegram"

    def _account(self, source):
        """Акаунт джерела, інакше — СТАБІЛЬНИЙ вибір із пулу збирачів.

        Два правила, обидва зі шкоди на проді:
        1) не беремо акаунти, привʼязані до чатів tgsearch-стріму. Той самий
           auth key, задіяний двома конвеєрами водночас, Telegram бачить як
           «used under two different IP addresses» і може вбити сесію — три
           сесії ми так уже втратили;
        2) не «перший за id»: раніше ВСІ непривʼязані джерела довбали акаунт
           #1, і його добовий ліміт резолву юзернеймів вичерпувався за годину
           (33 джерела висіли на «No user has X as username»). Вибір за
           лишком від id джерела — стабільний, тож джерело тримається свого
           акаунта й користається його прогрітою сесією.
        """
        acc = source.tg_account
        if acc and acc.is_authenticated:
            return acc
        from analysis.models import MonitorChat, PublishConfig
        from accounts.models import TelegramAccount
        busy = set(MonitorChat.objects.exclude(tg_account=None)
                   .values_list("tg_account_id", flat=True))
        # акаунт, яким ПУБЛІКУЄМО, збирачу не давати: паралельний конект убиває
        # ключ, і канал онімів би (#160 мав 3 джерела й лежав у пулі)
        busy |= set(PublishConfig.objects.exclude(forward_account=None)
                    .values_list("forward_account_id", flat=True))
        pool = list(TelegramAccount.objects.filter(is_authenticated=True)
                    .exclude(id__in=busy).order_by("id"))
        if not pool:      # усі зайняті — краще працювати, ніж стояти
            pool = list(TelegramAccount.objects.filter(is_authenticated=True)
                        .order_by("id"))
        if not pool:
            return None
        # зсув дає РОТАЦІЮ: без нього лишок від id завжди повертав той самий
        # акаунт, і джерело з вичерпаним лімітом резолву не мало шансу
        # перескочити на інший (33 джерела так і стояли)
        shift = int((source.poll_cursor or {}).get("acc_shift", 0))
        return pool[((source.id or 0) + shift) % len(pool)]

    @staticmethod
    def _handle(source) -> str:
        u = (source.url or "").strip()
        m = re.match(r"https?://t\.me/(?:s/)?([A-Za-z0-9_]+)", u)
        return m.group(1) if m else u.lstrip("@")

    def fetch(self, source) -> list[RawItem]:
        acc = self._account(source)
        if acc is None:
            raise RuntimeError("немає авторизованого TelegramAccount для полінгу")
        handle = self._handle(source)
        poll_cursor = dict(source.poll_cursor or {})
        first_poll = "last_msg_id" not in poll_cursor
        min_id = int(poll_cursor.get("last_msg_id", 0))
        limit = self.backfill_limit(source) if first_poll else self.max_items(source)
        # перший полінг — найновіші N (backfill); далі — найстаріші від watermark
        # (reverse=True) суцільно, щоб бурст >limit не лишив діру (див. рев'ю)
        reverse = not first_poll

        peer: dict = {}
        from accounts.services.telegram_client import AccountBusy, account_exclusive
        try:
            # один акаунт = один клієнт у всій системі; зайнятий іншим процесом
            # → чекаємо наступного проходу, а не відкриваємо другий конекшн
            with account_exclusive(acc):
                msgs = _fetch_history(acc, handle, min_id, limit, reverse, peer)
        except AccountBusy:
            raise RateLimited(30)
        _remember_peer(handle, peer, getattr(acc, "id", None))

        items, max_id = [], min_id
        for m in msgs:
            mid = int(m["id"])
            max_id = max(max_id, mid)
            meta = {}
            if m.get("media_kind"):
                meta["media"] = {"kind": m["media_kind"], "chat": handle, "mid": mid}
            items.append(RawItem(
                external_id=str(mid),
                url=canonical_url(f"https://t.me/{handle}/{mid}"),
                title="", text=m["text"], posted_at=m.get("date"), meta=meta))
        poll_cursor["last_msg_id"] = max_id
        source.poll_cursor = poll_cursor
        return items
