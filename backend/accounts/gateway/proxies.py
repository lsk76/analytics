"""Проксі-рядки Marsproxies sticky-session: розбір і регенерація session-id.

Sticky-сесія не гарантує IP на весь термін; `session-<id>` — довільний рядок,
який клієнт вигадує сам, тож «ремонт» мертвої проксі = новий id під тим самим
акаунтом/країною. Не спрацює, якщо провайдер зняв цілий гео-пул.
"""
from __future__ import annotations

import random
import re
import string

import socks

_MARS_RE = re.compile(
    r"^(?P<host>[^:]+):(?P<port>\d+):(?P<user>[^:]+):(?P<secret>.+?)"
    r"_country-(?P<country>[a-z]+)(?P<city>_city-[a-z0-9]+)?"
    r"_session-(?P<session>[a-z0-9]+)_lifetime-(?P<hours>\d+)h$"
)


def _random_session_id(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def generate_new_session(proxy_string: str) -> str | None:
    """Той самий host/port/акаунт/країна, свіжий session-id. None — не той формат."""
    m = _MARS_RE.match(proxy_string)
    if not m:
        return None
    g = m.groupdict()
    hours = min(int(g["hours"]), 168)
    return (f"{g['host']}:{g['port']}:{g['user']}:{g['secret']}"
            f"_country-{g['country']}{g['city'] or ''}"
            f"_session-{_random_session_id()}_lifetime-{hours}h")


def to_telethon(proxy_string: str, proxy_type: str = "socks5") -> tuple:
    """(type, host, port, rdns, user, pass) для TelegramClient(proxy=...)."""
    parts = proxy_string.split(":", 3)
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else 0
    user = parts[2] if len(parts) > 2 else None
    pwd = parts[3] if len(parts) > 3 else None
    ptype = socks.SOCKS5 if proxy_type == "socks5" else socks.HTTP
    return (ptype, host, port, True, user or None, pwd or None)
