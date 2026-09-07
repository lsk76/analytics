"""proxy_healthcheck — taskless-стадія для run_worker.py: періодична перевірка
й авторемонт пулу Proxy.

Marsproxies sticky-сесії (`user:secret_country-xx_session-<id>_lifetime-168h`)
не гарантують IP на весь заявлений термін («cannot assure... will try to keep
it for as long as possible» — з їхньої документації), тож проксі можуть
відвалюватись у будь-який момент. `session-<id>` — довільний рядок, який
клієнт сам вигадує (не потребує реєстрації через API), тож на невдалий
конект пробуємо просто згенерувати новий `session-id` під тим самим
акаунтом/країною — це і є автономний ремонт без ручних списків від оператора.
Спрацьовує лише для ще не деактивованої (провайдером) країни/міста — якщо
цілий гео-пул знятий з обслуговування, авторемонт це не полагодить.
"""
import asyncio
import random
import re
import string
from datetime import timedelta

import socks
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone as djtz
from telethon import TelegramClient
from telethon.sessions import StringSession

from ..models import Proxy
from .telegram_client import run_async

HEALTHCHECK_INTERVAL = timedelta(hours=4)  # не частіше цього — не спамити провайдера
REGEN_ATTEMPTS = 3

_MARS_RE = re.compile(
    r"^(?P<host>[^:]+):(?P<port>\d+):(?P<user>[^:]+):(?P<secret>.+?)"
    r"_country-(?P<country>[a-z]+)(?P<city>_city-[a-z0-9]+)?"
    r"_session-(?P<session>[a-z0-9]+)_lifetime-(?P<hours>\d+)h$"
)


def _random_session_id(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def generate_new_session(proxy_string: str) -> str | None:
    """Новий рядок з тим самим host/port/акаунтом/країною, але свіжим session-id.

    None — якщо proxy_string не в форматі marsproxies sticky-session
    (наприклад плоский host:port:user:pass без country/session/lifetime).
    """
    m = _MARS_RE.match(proxy_string)
    if not m:
        return None
    g = m.groupdict()
    hours = min(int(g["hours"]), 168)
    return (f"{g['host']}:{g['port']}:{g['user']}:{g['secret']}"
           f"_country-{g['country']}{g['city'] or ''}"
           f"_session-{_random_session_id()}_lifetime-{hours}h")


def _parse_socks5(proxy_string: str):
    host, port, username, password = proxy_string.split(":", 3)
    return (socks.SOCKS5, host, int(port), True, username or None, password or None)


async def _test(proxy_tuple, timeout):
    client = TelegramClient(StringSession(""), int(settings.TELEGRAM_API_ID),
                            settings.TELEGRAM_API_HASH, proxy=proxy_tuple,
                            connection_retries=0)
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        return client.is_connected()
    except Exception:
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def test_proxy_connectivity(proxy_string: str, timeout: float = 12) -> bool:
    """Живий конект (без TelegramAccount/сесії) — лише мережа+проксі, для здоров'я пулу."""
    try:
        tup = _parse_socks5(proxy_string)
    except Exception:
        return False
    try:
        return bool(run_async(_test(tup, timeout)))
    except Exception:
        return False


def _claim_proxy():
    now = djtz.now()
    cutoff = now - HEALTHCHECK_INTERVAL
    with transaction.atomic():
        p = (Proxy.objects
             .filter(is_active=True)
             .filter(Q(last_tested_at__isnull=True) | Q(last_tested_at__lt=cutoff))
             .select_for_update(skip_locked=True)
             .order_by(F("last_tested_at").asc(nulls_first=True))
             .first())
        if p:
            p.last_tested_at = now
            p.save(update_fields=["last_tested_at"])
    return p


def proxy_healthcheck_once() -> bool:
    """Одна проксі за виклик (гейт — не частіше HEALTHCHECK_INTERVAL на проксі)."""
    p = _claim_proxy()
    if not p:
        return False

    if test_proxy_connectivity(p.proxy_string):
        p.is_working = True
        p.fail_count = 0
        p.save(update_fields=["is_working", "fail_count"])
        return True

    p.fail_count += 1
    p.is_working = False
    fixed = False
    for _ in range(REGEN_ATTEMPTS):
        candidate = generate_new_session(p.proxy_string)
        if not candidate:
            break
        if test_proxy_connectivity(candidate):
            p.proxy_string = candidate
            p.is_working = True
            p.fail_count = 0
            fixed = True
            break
    p.save(update_fields=(["proxy_string", "is_working", "fail_count"] if fixed
                          else ["is_working", "fail_count"]))
    return True
