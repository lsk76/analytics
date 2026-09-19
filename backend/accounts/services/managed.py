"""ManagedAccount — тонкий клієнт до tg-gateway (docs/tg-gateway-plan.md §3.2).

Легка ручка до рядка TelegramAccount: без стану, без Telethon, кожен метод —
один HTTP-виклик у gateway. Споживач знає три винятки:

  RateLimited(retry_after)   — зачекати й повторити (FloodWait, пауза, зайнятий,
                               gateway недоступний); НЕ збій джерела/чату
  AccountUnavailable(reason) — цьому акаунту зараз не можна: взяти інший
                               (transport, needs_proxy, deauthorized, resolve…)
  TelegramOpError            — операція не вдалась з вини цілі (приватний чат,
                               нема прав, битий аргумент): рахувати як збій
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx

logger = logging.getLogger("accounts.managed")

GATEWAY_DOWN_RETRY = 30


def gateway_url() -> str:
    from django.conf import settings
    return (getattr(settings, "TG_GATEWAY_URL", None)
            or os.environ.get("TG_GATEWAY_URL") or "http://tg-gateway:8010").rstrip("/")


class RateLimited(Exception):
    def __init__(self, retry_after: int = 60, message: str = "", reason: str = ""):
        super().__init__(message or f"повторити за {retry_after}с")
        self.retry_after, self.reason = int(retry_after or 60), reason


class GatewayDown(RateLimited):
    """gateway не відповідає: споживачі трактують як RateLimited(30)."""
    def __init__(self, message: str = ""):
        super().__init__(GATEWAY_DOWN_RETRY, message or "tg-gateway недоступний", "gateway_down")


class AccountUnavailable(Exception):
    def __init__(self, reason: str, message: str = "", retry_after: int | None = None):
        super().__init__(message or reason)
        self.reason, self.retry_after = reason, retry_after


class TelegramOpError(Exception):
    def __init__(self, message: str, kind: str = "telegram"):
        super().__init__(message)
        self.kind = kind


def _raise_for(err: dict):
    kind, msg = err.get("kind"), err.get("message") or ""
    ra, reason = err.get("retry_after"), err.get("reason") or ""
    if kind in ("busy", "rate_limited"):
        raise RateLimited(ra or 30, msg, reason)
    if kind == "transport":
        raise AccountUnavailable("transport", msg, ra)
    if kind == "unavailable":
        raise AccountUnavailable(reason or "unavailable", msg, ra)
    raise TelegramOpError(msg, kind or "internal")


def gw_result(call, **empty) -> dict:
    """Для адмінки/воркерів, що очікують словник {ok, error, …}, а не виняток:
    будь-яка помилка gateway/акаунта → {"ok": False, "error": текст, **empty}."""
    try:
        res = call()
    except (RateLimited, AccountUnavailable, TelegramOpError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", **empty}
    if isinstance(res, dict):
        return {**empty, **res} if "ok" in res else {"ok": True, **empty, **res}
    return {"ok": True, "result": res, **empty}


def _dt(value):
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


class ManagedAccount:
    def __init__(self, account_id: int, row=None):
        self.id = int(account_id)
        self._row = row

    def __repr__(self):
        return f"<ManagedAccount #{self.id}>"

    def __eq__(self, other):
        return isinstance(other, ManagedAccount) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    # ---- стан з БД (без gateway) ----
    @property
    def row(self):
        """Рядок TelegramAccount; перечитується на вимогу (стан міняє gateway)."""
        if self._row is None:
            from accounts.models import TelegramAccount
            self._row = TelegramAccount.objects.select_related("proxy").get(pk=self.id)
        return self._row

    def refresh(self):
        self._row = None
        return self.row

    @property
    def state(self) -> str:
        return self.row.state

    @property
    def cooldown_until(self):
        return self.row.cooldown_until

    @property
    def proxy_id(self):
        return self.row.proxy_id

    @property
    def is_available(self) -> bool:
        return self.row.is_available

    def can_resolve(self) -> bool:
        return self.row.can_resolve()

    # ---- транспорт ----
    def _call(self, op: str, timeout: float | None = None, **kwargs):
        url = f"{gateway_url()}/accounts/{self.id}/{op}"
        body = dict(kwargs)
        if timeout:
            body["timeout"] = timeout
        # HTTP-таймаут довший за таймаут операції: відповідь має дійти
        http_timeout = (timeout or 120) + 15
        try:
            resp = httpx.post(url, json=body, timeout=http_timeout)
        except httpx.HTTPError as e:
            logger.warning("acc=#%s op=%s: gateway недоступний: %r", self.id, op, e)
            raise GatewayDown(f"{type(e).__name__}: {str(e)[:120]}")
        try:
            data = resp.json()
        except ValueError:
            raise GatewayDown(f"gateway: не JSON (HTTP {resp.status_code})")
        if not data.get("ok"):
            _raise_for(data.get("error") or {})
        self._row = None      # gateway щойно змінив стан рядка
        return data.get("result")

    # ---- читання ----
    def scan(self, chats: list[dict], patterns: list[str] | None = None,
             media: dict | None = None, timeout: float | None = None) -> list[dict]:
        return self._call("scan", timeout=timeout, chats=chats, patterns=patterns or [],
                          media=media)

    def fetch_history(self, handle, min_id: int = 0, limit: int = 50,
                      reverse: bool = False, peer_sink: dict | None = None) -> list[dict]:
        """Один чат, без патернів і медіа. -> [{id, text, date(datetime), media_kind}]
        peer_sink — сюди кладемо {id, access_hash}, якщо gateway резолвив."""
        entity = handle if isinstance(handle, dict) else (
            {"id": int(handle)} if str(handle).lstrip("-").isdigit()
            else {"username": str(handle).lstrip("@")})
        rows = self.scan([{"key": "h", "entity": entity, "min_id": min_id,
                           "limit": limit, "reverse": reverse}])
        row = rows[0]
        if row.get("error"):
            raise TelegramOpError(row["error"])
        if peer_sink is not None and row.get("resolved"):
            peer_sink.update(row["resolved"])
        return [{"id": h["mid"], "text": h["text"], "date": _dt(h.get("date")),
                 "media_kind": (h.get("media") or {}).get("kind")} for h in row["hits"]]

    def search(self, chats: list[dict], terms: list[str], since=None, limit: int = 50,
               pause: float = 1.0) -> list[dict]:
        return self._call("search", chats=chats, terms=terms,
                          since=since.isoformat() if hasattr(since, "isoformat") else since,
                          limit=limit, pause=pause)

    def resolve(self, handle) -> dict:
        return self._call("resolve", handle=handle)

    def channel_meta(self, handle) -> dict:
        return self._call("channel_meta", handle=handle)

    def message_date(self, handle, msg_id: int):
        return _dt(self._call("message_date", handle=handle, msg_id=int(msg_id)))

    def dialogs(self, kind: str = "", limit: int = 200) -> list[dict]:
        return self._call("dialogs", kind=kind, limit=limit)

    def recent_messages(self, peer, limit: int = 20) -> list[dict]:
        return self._call("recent_messages", peer=peer, limit=limit)

    def get_me(self) -> dict:
        return self._call("get_me")

    def check_alive(self) -> dict:
        """{"state", "ok", "detail"} — як колишній check_alive_sync, але
        помилки теж повертає словником: адмінці/MCP потрібен текст, не виняток."""
        try:
            return self._call("check_alive")
        except AccountUnavailable as e:
            return {"state": {"deauthorized": "розлогінений", "banned": "ЗАБАНЕНИЙ",
                              "deauth": "сесію відкликано", "needs_proxy": "немає проксі",
                              "transport": "проксі не зʼєднує"}.get(e.reason, e.reason),
                    "ok": False, "detail": str(e)[:120]}
        except RateLimited as e:
            return {"state": f"пауза ({e.reason or 'rate-limited'}, {e.retry_after}с)",
                    "ok": False, "detail": str(e)[:120]}
        except TelegramOpError as e:
            return {"state": f"помилка: {e.kind}", "ok": False, "detail": str(e)[:120]}

    # ---- дії ----
    def forward(self, from_chat, msg_id: int, to_chat) -> dict:
        return self._call("forward", from_chat=from_chat, msg_id=int(msg_id), to_chat=to_chat)

    def send_post(self, to_chat, text: str, src_chat=None, src_msg_id: int = 0,
                  src_peer: dict | None = None) -> dict:
        return self._call("send_post", to_chat=to_chat, text=text, src_chat=src_chat,
                          src_msg_id=int(src_msg_id or 0), src_peer=src_peer)

    def join(self, handles: list[str]) -> dict:
        return self._call("join", handles=list(handles))

    # ---- сервіс ----
    def spam_status(self) -> dict:
        return self._call("spam_status")

    def test_bot(self, bot_username: str, feedback_text: str = "",
                 choices: list | None = None, max_steps: int = 15) -> dict:
        return self._call("test_bot", bot_username=bot_username, feedback_text=feedback_text,
                          choices=choices, max_steps=max_steps)

    def botfather(self, op: str, **kw) -> dict:
        return self._call("botfather", op=op, **kw)

    # ---- авторизація ----
    def send_code(self) -> dict:
        return self._call("send_code")

    def verify_code(self, code: str, password: str | None = None) -> dict:
        return self._call("verify_code", code=code, password=password)

    # ---- самообслуговування ----
    def repair(self) -> dict:
        return self._call("repair")

    def replace_proxy(self, proxy_id: int) -> dict:
        return self._call("replace_proxy", proxy_id=int(proxy_id))

    def invalidate(self) -> None:
        """Рядок змінено поза gateway (проксі/сесія) — хай перебудує клієнт."""
        try:
            self._call("invalidate")
        except GatewayDown:
            pass      # gateway підніметься з чистим пулом
