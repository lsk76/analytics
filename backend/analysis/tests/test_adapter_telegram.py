"""Telegram-адаптер infospace поверх registry/ManagedAccount (gateway замокано)."""
import pytest
from django.utils import timezone

from accounts.models import Proxy, TelegramAccount
from accounts.services import registry
from accounts.services.managed import AccountUnavailable, ManagedAccount
from accounts.services.managed import RateLimited as GwRateLimited
from accounts.services.managed import TelegramOpError
from analysis.models import Channel, Source
from analysis.services.infospace.adapters import telegram
from analysis.services.infospace.adapters.base import RateLimited
from analysis.services.infospace.adapters.telegram import TelegramAdapter

pytestmark = pytest.mark.django_db


@pytest.fixture
def accounts(django_user_model):
    u = django_user_model.objects.create(username="tg-owner")
    out = []
    for i in (1, 2):
        p = Proxy.objects.create(proxy_string=f"h:{i}:u:p")
        out.append(TelegramAccount.objects.create(user=u, phone_number=f"+{i}",
                                                  is_authenticated=True, proxy=p))
    TelegramAccount.objects.create(user=u, phone_number="+3", is_authenticated=False)
    return out


class _FakeManaged(ManagedAccount):
    """fetch_history без HTTP: результат/виняток задає тест."""
    msgs, exc, calls = [], None, []

    def fetch_history(self, handle, min_id=0, limit=50, reverse=False, peer_sink=None):
        _FakeManaged.calls.append(dict(id=self.id, handle=handle, min_id=min_id,
                                       limit=limit, reverse=reverse))
        if _FakeManaged.exc:
            raise _FakeManaged.exc
        if peer_sink is not None:
            peer_sink.update({"id": 555, "access_hash": 777})
        return list(_FakeManaged.msgs)


@pytest.fixture
def fake(monkeypatch):
    _FakeManaged.msgs, _FakeManaged.exc, _FakeManaged.calls = [], None, []
    monkeypatch.setattr(registry, "ManagedAccount", _FakeManaged)
    return _FakeManaged


def test_handle_parsing():
    ad = TelegramAdapter()
    for url, exp in [("https://t.me/ulan_smi", "ulan_smi"), ("https://t.me/s/ulan_smi", "ulan_smi"),
                     ("@ulan_smi", "ulan_smi"), ("https://t.me/ulan_smi/123", "ulan_smi")]:
        assert ad._handle(Source(url=url)) == exp


def test_account_selection_prefers_pinned_then_pool(accounts, fake):
    a1, a2 = accounts
    ad = TelegramAdapter()
    s = Source(kind="telegram", url="https://t.me/x", id=10)
    assert ad._account(s).id in (a1.id, a2.id)
    assert ad._account(s).id == ad._account(s).id                  # стабільно
    s.poll_cursor = {"acc_shift": 1}
    assert ad._account(s).id != ad._account(Source(kind="telegram", url="u", id=10)).id
    s.tg_account = a2
    assert ad._account(s).id == a2.id                               # pinned
    a2.state = "deauthorized"; a2.save()
    s.tg_account = a2
    assert ad._account(s).id == a1.id                               # pinned мертвий → пул


def test_account_without_resolve_is_skipped_unless_hash_cached(accounts, fake):
    from datetime import timedelta
    a1, a2 = accounts
    ch = Channel.objects.create(username="x", title="x", tg_id=1)
    s = Source.objects.create(kind="telegram", url="https://t.me/x", name="x")
    ad = TelegramAdapter()
    first = ad._account(s, ch)
    first_row = TelegramAccount.objects.get(pk=first.id)
    first_row.resolve_exhausted_until = timezone.now() + timedelta(hours=1)
    first_row.save()
    other = a2 if first.id == a1.id else a1
    assert ad._account(s, ch).id == other.id                       # без хеша → хто резолвить
    ch.raw_meta = {"access_hash_by_acc": {str(first.id): 9}}
    ch.save()
    assert ad._account(s, ch).id == first.id                       # є хеш → резолв не потрібен
    TelegramAccount.objects.update(resolve_exhausted_until=timezone.now() + timedelta(hours=1))
    ch.raw_meta = {}
    assert ad._account(s, ch) is None                              # ніхто не резолвить → чекати


def test_no_accounts_is_rate_limited_not_failure(fake):
    s = Source.objects.create(kind="telegram", url="https://t.me/x", name="x")
    with pytest.raises(RateLimited):
        TelegramAdapter().fetch(s)


def test_fetch_first_poll_then_watermark(accounts, fake):
    now = timezone.now()
    fake.msgs = [{"id": 5, "text": "a", "date": now, "media_kind": "photo"},
                 {"id": 7, "text": "b", "date": now, "media_kind": None}]
    s = Source.objects.create(kind="telegram", url="https://t.me/ulan_smi", name="u")
    items = TelegramAdapter().fetch(s)
    assert [i.external_id for i in items] == ["5", "7"]
    assert items[0].meta["media"] == {"kind": "photo", "chat": "ulan_smi", "mid": 5}
    assert items[0].url == "https://t.me/ulan_smi/5"
    assert s.poll_cursor["last_msg_id"] == 7
    assert fake.calls[-1]["reverse"] is False                      # backfill: найновіші
    TelegramAdapter().fetch(s)
    assert fake.calls[-1]["min_id"] == 7 and fake.calls[-1]["reverse"] is True


def test_fetch_remembers_peer_per_account_and_reuses_it(accounts, fake):
    Channel.objects.create(username="ulan_smi", title="u")
    s = Source.objects.create(kind="telegram", url="https://t.me/ulan_smi", name="u")
    TelegramAdapter().fetch(s)
    ch = Channel.objects.get(username="ulan_smi")
    acc_id = fake.calls[-1]["id"]
    assert fake.calls[-1]["handle"] == "ulan_smi"                   # перший раз — резолв
    assert ch.raw_meta["access_hash_by_acc"] == {str(acc_id): 777} and ch.tg_id == 555
    TelegramAdapter().fetch(s)
    assert fake.calls[-1]["handle"] == {"channel_id": 555, "access_hash": 777}  # далі — без
    # інший акаунт хеш не бере: він персональний
    s.tg_account = accounts[1] if accounts[0].id == acc_id else accounts[0]
    s.save()
    TelegramAdapter().fetch(s)
    assert fake.calls[-1]["handle"] == "ulan_smi"


def test_fetch_creates_channel_row_for_cache(accounts, fake):
    s = Source.objects.create(kind="telegram", url="https://t.me/newchan", name="Новий")
    assert not Channel.objects.filter(username="newchan").exists()
    TelegramAdapter().fetch(s)
    ch = Channel.objects.get(username="newchan")
    assert ch.tg_id == 555 and ch.title == "Новий" and str(fake.calls[-1]["id"]) in ch.raw_meta["access_hash_by_acc"]
    fake.exc = TelegramOpError("ChannelPrivateError")
    s2 = Source.objects.create(kind="telegram", url="https://t.me/deadchan", name="d")
    with pytest.raises(TelegramOpError):
        TelegramAdapter().fetch(s2)
    assert not Channel.objects.filter(username="deadchan").exists()     # мертвий — не плодимо


def test_account_unavailable_rotates_and_rate_limits(accounts, fake):
    fake.exc = AccountUnavailable("transport", "proxy dead", retry_after=300)
    s = Source.objects.create(kind="telegram", url="https://t.me/x", name="x")
    with pytest.raises(RateLimited) as ei:
        TelegramAdapter().fetch(s)
    assert ei.value.retry_after == 300
    s.refresh_from_db()
    assert s.poll_cursor["acc_shift"] == 1                          # інший акаунт
    assert s.consecutive_failures == 0                              # не збій джерела


def test_pinned_account_unavailable_keeps_binding(accounts, fake):
    fake.exc = AccountUnavailable("resolve")
    s = Source.objects.create(kind="telegram", url="https://t.me/x", name="x",
                              tg_account=accounts[0])
    with pytest.raises(RateLimited):
        TelegramAdapter().fetch(s)
    s.refresh_from_db()
    assert "acc_shift" not in (s.poll_cursor or {}) and s.tg_account_id == accounts[0].id


def test_gateway_rate_limited_passes_seconds(accounts, fake):
    fake.exc = GwRateLimited(77)
    s = Source.objects.create(kind="telegram", url="https://t.me/x", name="x")
    with pytest.raises(RateLimited) as ei:
        TelegramAdapter().fetch(s)
    assert ei.value.retry_after == 77


def test_stale_cached_hash_is_forgotten_and_retried(accounts, fake):
    s = Source.objects.create(kind="telegram", url="https://t.me/x", name="x",
                              tg_account=accounts[0])
    ch = Channel.objects.create(username="x", title="x", tg_id=1,
                                raw_meta={"access_hash_by_acc": {str(accounts[0].id): 5}})
    fake.exc = TelegramOpError("ChannelInvalidError: Invalid channel object")
    with pytest.raises(RateLimited):                                # не збій джерела
        TelegramAdapter().fetch(s)
    assert fake.calls[-1]["handle"] == {"channel_id": 1, "access_hash": 5}
    ch.refresh_from_db()
    assert ch.raw_meta["access_hash_by_acc"] == {}                  # хеш забуто
    fake.exc = None
    TelegramAdapter().fetch(s)
    assert fake.calls[-1]["handle"] == "x"                          # далі — за юзернеймом


def test_chat_error_is_source_failure(accounts, fake):
    fake.exc = TelegramOpError("ChannelPrivateError: x")
    s = Source.objects.create(kind="telegram", url="https://t.me/x", name="x")
    with pytest.raises(TelegramOpError):
        TelegramAdapter().fetch(s)
