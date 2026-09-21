"""LiveAccount / AccountPool / HTTP-сервер gateway з фейковим Telethon.

Корутини ганяємо через async_to_sync, а не asyncio.run: тоді sync_to_async
(thread_sensitive) виконує ORM у ПОТОЦІ ТЕСТУ, і код бачить тестову
транзакцію; з asyncio.run довелося б робити transaction=True (flush усієї
бази на кожен тест)."""
import asyncio

import pytest
from asgiref.sync import async_to_sync
from aiohttp.test_utils import TestClient, TestServer
from django.utils import timezone
from telethon.errors import FloodWaitError

from accounts.gateway import _telethon, live as lv
from accounts.gateway.pool import AccountPool
from accounts.gateway.server import make_app
from accounts.models import Proxy, TelegramAccount

pytestmark = pytest.mark.django_db


class FakeClient:
    """Мінімум Telethon: connect/disconnect/is_connected/is_user_authorized."""
    instances: list = []

    def __init__(self, *a, authorized=True, connect_error=None, **kw):
        self.authorized, self.connect_error = authorized, connect_error
        self._connected = False
        self.calls = []
        FakeClient.instances.append(self)

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        self._connected = True

    async def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    async def is_user_authorized(self):
        return self.authorized


@pytest.fixture
def acc(django_user_model):
    u = django_user_model.objects.create(username="op")
    p = Proxy.objects.create(proxy_string="h:1:u:s_country-ru_session-abc_lifetime-168h")
    return TelegramAccount.objects.create(user=u, phone_number="+1", is_authenticated=True,
                                          proxy=p, session_string="")


@pytest.fixture
def fake(monkeypatch):
    """build_client → FakeClient; операція "echo" повертає kwargs; "boom" — кидає."""
    FakeClient.instances.clear()
    factory = {"kw": {}}
    # підміняємо сам TelegramClient: build_client лишається справжнім разом із
    # перевіркою «без робочої проксі не працюємо»
    monkeypatch.setattr(lv, "TelegramClient", lambda *a, **kw: FakeClient(**factory["kw"]))

    async def echo(ctx, **kw):
        ctx.client.calls.append(("echo", kw))
        return {"echo": kw}

    box = {"exc": None}

    async def boom(ctx, **kw):
        raise box["exc"]

    async def slow(ctx, **kw):
        await asyncio.sleep(kw.get("sec", 0.3))
        return "done"

    monkeypatch.setitem(_telethon.OPS, "echo", (echo, 5, True))
    monkeypatch.setitem(_telethon.OPS, "boom", (boom, 5, True))
    monkeypatch.setitem(_telethon.OPS, "slow", (slow, 5, True))
    # фоновий планувальник ремонту стартує разом із сервером і взяв би акаунт у
    # «профілактику» справжнім Telethon — у тестах йому нема кого лагодити
    from accounts.gateway import repair as rp
    monkeypatch.setattr(rp, "due_for_repair", lambda: [])
    factory["boom"] = box
    return factory


def run(coro):
    async def _wrap():
        return await coro
    return async_to_sync(_wrap)()


# ------------------------------------------------------------------ LiveAccount

def test_ok_connects_once_and_applies_ok(acc, fake):
    live = lv.LiveAccount(acc.id)
    assert run(live.call("echo", {"a": 1})) == {"echo": {"a": 1}}
    assert run(live.call("echo", {"a": 2})) == {"echo": {"a": 2}}
    assert len(FakeClient.instances) == 1                 # довгоживучий клієнт
    acc.refresh_from_db()
    assert acc.gateway_connected and acc.last_ok_at and acc.state == "ready"
    assert acc.last_used_at is not None


def test_no_proxy_is_unavailable_not_fallback(acc, fake):
    acc.proxy = None
    acc.save()
    live = lv.LiveAccount(acc.id)
    with pytest.raises(lv.GatewayError) as ei:
        run(live.call("echo"))
    assert ei.value.kind == "unavailable" and ei.value.reason == "needs_proxy"
    assert FakeClient.instances == []


def test_dead_proxy_is_unavailable(acc, fake):
    acc.proxy.is_working = False
    acc.proxy.save()
    live = lv.LiveAccount(acc.id)
    with pytest.raises(lv.GatewayError) as ei:
        run(live.call("echo"))
    assert ei.value.reason == "needs_proxy"


def test_transport_error_drops_client_and_cooldowns(acc, fake):
    fake["boom"]["exc"] = ConnectionError("Connection to Telegram failed 5 time(s)")
    live = lv.LiveAccount(acc.id)
    with pytest.raises(lv.GatewayError) as ei:
        run(live.call("boom"))
    assert ei.value.kind == "transport" and ei.value.retry_after >= 299
    assert live.client is None                            # клієнт скинуто
    acc.refresh_from_db(); acc.proxy.refresh_from_db()
    assert acc.state == "cooldown" and acc.transport_failures == 1
    assert acc.proxy.fail_count == 1 and acc.gateway_connected is False
    # ворота: наступний виклик не йде в Telegram, а повертає rate_limited
    with pytest.raises(lv.GatewayError) as ei2:
        run(live.call("echo"))
    assert ei2.value.kind == "rate_limited" and ei2.value.reason == "cooldown"


def test_connect_failure_is_transport(acc, fake):
    fake["kw"]["connect_error"] = OSError("Connection refused by destination host")
    live = lv.LiveAccount(acc.id)
    with pytest.raises(lv.GatewayError) as ei:
        run(live.call("echo"))
    assert ei.value.kind == "transport"
    acc.refresh_from_db()
    assert acc.state == "cooldown"


def test_flood_is_rate_limited_with_seconds(acc, fake):
    fake["boom"]["exc"] = FloodWaitError(request=None, capture=77)
    live = lv.LiveAccount(acc.id)
    with pytest.raises(lv.GatewayError) as ei:
        run(live.call("boom"))
    assert ei.value.kind == "rate_limited" and ei.value.retry_after == 77
    assert live.client is not None                        # FloodWait клієнт не рве


def test_unauthorized_session_deauthorizes(acc, fake):
    fake["kw"]["authorized"] = False
    live = lv.LiveAccount(acc.id)
    with pytest.raises(lv.GatewayError) as ei:
        run(live.call("echo"))
    assert ei.value.kind == "unavailable" and ei.value.reason == "deauth"
    acc.refresh_from_db()
    assert acc.state == "deauthorized" and acc.is_authenticated is False
    with pytest.raises(lv.GatewayError) as ei2:            # ворота, без Telegram
        run(live.call("echo"))
    assert ei2.value.reason == "deauthorized" and len(FakeClient.instances) == 1


def test_resolve_error_keeps_client_marks_resolve(acc, fake):
    fake["boom"]["exc"] = ValueError('No user has "x" as username')
    live = lv.LiveAccount(acc.id)
    with pytest.raises(lv.GatewayError) as ei:
        run(live.call("boom"))
    assert ei.value.kind == "unavailable" and ei.value.reason == "resolve"
    acc.refresh_from_db()
    assert acc.state == "ready" and not acc.can_resolve()


def test_proxy_change_rebuilds_client(acc, fake):
    live = lv.LiveAccount(acc.id)
    run(live.call("echo"))
    acc.proxy.proxy_string = "h:1:u:s_country-ru_session-zzz_lifetime-168h"
    acc.proxy.save()
    run(live.call("echo"))
    assert len(FakeClient.instances) == 2


def test_lock_serializes_and_busy_after_wait(acc, fake, monkeypatch):
    from analysis.models import Setting
    Setting.objects.create(key="gateway_lock_wait_sec", value="1")
    live = lv.LiveAccount(acc.id)

    # один event loop на обидва сценарії: asyncio.Lock привʼязується до loop
    # при першому очікуванні (у gateway loop один на процес)
    async def scenario():
        first = asyncio.create_task(live.call("slow", {"sec": 0.3}))
        await asyncio.sleep(0.05)
        second = await live.call("echo", {"b": 1})          # чекає lock, потім іде
        assert (await first, second) == ("done", {"echo": {"b": 1}})

        first = asyncio.create_task(live.call("slow", {"sec": 2}))
        await asyncio.sleep(0.05)
        try:
            with pytest.raises(lv.GatewayError) as ei:
                await live.call("echo")
            assert ei.value.kind == "busy"
        finally:
            first.cancel()
    run(scenario())


def test_unknown_op(acc, fake):
    with pytest.raises(lv.GatewayError) as ei:
        run(lv.LiveAccount(acc.id).call("nope"))
    assert ei.value.kind == "internal"


# ------------------------------------------------------------------ pool + repair

def test_needs_repair_schedules_in_pool(acc, fake):
    from analysis.models import Setting
    Setting.objects.create(key="gateway_repair_after", value="1")
    fake["boom"]["exc"] = ConnectionError("refused")
    pool = AccountPool()
    live = pool.get(acc.id)
    with pytest.raises(lv.GatewayError):
        run(live.call("boom"))
    assert acc.id in pool._repair_pending


def test_repair_regenerates_proxy(acc, fake, monkeypatch):
    from accounts.gateway import repair as rp
    acc.state, acc.transport_failures = "cooldown", 3
    acc.cooldown_until = timezone.now()
    acc.save()
    probes = []

    async def probe(account, proxy_string, proxy_type, timeout=30):
        probes.append(proxy_string)
        return len(probes) == 2          # старий рядок мертвий, перша регенерація жива
    monkeypatch.setattr(rp, "_probe", probe)
    live = lv.LiveAccount(acc.id)
    res = run(rp.repair(live))
    assert res == {"ok": True, "action": "regenerated", "attempt": 1}
    acc.refresh_from_db(); acc.proxy.refresh_from_db()
    assert acc.state == "ready" and acc.transport_failures == 0
    assert acc.proxy.proxy_string != "h:1:u:s_country-ru_session-abc_lifetime-168h"
    assert acc.proxy.is_working and acc.proxy.fail_count == 0


def test_repair_fails_to_needs_proxy(acc, fake, monkeypatch):
    from accounts.gateway import repair as rp

    async def probe(*a, **kw):
        return False
    monkeypatch.setattr(rp, "_probe", probe)
    res = run(rp.repair(lv.LiveAccount(acc.id)))
    assert res["action"] == "failed"
    acc.refresh_from_db(); acc.proxy.refresh_from_db()
    assert acc.state == "needs_proxy" and acc.proxy.is_working is False


def test_due_for_repair(acc):
    from accounts.gateway import repair as rp
    from analysis.models import Setting
    acc.state, acc.transport_failures = "cooldown", 3
    acc.save()
    assert rp.due_for_repair() == [acc.id]
    acc.state, acc.transport_failures = "ready", 0
    acc.save()
    assert rp.due_for_repair() == []                       # профілактика вимкнена
    Setting.objects.create(key="gateway_repair_proactive", value="1")
    assert rp.due_for_repair() == [acc.id]                 # last_ok_at порожній
    acc.last_ok_at = timezone.now()
    acc.save()
    assert rp.due_for_repair() == []


# ------------------------------------------------------------------ HTTP

def test_http_call_and_errors(acc, fake):
    fake["boom"]["exc"] = FloodWaitError(request=None, capture=5)

    async def go():
        async with TestClient(TestServer(make_app(AccountPool()))) as c:
            r = await (await c.post(f"/accounts/{acc.id}/echo", json={"x": 1})).json()
            assert r == {"ok": True, "result": {"echo": {"x": 1}}}
            r = await (await c.post(f"/accounts/{acc.id}/boom")).json()
            assert r["ok"] is False and r["error"]["kind"] == "rate_limited"
            assert r["error"]["retry_after"] == 5
            r = await (await c.post(f"/accounts/{acc.id}/nope")).json()
            assert r["error"]["kind"] == "internal"
            h = await (await c.get("/health")).json()
            assert h["ok"] and h["pool"]["accounts_seen"] == 1
            r = await (await c.post(f"/accounts/{acc.id}/invalidate")).json()
            assert r["ok"] and h["pool"]["connected"] == 1
    run(go())


def test_did_resolve_detection():
    assert lv._did_resolve("resolve", {"handle": "chan"}, {}) is True
    assert lv._did_resolve("resolve", {"handle": "-100123"}, {}) is False
    chats = [{"key": 1, "entity": {"username": "a"}}, {"key": 2, "entity": {"channel_id": 1, "access_hash": 2}}]
    assert lv._did_resolve("scan", {"chats": chats}, [{"key": 1, "error": None}, {"key": 2}]) is True
    assert lv._did_resolve("scan", {"chats": chats}, [{"key": 1, "error": "ChannelPrivate"}]) is False
    assert lv._did_resolve("scan", {"chats": chats[1:]}, [{"key": 2}]) is False
    assert lv._did_resolve("get_me", {}, {}) is False
