"""Стейт-машина Telegram-акаунта (docs/tg-gateway-plan.md §4).

`classify(exc)` — з якого винятку який результат; `apply(account, outcome)` —
перехід рядка `TelegramAccount` (+ побічний ефект на проксі). Це ЄДИНЕ місце,
де вирішується, що означає помилка Telegram для акаунта: раніше кожен
споживач (збирач, стрім, публікація) робив це по-своєму і по-різному помилявся.

Синхронний код: gateway кличе через sync_to_async, тести — напряму.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from enum import Enum

from django.db.models import F
from django.utils import timezone as djtz
from telethon.errors import (AuthKeyDuplicatedError, AuthKeyUnregisteredError,
                             FloodWaitError, PhoneNumberBannedError, RPCError,
                             SessionRevokedError, UserDeactivatedBanError,
                             UserDeactivatedError, UsernameInvalidError,
                             UsernameNotOccupiedError)

logger = logging.getLogger("accounts.gateway.state")

# дефолти; оператор крутить через Setting без деплою
DEFAULT_COOLDOWN_BASE_SEC = 300
DEFAULT_COOLDOWN_CAP_SEC = 3600
DEFAULT_REPAIR_AFTER = 3
DEFAULT_RESOLVE_BASE_SEC = 6 * 3600     # перша відмова резолву
DEFAULT_RESOLVE_CAP_SEC = 24 * 3600     # стеля подвоєнь

_TRANSPORT_MARKERS = ("Connection to Telegram failed", "Connection refused",
                      "Server closed the connection", "підвисло")
_RESOLVE_MARKERS = ("as username", "Cannot find any entity")


class Outcome(str, Enum):
    OK = "ok"
    TRANSPORT = "transport"      # проксі/мережа: винен не акаунт і не джерело
    FLOOD = "flood"              # FloodWait: пауза рівно на стільки, скільки сказали
    RESOLVE = "resolve"          # вичерпано добовий ліміт резолву юзернеймів
    DEAUTH = "deauth"            # сесію відкликано/вбито
    BANNED = "banned"
    TELEGRAM = "telegram"        # інша RPC-помилка: не наша, але й не фатальна
    INTERNAL = "internal"        # наш баг / невідоме


def _setting_int(key: str, default: int) -> int:
    from analysis.models import Setting  # тут, щоб accounts не тягнув analysis при імпорті
    try:
        return int(Setting.get(key, default))
    except (TypeError, ValueError):
        return default


def classify(exc: BaseException | None) -> tuple[Outcome, dict]:
    """Виняток → (Outcome, meta). Чиста функція, без БД."""
    if exc is None:
        return Outcome.OK, {}
    if isinstance(exc, FloodWaitError):
        return Outcome.FLOOD, {"seconds": int(getattr(exc, "seconds", 60) or 60)}
    if isinstance(exc, (UserDeactivatedBanError, PhoneNumberBannedError)):
        return Outcome.BANNED, {}
    if isinstance(exc, (AuthKeyUnregisteredError, SessionRevokedError,
                        AuthKeyDuplicatedError, UserDeactivatedError)):
        return Outcome.DEAUTH, {}
    if isinstance(exc, (UsernameInvalidError, UsernameNotOccupiedError)):
        return Outcome.RESOLVE, {}
    text = str(exc) or ""
    if isinstance(exc, ValueError) and any(m in text for m in _RESOLVE_MARKERS):
        return Outcome.RESOLVE, {}
    # порядок важливий: ConnectionError/TimeoutError — підкласи OSError/Exception,
    # а RPCError Telethon — ні, тож транспорт перевіряємо до RPC
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)):
        return Outcome.TRANSPORT, {}
    if any(m in text for m in _TRANSPORT_MARKERS):
        return Outcome.TRANSPORT, {}
    if isinstance(exc, RPCError):
        return Outcome.TELEGRAM, {}
    return Outcome.INTERNAL, {}


def _flag_proxy(account) -> None:
    """Проксі акаунта — у голову черги ремонту. is_working НЕ чіпаємо: це робить
    лише mark_repair_failed, бо «неробоча» проксі має означати «акаунт стоїть»,
    а не «йдемо без проксі»."""
    if not account.proxy_id:
        return
    from accounts.models import Proxy
    Proxy.objects.filter(pk=account.proxy_id).update(
        fail_count=F("fail_count") + 1, last_tested_at=None)


def apply(account, outcome: Outcome, *, meta: dict | None = None,
          error: str = "", now=None) -> list[str]:
    """Перехід стану за таблицею §4. Мутує й зберігає рядок, повертає список
    змінених полів. `meta` доповнюється: needs_repair=True, коли транспортних
    збоїв поспіль ≥ gateway_repair_after."""
    meta = meta if meta is not None else {}
    now = now or djtz.now()
    A = type(account)
    fields: list[str] = []

    def _set(name, value):
        if getattr(account, name) != value:
            setattr(account, name, value)
            fields.append(name)

    if outcome is Outcome.OK:
        _set("state", A.STATE_READY)
        _set("transport_failures", 0)
        _set("cooldown_until", None)
        _set("last_ok_at", now)
        _set("last_error", "")
        if meta.get("resolved"):
            # акаунт щойно резолвив юзернейм — ліміт живий, паузу знімаємо
            _set("resolve_failures", 0)
            _set("resolve_exhausted_until", None)
    elif outcome is Outcome.TRANSPORT:
        n = (account.transport_failures or 0) + 1
        base = _setting_int("gateway_cooldown_base_sec", DEFAULT_COOLDOWN_BASE_SEC)
        cap = _setting_int("gateway_cooldown_cap_sec", DEFAULT_COOLDOWN_CAP_SEC)
        _set("transport_failures", n)
        _set("state", A.STATE_COOLDOWN)
        _set("cooldown_until", now + timedelta(seconds=min(base * (2 ** (n - 1)), cap)))
        _set("last_error", error[:2000])
        _flag_proxy(account)
        meta["needs_repair"] = n >= _setting_int("gateway_repair_after", DEFAULT_REPAIR_AFTER)
    elif outcome is Outcome.FLOOD:
        secs = int(meta.get("seconds") or 60)
        _set("state", A.STATE_COOLDOWN)
        _set("cooldown_until", now + timedelta(seconds=secs))
        _set("last_error", error[:2000] or f"FloodWait {secs}s")
    elif outcome is Outcome.RESOLVE:
        # акаунт живий: лише резолв вичерпано, для операцій без резолву він ready.
        # Пауза адаптивна: 6 год, повторна відмова одразу після паузи — 12, далі
        # 24 (кап). Успішний резолв (meta resolved) скидає. Обмежений SpamBot-ом
        # акаунт так сам «відсунеться» до доби, живий — повернеться за 6 год.
        n = (account.resolve_failures or 0) + 1
        base = _setting_int("gateway_resolve_base_sec", DEFAULT_RESOLVE_BASE_SEC)
        cap = _setting_int("gateway_resolve_cap_sec", DEFAULT_RESOLVE_CAP_SEC)
        _set("resolve_failures", n)
        _set("resolve_exhausted_until", now + timedelta(seconds=min(base * (2 ** (n - 1)), cap)))
        _set("last_error", error[:2000])
    elif outcome is Outcome.DEAUTH:
        _set("state", A.STATE_DEAUTHORIZED)
        _set("is_authenticated", False)
        _set("gateway_connected", False)
        _set("last_error", error[:2000])
    elif outcome is Outcome.BANNED:
        _set("state", A.STATE_BANNED)
        _set("is_authenticated", False)
        _set("gateway_connected", False)
        _set("last_error", error[:2000])
    else:  # TELEGRAM / INTERNAL — акаунт не винен, стан не рухаємо
        _set("last_error", error[:2000])

    if fields:
        account.save(update_fields=fields)
    logger.info("acc=#%s proxy=#%s outcome=%s → state=%s%s",
                account.pk, account.proxy_id or "-", outcome.value, account.state,
                " needs_repair" if meta.get("needs_repair") else "")
    return fields


def mark_repair_failed(account, error: str = "") -> list[str]:
    """Усі регенерації session-id проксі провалились: проксі мертва, акаунт
    стоїть, доки оператор не дасть іншу (needs_proxy)."""
    A = type(account)
    if account.proxy_id:
        from accounts.models import Proxy
        Proxy.objects.filter(pk=account.proxy_id).update(is_working=False)
    account.state = A.STATE_NEEDS_PROXY
    account.cooldown_until = None
    account.last_error = (error or "проксі не зʼєднується після ремонту")[:2000]
    account.save(update_fields=["state", "cooldown_until", "last_error"])
    logger.warning("acc=#%s proxy=#%s ремонт провалено → state=needs_proxy",
                   account.pk, account.proxy_id or "-")
    return ["state", "cooldown_until", "last_error"]


def mark_repaired(account, now=None) -> list[str]:
    """Проксі замінено/полагоджено і живість підтверджено."""
    return apply(account, Outcome.OK, now=now)
