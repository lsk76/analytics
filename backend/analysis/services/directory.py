"""Довідник каналів і джерел: нормалізація адреси — єдиний ключ для всіх платформ.

Рішення (2026-09-22): один рядок довідника (Channel) на будь-яке місце, звідки
можуть прийти дані — Telegram, VK, сайт, RSS. Ключ — нормалізоване посилання,
платформа виводиться з нього. Джерело (Source) — один-до-одного з рядком
довідника й існує лише для того, що ми реально опитуємо.

Нормалізація:
  * схема https, хост у нижньому регістрі, без `www.`, без кінцевого слеша,
    без фрагмента; query лишається (RSS-стрічки бувають із параметрами);
  * Telegram: `@name`, `t.me/name`, `t.me/s/name`, `telegram.me/name`
    → `https://t.me/<name у нижньому регістрі>`; пост `t.me/name/123` → канал;
    інвайт `t.me/+hash` лишається як є (регістр важливий);
    чат без юзернейму → службова `https://t.me/c/<внутрішній id>` (як робить
    сам Telegram для закритих чатів);
  * VK: `vk.com/name`, `m.vk.com/name` → `https://vk.com/<name у нижньому регістрі>`.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

PLATFORM_TELEGRAM = "telegram"
PLATFORM_VK = "vk"
PLATFORM_WEB = "web"
PLATFORM_RSS = "rss"

_TG_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
_VK_HOSTS = {"vk.com", "m.vk.com", "vk.ru"}
_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def tg_internal_id(tg_id: int) -> int:
    """-1001234567890 → 1234567890; 1234567890 → 1234567890 (як у t.me/c/…)."""
    s = str(abs(int(tg_id)))
    return int(s[3:]) if s.startswith("100") and len(s) > 10 else int(s)


def telegram_url(username: str = "", tg_id: int | None = None) -> str:
    """Адреса довідника для Telegram-чату за юзернеймом, інакше за tg_id."""
    u = (username or "").strip().lstrip("@")
    if u.startswith("+"):
        return f"https://t.me/{u}"
    if _USERNAME.match(u) and not u.startswith("linked:"):
        return f"https://t.me/{u.lower()}"
    if tg_id:
        return f"https://t.me/c/{tg_internal_id(tg_id)}"
    return ""


def normalize_url(raw: str, kind_hint: str = "") -> tuple[str, str]:
    """→ (platform, url). Порожній/битий вхід → ("", "")."""
    s = (raw or "").strip()
    if not s:
        return "", ""
    if s.startswith("@"):
        return PLATFORM_TELEGRAM, telegram_url(s)
    if "://" not in s:
        s = "https://" + s
    p = urlsplit(s)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", p.path or "").rstrip("/")

    if host in _TG_HOSTS:
        parts = [x for x in path.split("/") if x]
        if parts and parts[0] == "s":
            parts = parts[1:]
        if not parts:
            return PLATFORM_TELEGRAM, ""
        if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
            return PLATFORM_TELEGRAM, f"https://t.me/c/{int(parts[1])}"
        if parts[0].startswith("+") or parts[0] == "joinchat":
            token = parts[1] if parts[0] == "joinchat" and len(parts) > 1 else parts[0].lstrip("+")
            return PLATFORM_TELEGRAM, f"https://t.me/+{token}"
        return PLATFORM_TELEGRAM, telegram_url(parts[0])

    if host in _VK_HOSTS:
        parts = [x for x in path.split("/") if x]
        return PLATFORM_VK, f"https://vk.com/{parts[0].lower()}" if parts else ""

    platform = PLATFORM_RSS if kind_hint == PLATFORM_RSS else PLATFORM_WEB
    url = urlunsplit(("https", host, path or "", p.query, ""))
    return platform, url.rstrip("/")


def channel_url(channel) -> tuple[str, str]:
    """(platform, url) для наявного рядка довідника Telegram."""
    return PLATFORM_TELEGRAM, telegram_url(channel.username, channel.tg_id)
