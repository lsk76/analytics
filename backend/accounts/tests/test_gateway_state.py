"""Стейт-машина акаунта (accounts/gateway/state.py) — по рядку на кожен перехід
таблиці docs/tg-gateway-plan.md §4."""
import asyncio
from datetime import timedelta

import pytest
from django.utils import timezone
from telethon.errors import (AuthKeyDuplicatedError, AuthKeyUnregisteredError,
                             ChatAdminRequiredError, FloodWaitError,
                             PhoneNumberBannedError, SessionRevokedError,
                             UserDeactivatedBanError, UserDeactivatedError,
                             UsernameInvalidError, UsernameNotOccupiedError)

from accounts.gateway import state as st
from accounts.models import Proxy, TelegramAccount
from analysis.models import Setting

pytestmark = pytest.mark.django_db


@pytest.fixture
def acc(django_user_model):
    u = django_user_model.objects.create(username="op")
    p = Proxy.objects.create(proxy_string="h:1:u:p", last_tested_at=timezone.now())
    return TelegramAccount.objects.create(user=u, phone_number="+1", is_authenticated=True,
                                          proxy=p)


# ------------------------------------------------------------------ classify

@pytest.mark.parametrize("exc,expected", [
    (None, st.Outcome.OK),
    (ConnectionError("Connection to Telegram failed 5 time(s)"), st.Outcome.TRANSPORT),
    (TimeoutError("fetch перевищив 120s — джерело підвисло"), st.Outcome.TRANSPORT),
    (asyncio.TimeoutError(), st.Outcome.TRANSPORT),
    (OSError("Connection refused by destination host"), st.Outcome.TRANSPORT),
    (RuntimeError("Server closed the connection: 0 bytes"), st.Outcome.TRANSPORT),
    (FloodWaitError(request=None, capture=42), st.Outcome.FLOOD),
    (UsernameInvalidError(request=None), st.Outcome.RESOLVE),
    (UsernameNotOccupiedError(request=None), st.Outcome.RESOLVE),
    (ValueError('No user has "kol49" as username'), st.Outcome.RESOLVE),
    (ValueError("Cannot find any entity corresponding to x"), st.Outcome.RESOLVE),
    (AuthKeyUnregisteredError(request=None), st.Outcome.DEAUTH),
    (SessionRevokedError(request=None), st.Outcome.DEAUTH),
    (AuthKeyDuplicatedError(request=None), st.Outcome.DEAUTH),
    (UserDeactivatedError(request=None), st.Outcome.DEAUTH),
    (UserDeactivatedBanError(request=None), st.Outcome.BANNED),
    (PhoneNumberBannedError(request=None), st.Outcome.BANNED),
    (ChatAdminRequiredError(request=None), st.Outcome.TELEGRAM),
    (ValueError("щось інше"), st.Outcome.INTERNAL),
    (KeyError("x"), st.Outcome.INTERNAL),
])
def test_classify(exc, expected):
    assert st.classify(exc)[0] is expected


def test_classify_flood_seconds():
    assert st.classify(FloodWaitError(request=None, capture=42))[1] == {"seconds": 42}


# ------------------------------------------------------------------ apply

def test_ok_resets_counters(acc):
    acc.transport_failures = 2
    acc.state = TelegramAccount.STATE_COOLDOWN
    acc.cooldown_until = timezone.now() + timedelta(hours=1)
    acc.last_error = "x"
    acc.save()
    st.apply(acc, st.Outcome.OK)
    acc.refresh_from_db()
    assert acc.state == "ready" and acc.transport_failures == 0
    assert acc.cooldown_until is None and acc.last_error == ""
    assert acc.last_ok_at is not None and acc.is_available


def test_transport_cooldown_grows_and_flags_proxy(acc):
    now = timezone.now()
    meta = {}
    st.apply(acc, st.Outcome.TRANSPORT, meta=meta, error="refused", now=now)
    acc.refresh_from_db(); acc.proxy.refresh_from_db()
    assert acc.state == "cooldown" and acc.transport_failures == 1
    assert acc.cooldown_until == now + timedelta(seconds=300)
    assert not acc.is_available
    assert acc.proxy.fail_count == 1 and acc.proxy.last_tested_at is None
    assert acc.proxy.is_working is True          # без проксі не працюємо → не знімаємо
    assert meta["needs_repair"] is False

    st.apply(acc, st.Outcome.TRANSPORT, now=now)
    acc.refresh_from_db()
    assert acc.cooldown_until == now + timedelta(seconds=600)

    meta = {}
    st.apply(acc, st.Outcome.TRANSPORT, meta=meta, now=now)
    acc.refresh_from_db()
    assert acc.transport_failures == 3 and acc.cooldown_until == now + timedelta(seconds=1200)
    assert meta["needs_repair"] is True


def test_transport_cooldown_capped_and_settings(acc):
    Setting.objects.create(key="gateway_cooldown_base_sec", value="100")
    Setting.objects.create(key="gateway_cooldown_cap_sec", value="250")
    Setting.objects.create(key="gateway_repair_after", value="2")
    now = timezone.now()
    st.apply(acc, st.Outcome.TRANSPORT, now=now)
    meta = {}
    st.apply(acc, st.Outcome.TRANSPORT, meta=meta, now=now)
    acc.refresh_from_db()
    assert acc.cooldown_until == now + timedelta(seconds=200)
    assert meta["needs_repair"] is True
    st.apply(acc, st.Outcome.TRANSPORT, now=now)
    acc.refresh_from_db()
    assert acc.cooldown_until == now + timedelta(seconds=250)   # кап


def test_flood_pauses_exact_seconds(acc):
    now = timezone.now()
    st.apply(acc, st.Outcome.FLOOD, meta={"seconds": 900}, now=now)
    acc.refresh_from_db()
    assert acc.state == "cooldown" and acc.cooldown_until == now + timedelta(seconds=900)
    assert acc.transport_failures == 0           # це не транспорт
    acc.proxy.refresh_from_db()
    assert acc.proxy.fail_count == 0


def test_resolve_exhausted_keeps_account_ready_and_backs_off(acc):
    now = timezone.now()
    st.apply(acc, st.Outcome.RESOLVE, error='No user has "x" as username', now=now)
    acc.refresh_from_db()
    assert acc.state == "ready" and acc.is_available
    assert not acc.can_resolve() and acc.resolve_failures == 1
    assert acc.resolve_exhausted_until == now + timedelta(hours=6)
    st.apply(acc, st.Outcome.RESOLVE, now=now)
    acc.refresh_from_db()
    assert acc.resolve_exhausted_until == now + timedelta(hours=12)
    st.apply(acc, st.Outcome.RESOLVE, now=now)
    st.apply(acc, st.Outcome.RESOLVE, now=now)
    acc.refresh_from_db()
    assert acc.resolve_exhausted_until == now + timedelta(hours=24)    # кап
    st.apply(acc, st.Outcome.OK, now=now)                               # ok БЕЗ резолву
    acc.refresh_from_db()
    assert acc.resolve_failures == 4 and not acc.can_resolve()
    st.apply(acc, st.Outcome.OK, meta={"resolved": True}, now=now)     # резолв удався
    acc.refresh_from_db()
    assert acc.resolve_failures == 0 and acc.can_resolve()


def test_deauth_and_banned_drop_authentication(acc):
    acc.gateway_connected = True
    acc.save()
    st.apply(acc, st.Outcome.DEAUTH, error="AuthKeyDuplicated")
    acc.refresh_from_db()
    assert acc.state == "deauthorized" and acc.is_authenticated is False
    assert acc.gateway_connected is False and not acc.is_available

    acc2 = TelegramAccount.objects.create(user=acc.user, phone_number="+2",
                                          is_authenticated=True)
    st.apply(acc2, st.Outcome.BANNED)
    acc2.refresh_from_db()
    assert acc2.state == "banned" and acc2.is_authenticated is False


def test_telegram_and_internal_only_record_error(acc):
    for out in (st.Outcome.TELEGRAM, st.Outcome.INTERNAL):
        st.apply(acc, out, error="boom")
        acc.refresh_from_db()
        assert acc.state == "ready" and acc.last_error == "boom" and acc.is_available


def test_repair_failed_then_repaired(acc):
    st.mark_repair_failed(acc)
    acc.refresh_from_db(); acc.proxy.refresh_from_db()
    assert acc.state == "needs_proxy" and not acc.is_available
    assert acc.proxy.is_working is False
    st.mark_repaired(acc)
    acc.refresh_from_db()
    assert acc.state == "ready" and acc.is_available


def test_is_available_after_cooldown_passes(acc):
    acc.state = TelegramAccount.STATE_COOLDOWN
    acc.cooldown_until = timezone.now() - timedelta(seconds=1)
    assert not acc.is_available                  # стан ще cooldown: перехід робить gateway
    acc.state = TelegramAccount.STATE_READY
    assert acc.is_available


def test_apply_returns_changed_fields_and_noop_saves_nothing(acc):
    assert "last_ok_at" in st.apply(acc, st.Outcome.OK)
    assert st.apply(acc, st.Outcome.INTERNAL, error="") == []
