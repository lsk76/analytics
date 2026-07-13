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


def _fetch_history(account, handle, min_id, limit, reverse):
    """Обгортка навколо Telethon (винесена для DI у тестах). FloodWait → RateLimited."""
    from accounts.services.telegram_client import TelegramUserClient, run_async
    try:
        from telethon.errors import FloodWaitError
    except Exception:  # noqa: BLE001 — telethon має бути, але не валимо імпорт пакета
        FloodWaitError = ()
    try:
        return run_async(TelegramUserClient.fetch_history(
            account, handle, min_id=min_id, limit=limit, reverse=reverse))
    except FloodWaitError as e:  # type: ignore[misc]
        raise RateLimited(getattr(e, "seconds", 60))


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

        msgs = _fetch_history(acc, handle, min_id, limit, reverse)  # FloodWait → RateLimited

        items, max_id = [], min_id
        for m in msgs:
            mid = int(m["id"])
            max_id = max(max_id, mid)
            items.append(RawItem(
                external_id=str(mid),
                url=canonical_url(f"https://t.me/{handle}/{mid}"),
                title="", text=m["text"], posted_at=m.get("date"), meta={}))
        poll_cursor["last_msg_id"] = max_id
        source.poll_cursor = poll_cursor
        return items
