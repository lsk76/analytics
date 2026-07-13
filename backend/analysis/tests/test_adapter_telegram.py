"""Telegram-адаптер: watermark min_id, backfill, url, handle-parse, FloodWait."""
from datetime import datetime, timezone

import pytest

from analysis.services.infospace.adapters import get_adapter, telegram
from analysis.services.infospace.adapters.base import RateLimited
from analysis.services.infospace.adapters.telegram import TelegramAdapter


class _Src:
    def __init__(self, url="https://t.me/ulan_smi", poll_cursor=None, config=None):
        self.url = url
        self.poll_cursor = poll_cursor or {}
        self.config = config or {}
        self.tg_account = None


class _Acct:
    is_authenticated = True


def _msgs(ids):
    return [{"id": i, "text": f"повідомлення {i}",
             "date": datetime(2026, 7, 8, tzinfo=timezone.utc)} for i in ids]


def _patch(monkeypatch, hist, acct=_Acct()):
    monkeypatch.setattr(TelegramAdapter, "_account", lambda self, s: acct)
    monkeypatch.setattr(telegram, "_fetch_history",
                        lambda account, handle, min_id, limit, reverse: hist)


def test_telegram_registered():
    assert isinstance(get_adapter("telegram"), TelegramAdapter)


@pytest.mark.parametrize("url,expected", [
    ("https://t.me/ulan_smi", "ulan_smi"),
    ("https://t.me/s/ulan_smi", "ulan_smi"),
    ("@ulan_smi", "ulan_smi"),
    ("ulan_smi", "ulan_smi"),
])
def test_handle_parse(url, expected):
    assert TelegramAdapter._handle(_Src(url=url)) == expected


def test_first_poll_sets_watermark_and_urls(monkeypatch):
    src = _Src()
    _patch(monkeypatch, _msgs([28919, 28920, 28921]))
    items = TelegramAdapter().fetch(src)
    assert len(items) == 3
    assert items[0].url == "https://t.me/ulan_smi/28919"
    assert items[0].text == "повідомлення 28919"
    assert items[0].posted_at.year == 2026
    assert src.poll_cursor["last_msg_id"] == 28921         # максимум id


def test_second_poll_uses_min_id_and_reverse(monkeypatch):
    src = _Src(poll_cursor={"last_msg_id": 28921})
    captured = {}

    def _fh(account, handle, min_id, limit, reverse):
        captured.update(min_id=min_id, limit=limit, reverse=reverse)
        return _msgs([28922])
    monkeypatch.setattr(TelegramAdapter, "_account", lambda self, s: _Acct())
    monkeypatch.setattr(telegram, "_fetch_history", _fh)
    items = TelegramAdapter().fetch(src)
    assert captured["min_id"] == 28921               # watermark → min_id
    assert captured["reverse"] is True               # догін суцільно (без діри)
    assert captured["limit"] == 100                  # max_items на подальших полінгах
    assert src.poll_cursor["last_msg_id"] == 28922


def test_first_poll_uses_backfill_limit_and_no_reverse(monkeypatch):
    src = _Src(config={"backfill_limit": 5})          # порожній poll_cursor → перший полінг
    captured = {}

    def _fh(account, handle, min_id, limit, reverse):
        captured.update(limit=limit, reverse=reverse, min_id=min_id)
        return _msgs([1, 2])
    monkeypatch.setattr(TelegramAdapter, "_account", lambda self, s: _Acct())
    monkeypatch.setattr(telegram, "_fetch_history", _fh)
    TelegramAdapter().fetch(src)
    assert captured["limit"] == 5                      # backfill_limit
    assert captured["reverse"] is False               # найновіші N
    assert captured["min_id"] == 0


def test_fetch_history_converts_floodwait_to_ratelimited(monkeypatch):
    """Реальна конверсія telethon FloodWaitError → RateLimited у _fetch_history."""
    from telethon.errors import FloodWaitError
    from accounts.services import telegram_client as tc

    class _FW(FloodWaitError):
        def __init__(self):            # без super().__init__ — не тягнемо RPC-payload
            self.seconds = 33

    async def _boom(*a, **k):
        raise _FW()
    monkeypatch.setattr(tc.TelegramUserClient, "fetch_history", _boom)
    with pytest.raises(RateLimited) as ei:
        telegram._fetch_history(_Acct(), "ulan_smi", 0, 5, False)
    assert ei.value.retry_after == 33


def test_empty_channel_keeps_watermark(monkeypatch):
    src = _Src(poll_cursor={"last_msg_id": 100})
    _patch(monkeypatch, [])
    assert TelegramAdapter().fetch(src) == []
    assert src.poll_cursor["last_msg_id"] == 100


def test_floodwait_propagates_as_ratelimited(monkeypatch):
    src = _Src()
    monkeypatch.setattr(TelegramAdapter, "_account", lambda self, s: _Acct())

    def _boom(account, handle, min_id, limit, reverse):
        raise RateLimited(42)
    monkeypatch.setattr(telegram, "_fetch_history", _boom)
    with pytest.raises(RateLimited) as ei:
        TelegramAdapter().fetch(src)
    assert ei.value.retry_after == 42


def test_no_account_raises(monkeypatch):
    monkeypatch.setattr(TelegramAdapter, "_account", lambda self, s: None)
    with pytest.raises(RuntimeError):
        TelegramAdapter().fetch(_Src())


@pytest.mark.django_db
def test_account_selection_prefers_source_then_pool():
    """_account: явний авторизований акаунт джерела > перший авторизований із пулу."""
    from django.contrib.auth import get_user_model
    from accounts.models import TelegramAccount
    from analysis.models import Source

    u = get_user_model().objects.create(username="tg-owner")
    a1 = TelegramAccount.objects.create(user=u, phone_number="+1", is_authenticated=True)
    a2 = TelegramAccount.objects.create(user=u, phone_number="+2", is_authenticated=True)
    unauth = TelegramAccount.objects.create(user=u, phone_number="+3", is_authenticated=False)
    ad = TelegramAdapter()

    # пул: перший авторизований (a1)
    s = Source(kind="telegram", url="https://t.me/x")
    assert ad._account(s) == a1
    # явно призначений авторизований — має пріоритет
    s.tg_account = a2
    assert ad._account(s) == a2
    # призначений НЕавторизований → фолбек на пул (a1)
    s.tg_account = unauth
    assert ad._account(s) == a1
    # немає жодного авторизованого → None
    TelegramAccount.objects.update(is_authenticated=False)
    s.tg_account = None
    assert ad._account(s) is None
