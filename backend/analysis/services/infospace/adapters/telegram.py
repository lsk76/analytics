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


def _remember_peer(handle: str, peer: dict) -> None:
    """Зберегти (id, access_hash) каналу в Channel.raw_meta — одноразово.

    Публікація бере peer звідси й шле медіа без резолву юзернейма. Пишемо лише
    коли значення змінилось, щоб не смикати БД на кожному полінгу.
    """
    if not peer or not handle:
        return
    from analysis.models import Channel
    ch = Channel.objects.filter(username__iexact=handle).only("id", "raw_meta").first()
    if ch is None:
        return
    meta = ch.raw_meta or {}
    if meta.get("access_hash") == peer["access_hash"]:
        return
    meta["access_hash"] = peer["access_hash"]
    ch.raw_meta = meta
    fields = ["raw_meta"]
    if not ch.tg_id:
        ch.tg_id = peer["id"]
        fields.append("tg_id")
    ch.save(update_fields=fields)


@register
class TelegramAdapter(BaseSourceAdapter):
    kind = "telegram"

    def _account(self, source):
        # Явно призначений акаунт джерела, інакше — перший авторизований із пулу.
        # (Справжня ротація по акаунтах/шардинг джерел — Phase 4; поки 1 акаунт.)
        acc = source.tg_account
        if acc and acc.is_authenticated:
            return acc
        from accounts.models import TelegramAccount
        return (TelegramAccount.objects.filter(is_authenticated=True)
                .order_by("id").first())

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
        msgs = _fetch_history(acc, handle, min_id, limit, reverse, peer)  # FloodWait → RateLimited
        _remember_peer(handle, peer)

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
