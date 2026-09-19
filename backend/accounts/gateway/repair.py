"""Ремонт акаунта: живість → регенерація session-id проксі → стан.

Замінює worker-proxy-health: перевірка іде РЕАЛЬНОЮ сесією акаунта тим самим
шляхом, що й робота, а не голим connect() без акаунта раз на 4 години.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.db.models import Q
from django.utils import timezone as djtz
from telethon import TelegramClient
from telethon.sessions import StringSession

from . import state as st
from .proxies import generate_new_session, to_telethon

logger = logging.getLogger("accounts.gateway.repair")

REGEN_ATTEMPTS = 3
PROACTIVE_AFTER = timedelta(hours=4)
NEEDS_PROXY_RETRY = timedelta(hours=1)
PROACTIVE_BATCH = 5


def due_for_repair() -> list[int]:
    """Кому час: (а) cooldown із ≥ repair_after транспортних збоїв; (б) needs_proxy
    раз на годину — раптом провайдер повернув гео; (в) профілактика (Setting
    gateway_repair_proactive=1): ready без успішної операції понад 4 год, по
    PROACTIVE_BATCH за тик."""
    from accounts.models import TelegramAccount as A
    from analysis.models import Setting
    try:
        after = int(Setting.get("gateway_repair_after", st.DEFAULT_REPAIR_AFTER))
    except (TypeError, ValueError):
        after = st.DEFAULT_REPAIR_AFTER
    now = djtz.now()
    base = A.objects.filter(is_active=True, is_authenticated=True)
    ids = list(base.filter(state=A.STATE_COOLDOWN, transport_failures__gte=after)
               .values_list("id", flat=True))
    ids += list(base.filter(state=A.STATE_NEEDS_PROXY,
                            updated_at__lt=now - NEEDS_PROXY_RETRY)
                .values_list("id", flat=True))
    # профілактика — лише за явним дозволом оператора: вона ЗАХОДИТЬ у Telegram
    # реальними сесіями, і на дев-стеку з копією прод-акаунтів це небезпечно
    if Setting.get("gateway_repair_proactive", "0") == "1":
        ids += list(base.filter(state=A.STATE_READY)
                    .filter(Q(last_ok_at__isnull=True) | Q(last_ok_at__lt=now - PROACTIVE_AFTER))
                    .order_by("last_ok_at")[:PROACTIVE_BATCH]
                    .values_list("id", flat=True))
    return list(dict.fromkeys(ids))


async def _probe(account, proxy_string: str, proxy_type: str, timeout: float = 30) -> bool:
    """Живість сесії акаунта через КОНКРЕТНИЙ проксі-рядок (не той, що в БД)."""
    client = TelegramClient(StringSession(account.session_string or ""),
                            int(account.api_id), account.api_hash,
                            proxy=to_telethon(proxy_string, proxy_type),
                            connection_retries=1, retry_delay=1, timeout=15,
                            **account.client_kwargs())
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        if not await client.is_user_authorized():
            return False
        await client.get_me()
        return True
    except Exception as e:  # noqa: BLE001
        logger.info("probe acc=#%s ...%s: %s", account.pk, proxy_string[-20:], type(e).__name__)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


@sync_to_async
def _load(account_id: int):
    from accounts.models import TelegramAccount
    return TelegramAccount.objects.select_related("proxy").get(pk=account_id)


@sync_to_async
def _save_proxy(proxy, proxy_string: str) -> None:
    proxy.proxy_string = proxy_string
    proxy.is_working = True
    proxy.fail_count = 0
    proxy.last_tested_at = djtz.now()
    proxy.save(update_fields=["proxy_string", "is_working", "fail_count", "last_tested_at"])


@sync_to_async
def _touch_proxy(proxy) -> None:
    proxy.last_tested_at = djtz.now()
    proxy.save(update_fields=["last_tested_at"])


async def repair(live) -> dict:
    """Під lock акаунта: перевірити → полагодити проксі → перевести стан.
    -> {"ok": bool, "action": "alive"|"regenerated"|"failed"|"skipped", ...}"""
    async with live.lock:
        account = await _load(live.id)
        A = type(account)
        if account.state in (A.STATE_DEAUTHORIZED, A.STATE_BANNED) or not account.is_authenticated:
            return {"ok": False, "action": "skipped", "reason": account.state}
        proxy = account.proxy
        if proxy is None or not proxy.is_active:
            await sync_to_async(st.mark_repair_failed)(account, "проксі не призначена")
            await live.drop()
            return {"ok": False, "action": "failed", "reason": "no_proxy"}

        # 1) як є
        if await _probe(account, proxy.proxy_string, proxy.proxy_type):
            await _touch_proxy(proxy)
            if not proxy.is_working:
                proxy.is_working, proxy.fail_count = True, 0
                await sync_to_async(proxy.save)(update_fields=["is_working", "fail_count"])
            await sync_to_async(st.mark_repaired)(account)
            logger.info("repair acc=#%s: живий через проксі #%s", account.pk, proxy.pk)
            return {"ok": True, "action": "alive"}

        # 2) свіжий session-id (лише формат marsproxies sticky)
        for attempt in range(1, REGEN_ATTEMPTS + 1):
            candidate = generate_new_session(proxy.proxy_string)
            if not candidate:
                break
            if await _probe(account, candidate, proxy.proxy_type):
                await _save_proxy(proxy, candidate)
                await live.drop()          # наступний виклик збере клієнт під новий рядок
                await sync_to_async(st.mark_repaired)(account)
                logger.info("repair acc=#%s: проксі #%s регенеровано (спроба %d/%d)",
                            account.pk, proxy.pk, attempt, REGEN_ATTEMPTS)
                return {"ok": True, "action": "regenerated", "attempt": attempt}

        # 3) не вийшло: проксі мертва, акаунт стоїть до заміни
        await sync_to_async(st.mark_repair_failed)(
            account, f"проксі #{proxy.pk} не зʼєднується після {REGEN_ATTEMPTS} регенерацій")
        await live.drop()
        logger.error("repair acc=#%s: проксі #%s мертва → needs_proxy", account.pk, proxy.pk)
        return {"ok": False, "action": "failed", "reason": "proxy_dead"}


async def replace_proxy(live, proxy_id: int) -> dict:
    """Оператор дає іншу проксі: перевірити нею і лише тоді записати."""
    from accounts.models import Proxy
    async with live.lock:
        account = await _load(live.id)
        proxy = await sync_to_async(Proxy.objects.get)(pk=proxy_id)
        if not await _probe(account, proxy.proxy_string, proxy.proxy_type):
            return {"ok": False, "error": f"акаунт не зʼєднується через проксі #{proxy_id}"}
        account.proxy = proxy
        await sync_to_async(account.save)(update_fields=["proxy"])
        await _save_proxy(proxy, proxy.proxy_string)
        await live.drop()
        await sync_to_async(st.mark_repaired)(account)
        return {"ok": True, "proxy_id": proxy_id}
