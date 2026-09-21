"""LiveAccount — один Telegram-акаунт у процесі gateway.

Тримає довгоживучий Telethon-клієнт і `asyncio.Lock`: одночасно з акаунтом
працює рівно одна операція. Перед операцією — ворота (стан/пауза/проксі),
після — перехід стану через `state.classify/apply`. Споживачі бачать лише
результат або `GatewayError(kind, message, retry_after)`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.utils import timezone as djtz
from telethon import TelegramClient
from telethon.sessions import StringSession

from . import _telethon, state as st
from .proxies import to_telethon

logger = logging.getLogger("accounts.gateway.live")

# Outcome → kind у відповіді клієнту
KIND = {
    st.Outcome.TRANSPORT: "transport",
    st.Outcome.FLOOD: "rate_limited",
    st.Outcome.RESOLVE: "unavailable",
    st.Outcome.DEAUTH: "unavailable",
    st.Outcome.BANNED: "unavailable",
    st.Outcome.TELEGRAM: "telegram",
    st.Outcome.INTERNAL: "internal",
}


class GatewayError(Exception):
    def __init__(self, kind: str, message: str, retry_after: int | None = None,
                 reason: str = ""):
        super().__init__(message)
        self.kind, self.message, self.retry_after, self.reason = kind, message, retry_after, reason

    def as_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message,
                "retry_after": self.retry_after, "reason": self.reason}


@dataclass
class OpContext:
    client: TelegramClient
    account: object          # знімок рядка TelegramAccount
    save: object             # async save(**fields)


def _setting_int(key: str, default: int) -> int:
    from analysis.models import Setting
    try:
        return int(Setting.get(key, default))
    except (TypeError, ValueError):
        return default


@sync_to_async
def _load(account_id: int):
    from accounts.models import TelegramAccount
    return TelegramAccount.objects.select_related("proxy").get(pk=account_id)


@sync_to_async
def _save_fields(account_id: int, **fields):
    from accounts.models import TelegramAccount
    TelegramAccount.objects.filter(pk=account_id).update(**fields)


@sync_to_async
def _apply(account, outcome, meta, error):
    return st.apply(account, outcome, meta=meta, error=error)


_setting_int_async = sync_to_async(_setting_int)


def _did_resolve(op: str, kwargs: dict, result) -> bool:
    """Чи операція успішно пройшла через резолв юзернейма (не хеш і не id):
    тоді ліміт резолву акаунта точно живий і паузу можна зняти."""
    if op in ("resolve", "channel_meta"):
        return not str(kwargs.get("handle", "")).lstrip("-").isdigit()
    if op in ("scan", "search"):
        by_key = {r.get("key"): r for r in (result or [])} if isinstance(result, list) else {}
        for ch in kwargs.get("chats") or []:
            ent = ch.get("entity") or {}
            if (ent.get("username") or ent.get("linked_parent")) \
                    and not (by_key.get(ch.get("key")) or {}).get("error"):
                return True
    return False


def build_client(account) -> TelegramClient:
    """Клієнт ЛИШЕ через проксі акаунта. Немає робочої — GatewayError, не
    фолбек на IP сервера (саме так горіли сесії: один auth key з двох IP)."""
    p = account.proxy
    if p is None or not p.is_active or not p.is_working:
        raise GatewayError("unavailable", f"акаунт #{account.pk}: немає робочої проксі",
                           reason="needs_proxy")
    return TelegramClient(
        StringSession(account.session_string or ""),
        int(account.api_id), account.api_hash,
        proxy=to_telethon(p.proxy_string, p.proxy_type),
        connection_retries=3, retry_delay=2, timeout=20,
        **account.client_kwargs(),
    )


class LiveAccount:
    def __init__(self, account_id: int, pool=None):
        self.id = account_id
        self.pool = pool
        self.lock = asyncio.Lock()
        self.client: TelegramClient | None = None
        self._client_proxy: str | None = None   # рядок проксі, під який зібрано клієнт
        self._client_session: str = ""          # session_string, під який зібрано клієнт
        self.last_used = 0.0
        self.connected_at = 0.0
        self.ops_ok = 0
        self.ops_failed = 0

    # ---- зʼєднання ----
    async def _ensure_client(self, account) -> TelegramClient:
        proxy_sig = account.proxy.proxy_string if account.proxy else None
        session_sig = account.session_string or ""
        if self.client is not None and (self._client_proxy != proxy_sig
                                        or self._client_session != session_sig):
            # оператор змінив проксі або сесію — старий клієнт більше не той акаунт
            await self.drop()
        if self.client is None:
            client = build_client(account)
            try:
                await asyncio.wait_for(client.connect(), timeout=30)
            except Exception:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                raise
            self.client, self._client_proxy, self._client_session = client, proxy_sig, session_sig
            self.connected_at = time.time()
            await _save_fields(self.id, gateway_connected=True)
            logger.info("acc=#%s підключено через проксі #%s", self.id,
                        account.proxy_id or "-")
        elif not self.client.is_connected():
            await asyncio.wait_for(self.client.connect(), timeout=30)
        return self.client

    async def drop(self, mark_db: bool = True) -> None:
        client, self.client, self._client_proxy, self._client_session = self.client, None, None, ""
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            if mark_db:
                try:
                    await _save_fields(self.id, gateway_connected=False)
                except Exception:  # noqa: BLE001
                    pass

    # ---- ворота перед операцією ----
    @staticmethod
    def _gate(account, needs_auth: bool) -> None:
        A = type(account)
        if account.state in (A.STATE_DEAUTHORIZED, A.STATE_BANNED) and needs_auth:
            raise GatewayError("unavailable", f"акаунт #{account.pk}: {account.get_state_display()}",
                               reason=account.state)
        if needs_auth and not account.is_authenticated:
            raise GatewayError("unavailable", f"акаунт #{account.pk}: не авторизований",
                               reason="deauthorized")
        if account.state == A.STATE_NEEDS_PROXY:
            raise GatewayError("unavailable", f"акаунт #{account.pk}: потрібна проксі",
                               reason="needs_proxy")
        if account.cooldown_until and account.cooldown_until > djtz.now():
            left = int((account.cooldown_until - djtz.now()).total_seconds()) + 1
            raise GatewayError("rate_limited", f"акаунт #{account.pk}: пауза ще {left}с",
                               retry_after=left, reason="cooldown")
        if not account.is_active:
            raise GatewayError("unavailable", f"акаунт #{account.pk}: вимкнений", reason="inactive")

    # ---- виклик ----
    async def call(self, op: str, kwargs: dict | None = None, timeout: float | None = None):
        if op not in _telethon.OPS:
            raise GatewayError("internal", f"невідома операція {op!r}")
        fn, default_timeout, needs_auth = _telethon.OPS[op]
        kwargs = kwargs or {}
        wait = await _setting_int_async("gateway_lock_wait_sec", 30)
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=wait)
        except asyncio.TimeoutError:
            raise GatewayError("busy", f"акаунт #{self.id}: зайнятий довше {wait}с",
                               retry_after=wait, reason="busy")
        try:
            return await self._call_locked(op, fn, kwargs, timeout or default_timeout, needs_auth)
        finally:
            self.lock.release()

    async def _call_locked(self, op, fn, kwargs, timeout, needs_auth):
        account = await _load(self.id)
        self._gate(account, needs_auth)
        self.last_used = time.time()
        error_for_state: BaseException | None = None
        try:
            client = await self._ensure_client(account)
            if needs_auth and not await client.is_user_authorized():
                from telethon.errors import AuthKeyUnregisteredError
                raise AuthKeyUnregisteredError(request=None)
            ctx = OpContext(client=client, account=account,
                            save=lambda **f: _save_fields(self.id, **f))
            result = await asyncio.wait_for(fn(ctx, **kwargs), timeout=timeout)
        except GatewayError:
            raise
        except BaseException as e:  # noqa: BLE001 — класифікуємо все, крім наших
            if isinstance(e, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            error_for_state = e
        if error_for_state is None:
            self.ops_ok += 1
            await _apply(account, st.Outcome.OK,
                         {"resolved": _did_resolve(op, kwargs, result)}, "")
            await _save_fields(self.id, last_used_at=djtz.now())
            return result

        e = error_for_state
        self.ops_failed += 1
        outcome, meta = st.classify(e)
        err_text = f"{type(e).__name__}: {str(e)[:300]}"
        fields = await _apply(account, outcome, meta, err_text)
        logger.warning("acc=#%s proxy=#%s op=%s → %s: %s", self.id, account.proxy_id or "-",
                       op, outcome.value, err_text)
        if outcome in (st.Outcome.TRANSPORT, st.Outcome.DEAUTH, st.Outcome.BANNED):
            await self.drop()
        if outcome is st.Outcome.TRANSPORT and meta.get("needs_repair") and self.pool is not None:
            self.pool.schedule_repair(self.id)
        retry_after = None
        if outcome is st.Outcome.FLOOD:
            retry_after = int(meta.get("seconds") or 60)
        elif outcome is st.Outcome.TRANSPORT and "cooldown_until" in fields and account.cooldown_until:
            retry_after = int((account.cooldown_until - djtz.now()).total_seconds()) + 1
        raise GatewayError(KIND[outcome], err_text, retry_after=retry_after,
                           reason=outcome.value)

    # ---- для /health ----
    def stats(self) -> dict:
        return {"id": self.id, "connected": bool(self.client and self.client.is_connected()),
                "locked": self.lock.locked(), "ops_ok": self.ops_ok,
                "ops_failed": self.ops_failed,
                "idle_sec": int(time.time() - self.last_used) if self.last_used else None}
