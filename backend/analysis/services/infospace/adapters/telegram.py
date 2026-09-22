"""Telegram-адаптер: полінг історії каналу через tg-gateway.

Акаунт: привʼязаний `source.tg_account`, інакше `registry.pick("collector",
key=source.id)` — стабільно за id джерела з ротацією через
`poll_cursor["acc_shift"]`. Watermark — last_msg_id (min_id у Telethon).
Помилки акаунта (пауза, проксі, резолв) — не збій джерела: ротуємо акаунт і
відсуваємо полінг (RateLimited); лише TelegramOpError (приватний/видалений
канал) рахується як збій джерела.
"""
from __future__ import annotations

import logging
import re

from analysis.services import peers

from ..utils import canonical_url
from . import register
from .base import BaseSourceAdapter, RateLimited, RawItem

logger = logging.getLogger(__name__)


def _channel(handle: str):
    from analysis.models import Channel
    return Channel.objects.filter(username__iexact=handle).only("id", "raw_meta", "tg_id").first()


def _channel_or_create(handle: str, source, peer: dict):
    """Рядок Channel для кешу хеша. Без рядка кеш не працює і джерело платить
    резолв на кожен рестарт gateway (на проді таких джерел ~100). Створюємо
    лише після УСПІШНОГО читання з хешем — не плодимо рядки для мертвих
    юзернеймів."""
    ch = _channel(handle)
    if ch is None and peer and peer.get("access_hash"):
        # джерело вже має рядок довідника (1:1) — це і є канал
        ch = source.channel
    return ch


def _bump_shift(source) -> None:
    """Наступний прохід візьме інший акаунт із пулу. Пишемо одразу в БД: стадія
    при RateLimited poll_cursor не зберігає."""
    from analysis.models import Source
    cur = dict(source.poll_cursor or {})
    cur["acc_shift"] = int(cur.get("acc_shift", 0)) + 1
    source.poll_cursor = cur
    Source.objects.filter(pk=source.pk).update(poll_cursor=cur)


@register
class TelegramAdapter(BaseSourceAdapter):
    kind = "telegram"

    def _account(self, source, channel=None):
        """Привʼязаний акаунт джерела або стабільний вибір із пулу збирачів.

        Акаунт, який ще не резолвив цей канал (нема кешованого хеша), має
        вміти резолвити зараз: вичерпаний ліміт резолву — це ще один прохід
        у нікуди. Тому без хеша беремо лише кандидатів із живим резолвом."""
        from accounts.services import registry
        acc = registry.pinned_for(source)
        if acc is not None:
            return acc
        shift = int((source.poll_cursor or {}).get("acc_shift", 0))
        key = source.id or 0
        try:
            acc = registry.pick("collector", key=key, shift=shift)
            if peers.peer_for(channel, acc.id) is None and not acc.can_resolve():
                acc = registry.pick("collector", key=key, shift=shift, need_resolve=True)
            return acc
        except registry.NoAccountAvailable:
            return None

    @staticmethod
    def _handle(source) -> str:
        u = (source.url or "").strip()
        m = re.match(r"https?://t\.me/(?:s/)?([A-Za-z0-9_]+)", u)
        return m.group(1) if m else u.lstrip("@")

    def fetch(self, source) -> list[RawItem]:
        from accounts.services.managed import AccountUnavailable, TelegramOpError
        from accounts.services.managed import RateLimited as GwRateLimited
        handle = self._handle(source)
        channel = _channel(handle)
        acc = self._account(source, channel)
        if acc is None:
            raise RateLimited(120)     # пул порожній: не збій джерела, зачекати
        poll_cursor = dict(source.poll_cursor or {})
        first_poll = "last_msg_id" not in poll_cursor
        min_id = int(poll_cursor.get("last_msg_id", 0))
        limit = self.backfill_limit(source) if first_poll else self.max_items(source)
        # перший полінг — найновіші N (backfill); далі — найстаріші від watermark
        # (reverse=True) суцільно, щоб бурст >limit не лишив діру
        reverse = not first_poll

        # хеш, який ЦЕЙ акаунт уже здобув, → читаємо без ResolveUsernameRequest
        # (добовий ліміт резолву — головна причина «No user has X as username»)
        target = peers.peer_for(channel, acc.id) or handle
        peer: dict = {}
        try:
            msgs = acc.fetch_history(target, min_id=min_id, limit=limit, reverse=reverse,
                                     peer_sink=peer)
        except AccountUnavailable as e:
            # проксі/пауза/резолв цього акаунта — джерело ні до чого: інший акаунт
            logger.info("info_collect: %s — акаунт #%s недоступний (%s), ротація",
                        source.name, acc.id, e.reason)
            if not source.tg_account_id:
                _bump_shift(source)
            raise RateLimited(e.retry_after or 60)
        except GwRateLimited as e:
            raise RateLimited(e.retry_after)
        except TelegramOpError as e:
            if target is not handle and peers.is_stale_peer_error(str(e)):
                # кешований хеш протух (канал мігрував/перестворений): забути й
                # перечитати за юзернеймом наступним проходом — це не збій джерела
                peers.forget_peer(channel, acc.id)
                logger.info("info_collect: %s — хеш під акаунт #%s протух, скинуто", source.name, acc.id)
                raise RateLimited(60)
            raise
        peers.remember_peer(channel or _channel_or_create(handle, source, peer), acc.id, peer)

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
