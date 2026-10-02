"""Клієнт VK: конфіг (Setting > env), розбір помилок, вікно стіни, дрібні хелпери.

Мережі тут немає: `vk.call` підмінений — перевіряємо НАШУ логіку, а не VK.
"""
from datetime import datetime, timezone
from unittest import mock

import pytest

from analysis.models import Setting
from analysis.services import vk


@pytest.mark.django_db
def test_setting_beats_env(settings):
    settings.VK_API_TOKEN = "з-оточення"
    assert vk.token() == "з-оточення"
    Setting.objects.create(key="vk_api_token", value="з-адмінки")
    assert vk.token() == "з-адмінки"


def test_call_without_token_says_what_to_do(settings):
    settings.VK_API_TOKEN = ""
    with pytest.raises(vk.VkNotConfigured) as e:
        vk.call("users.get")
    assert "vk_api_token" in str(e.value)


def _resp(payload):
    r = mock.Mock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def _post(payload):
    return mock.patch("httpx.post", return_value=_resp(payload))


def test_auth_error_is_not_retried(settings, monkeypatch):
    settings.VK_API_TOKEN = "x"
    monkeypatch.setattr(vk, "_min_interval", lambda: 0)
    with _post({"error": {"error_code": vk.ERR_AUTH, "error_msg": "User authorization failed"}}) as p:
        with pytest.raises(vk.VkError) as e:
            vk.call("users.get")
    assert p.call_count == 1          # токен не полагодиться від повтору
    assert "vk_api_token" in str(e.value)


def test_rate_limit_is_retried_then_raised(settings, monkeypatch):
    settings.VK_API_TOKEN = "x"
    monkeypatch.setattr(vk, "_min_interval", lambda: 0)
    monkeypatch.setattr(vk.time, "sleep", lambda *_: None)
    with _post({"error": {"error_code": vk.ERR_TOO_MANY, "error_msg": "Too many requests"}}) as p:
        with pytest.raises(vk.VkRateLimited):
            vk.call("wall.get")
    assert p.call_count == vk.MAX_RETRIES


def test_access_denied_has_its_own_type(settings, monkeypatch):
    settings.VK_API_TOKEN = "x"
    monkeypatch.setattr(vk, "_min_interval", lambda: 0)
    with _post({"error": {"error_code": vk.ERR_WALL_ACCESS, "error_msg": "Access denied"}}):
        with pytest.raises(vk.VkAccessDenied):
            vk.call("wall.getComments")


def test_resolve_owner_shortcuts_without_network():
    """club/public/id розбираються локально — зайвий запит до VK ні до чого."""
    with mock.patch.object(vk, "call", side_effect=AssertionError("не має ходити в VK")):
        assert vk.resolve_owner("https://vk.com/club123") == (-123, "group")
        assert vk.resolve_owner("public77") == (-77, "group")
        assert vk.resolve_owner("@id42") == (42, "user")


def test_resolve_owner_group_is_negative():
    with mock.patch.object(vk, "call", return_value={"type": "group", "object_id": 5}):
        assert vk.resolve_owner("https://vk.com/sakha_news") == (-5, "group")


def test_post_text_takes_repost_body():
    item = {"text": "", "copy_history": [{"text": "текст репоста"}]}
    assert vk.post_text(item) == "текст репоста"


def test_links():
    assert vk.post_url(-1, 2) == "https://vk.com/wall-1_2"
    assert vk.comment_url(-1, 2, 3) == "https://vk.com/wall-1_2?reply=3"


def _item(pid, ts, pinned=False):
    return {"id": pid, "date": ts, "text": f"пост {pid}", **({"is_pinned": 1} if pinned else {})}


def test_wall_window_skips_pinned_and_stops_before_window(monkeypatch):
    since = datetime(2026, 9, 10, tzinfo=timezone.utc)
    until = datetime(2026, 9, 12, 23, 59, tzinfo=timezone.utc)
    page = [_item(10, int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()), pinned=True),
            _item(9, int(datetime(2026, 9, 13, tzinfo=timezone.utc).timestamp())),   # пізніше вікна
            _item(8, int(datetime(2026, 9, 11, tzinfo=timezone.utc).timestamp())),   # у вікні
            _item(7, int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()))]    # раніше вікна
    monkeypatch.setattr(vk, "wall_get", lambda *a, **kw: {"items": page})
    got = vk.wall_posts_between(-1, since, until)
    assert [p["id"] for p in got] == [8]
