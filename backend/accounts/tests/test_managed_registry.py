"""ManagedAccount (HTTP-клієнт до gateway, мок respx) і registry (вибір із пулу)."""
from datetime import timedelta

import httpx
import pytest
import respx
from django.utils import timezone

from accounts.models import AccountTag, Proxy, TelegramAccount
from accounts.services import managed as md, registry
from analysis.models import Setting

pytestmark = pytest.mark.django_db

GW = "http://tg-gateway:8010"


@pytest.fixture
def acc(django_user_model):
    u = django_user_model.objects.create(username="op")
    p = Proxy.objects.create(proxy_string="h:1:u:p")
    return TelegramAccount.objects.create(user=u, phone_number="+1", is_authenticated=True,
                                          proxy=p)


# ------------------------------------------------------------------ managed

@respx.mock
def test_call_ok_and_error_mapping(acc):
    m = md.ManagedAccount(acc.id)
    route = respx.post(f"{GW}/accounts/{acc.id}/get_me").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"id": 7}}))
    assert m.get_me() == {"id": 7}
    assert route.called

    def err(kind, **kw):
        return httpx.Response(200, json={"ok": False, "error": {
            "kind": kind, "message": "x", "retry_after": kw.get("ra"), "reason": kw.get("reason", "")}})

    respx.post(f"{GW}/accounts/{acc.id}/get_me").mock(return_value=err("rate_limited", ra=77))
    with pytest.raises(md.RateLimited) as ei:
        m.get_me()
    assert ei.value.retry_after == 77

    respx.post(f"{GW}/accounts/{acc.id}/get_me").mock(return_value=err("busy", ra=30))
    with pytest.raises(md.RateLimited):
        m.get_me()

    respx.post(f"{GW}/accounts/{acc.id}/get_me").mock(return_value=err("transport", ra=300))
    with pytest.raises(md.AccountUnavailable) as ei:
        m.get_me()
    assert ei.value.reason == "transport" and ei.value.retry_after == 300

    respx.post(f"{GW}/accounts/{acc.id}/get_me").mock(return_value=err("unavailable", reason="resolve"))
    with pytest.raises(md.AccountUnavailable) as ei:
        m.get_me()
    assert ei.value.reason == "resolve"

    respx.post(f"{GW}/accounts/{acc.id}/get_me").mock(return_value=err("telegram"))
    with pytest.raises(md.TelegramOpError):
        m.get_me()


@respx.mock
def test_gateway_down_is_rate_limited_30(acc):
    respx.post(f"{GW}/accounts/{acc.id}/get_me").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(md.RateLimited) as ei:
        md.ManagedAccount(acc.id).get_me()
    assert isinstance(ei.value, md.GatewayDown) and ei.value.retry_after == 30


@respx.mock
def test_fetch_history_wraps_scan(acc):
    route = respx.post(f"{GW}/accounts/{acc.id}/scan").mock(return_value=httpx.Response(200, json={
        "ok": True, "result": [{"key": "h", "max_id": 12, "n_seen": 2, "n_media": 0, "error": None,
                                "resolved": {"id": 5, "access_hash": 9},
                                "hits": [{"mid": 11, "text": "a", "date": "2026-09-19T10:00:00+00:00",
                                          "author_id": None, "term": None, "media": {"kind": "photo"}},
                                         {"mid": 12, "text": "b", "date": None, "author_id": None,
                                          "term": None, "media": None}]}]}))
    peer = {}
    out = md.ManagedAccount(acc.id).fetch_history("chan", min_id=10, limit=50, reverse=True,
                                                  peer_sink=peer)
    import json
    sent = json.loads(route.calls[0].request.content)
    assert sent["chats"][0] == {"key": "h", "entity": {"username": "chan"}, "min_id": 10,
                                "limit": 50, "reverse": True}
    assert peer == {"id": 5, "access_hash": 9}
    assert out[0]["id"] == 11 and out[0]["media_kind"] == "photo"
    assert out[0]["date"].year == 2026 and out[1]["date"] is None


@respx.mock
def test_fetch_history_chat_error_is_op_error(acc):
    respx.post(f"{GW}/accounts/{acc.id}/scan").mock(return_value=httpx.Response(200, json={
        "ok": True, "result": [{"key": "h", "hits": [], "max_id": 0, "n_seen": 0, "n_media": 0,
                                "error": "ChannelPrivateError: x", "resolved": None}]}))
    with pytest.raises(md.TelegramOpError):
        md.ManagedAccount(acc.id).fetch_history("chan")


@respx.mock
def test_check_alive_returns_dict_on_errors(acc):
    respx.post(f"{GW}/accounts/{acc.id}/check_alive").mock(return_value=httpx.Response(200, json={
        "ok": False, "error": {"kind": "unavailable", "message": "m", "retry_after": None,
                               "reason": "deauth"}}))
    res = md.ManagedAccount(acc.id).check_alive()
    assert res["ok"] is False and res["state"] == "сесію відкликано"


def test_row_reads_db_and_refreshes(acc):
    m = md.ManagedAccount(acc.id)
    assert m.state == "ready" and m.is_available and m.proxy_id == acc.proxy_id
    TelegramAccount.objects.filter(pk=acc.id).update(state="cooldown",
                                                     cooldown_until=timezone.now() + timedelta(hours=1))
    assert m.is_available                      # кеш
    m.refresh()
    assert not m.is_available


# ------------------------------------------------------------------ registry

def _mk(user, phone, **kw):
    p = Proxy.objects.create(proxy_string=f"h:{phone}:u:p")
    return TelegramAccount.objects.create(user=user, phone_number=phone, is_authenticated=True,
                                          proxy=p, **kw)


@pytest.fixture
def pool(django_user_model):
    u = django_user_model.objects.create(username="op")
    return [_mk(u, "+1"), _mk(u, "+2"), _mk(u, "+3"), _mk(u, "+4")]


def test_candidates_filters_state_proxy_and_roles(pool):
    from analysis.models import MonitorChat, PublishConfig
    from analysis.tests.factories import TaskFactory
    a1, a2, a3, a4 = pool
    a2.state = "cooldown"; a2.cooldown_until = timezone.now() + timedelta(minutes=5); a2.save()
    a3.proxy.is_working = False; a3.proxy.save()
    assert registry.candidates("collector") == [a1.id, a4.id]
    # cooldown минув — знову в пулі (стан ще cooldown: перехід зробить gateway при OK,
    # але ворота gateway пропустять, бо cooldown_until у минулому)
    a2.cooldown_until = timezone.now() - timedelta(seconds=1); a2.save()
    assert a2.id not in registry.candidates("collector")     # state != ready
    a2.state = "ready"; a2.save()
    assert registry.candidates("collector") == [a1.id, a2.id, a4.id]
    # привʼязаний до стріму — не для збирача, але для стріму годиться
    task = TaskFactory()
    from analysis.models import Channel
    ch = Channel.objects.create(username="c1", title="c1")
    MonitorChat.objects.create(task=task, channel=ch, tg_account=a1)
    assert registry.candidates("collector") == [a2.id, a4.id]
    assert a1.id in registry.candidates("stream")
    # публікатор — ні для кого
    PublishConfig.objects.create(task=task, forward_account=a4, chat_id="-1", name="p")
    assert registry.candidates("collector") == [a2.id]
    assert registry.candidates("stream") == [a1.id, a2.id]
    assert registry.candidates("publisher") == []


def test_candidates_need_resolve_and_tags(pool):
    a1, a2, a3, a4 = pool
    a1.resolve_exhausted_until = timezone.now() + timedelta(hours=1); a1.save()
    assert a1.id in registry.candidates("collector")
    assert a1.id not in registry.candidates("collector", need_resolve=True)
    t = AccountTag.objects.create(name="кампанія")
    a2.tags.add(t)
    Setting.objects.create(key="registry_roles_json",
                           value='{"collector": {"exclude_tags": ["кампанія"]}}')
    assert a2.id not in registry.candidates("collector")
    assert a2.id in registry.candidates("stream")


def test_pick_is_stable_and_rotates_with_shift(pool):
    ids = registry.candidates("collector")
    assert registry.pick("collector", key=5).id == ids[5 % 4]
    assert registry.pick("collector", key=5).id == ids[5 % 4]        # стабільно
    assert registry.pick("collector", key=5, shift=1).id == ids[6 % 4]
    TelegramAccount.objects.update(state="needs_proxy")
    with pytest.raises(registry.NoAccountAvailable):
        registry.pick("collector", key=1)


def test_pinned_for(pool):
    from analysis.tests.factories import SourceFactory
    a1 = pool[0]
    src = SourceFactory(kind="telegram", url="https://t.me/x", tg_account=a1)
    assert registry.pinned_for(src).id == a1.id
    a1.state = "deauthorized"; a1.save()
    assert registry.pinned_for(src) is None
    assert registry.pinned_for(SourceFactory()) is None
