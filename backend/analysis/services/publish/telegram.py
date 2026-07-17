"""Тонкий клієнт Telegram Bot API для публікації подій у канал.

Лише sendMessage — воркеру більше нічого не треба. Синхронний (httpx, як
web-адаптер infospace), бо стадія publish шле по одному посту з throttle.
"""
from __future__ import annotations

import httpx

API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramError(RuntimeError):
    """Помилка Bot API (мережа або ok=false). retry_after — секунди для 429."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def send_message(token: str, chat_id: str, text: str,
                 parse_mode: str = "HTML", timeout: float = 30.0) -> int:
    """Опублікувати текст у канал. Повертає message_id.

    Кидає TelegramError на будь-яку відмову; для 429 виставляє retry_after
    (з parameters.retry_after), щоб воркер міг відкласти без інкременту спроб.
    """
    if not token:
        raise TelegramError("порожній bot token (env TELEGRAM_BOT_TOKEN або PublishConfig.bot_token)")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        resp = httpx.post(API.format(token=token), json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        raise TelegramError(f"мережна помилка: {e!r}") from e

    try:
        data = resp.json()
    except ValueError:
        raise TelegramError(f"HTTP {resp.status_code}, не-JSON тіло: {resp.text[:200]}")

    if not data.get("ok"):
        retry_after = (data.get("parameters") or {}).get("retry_after")
        raise TelegramError(
            f"Bot API помилка {data.get('error_code')}: {data.get('description')}",
            retry_after=retry_after)
    return data["result"]["message_id"]
