"""Інструменти vk_* у MCP-шарі: реєстрація, права, переклад помилок, рендер.

Мережі немає — підмінений `analysis.services.vk`. Перевіряємо те, що ламається
в бою: чи сказав інструмент людині, ЩО робити (немає токена, темп, закрита
спільнота), і чи читається відповідь без сирого JSON.
"""
from unittest import mock

import pytest

from analysis.services import vk
from analysis.services.mcp_api import TOOLS, perms
from analysis.services.mcp_api.registry import ToolError

TOOL_NAMES = ("vk_status", "vk_find", "vk_groups", "vk_wall", "vk_comments")


def test_tools_registered_with_perms_and_readonly():
    for name in TOOL_NAMES:
        assert name in TOOLS, f"інструмент {name} не зареєстровано"
        assert perms.required(name), f"{name} без права в perms.PERMS"
        assert not TOOLS[name].mutates, f"{name} не має писати в БД"


def test_status_without_token_tells_what_to_do(settings):
    settings.VK_API_TOKEN = ""
    out = TOOLS["vk_status"]({})
    assert "НЕМАЄ" in out and "vk_api_token" in out


def test_status_with_token_names_the_account(settings):
    settings.VK_API_TOKEN = "x"
    with mock.patch.object(vk, "me", return_value={"id": 1, "first_name": "Іван",
                                                   "last_name": "П", "screen_name": "ivan"}):
        out = TOOLS["vk_status"]({})
    assert "Іван П" in out and "@ivan" in out


def test_find_renders_posts_and_sources(settings):
    settings.VK_API_TOKEN = "x"
    res = {"total_count": 2, "items": [
        {"owner_id": -42, "id": 7, "date": 1759000000, "text": "влада мовчить",
         "views": {"count": 1234}}],
        "groups": [{"id": 42, "name": "Новини"}], "profiles": []}
    with mock.patch.object(vk, "newsfeed_search", return_value=res):
        out = TOOLS["vk_find"]({"q": "влада", "days": 3})
    assert "Новини" in out and "https://vk.com/wall-42_7" in out and "влада мовчить" in out


def test_find_without_token_is_a_clear_refusal(settings):
    settings.VK_API_TOKEN = ""
    with mock.patch.object(vk, "newsfeed_search", side_effect=vk.VkNotConfigured("нема")):
        with pytest.raises(ToolError, match="vk_api_token"):
            TOOLS["vk_find"]({"q": "влада"})


def test_rate_limit_becomes_advice(settings):
    settings.VK_API_TOKEN = "x"
    with mock.patch.object(vk, "newsfeed_search",
                           side_effect=vk.VkRateLimited("часто", retry_after=9)):
        with pytest.raises(ToolError, match="9с"):
            TOOLS["vk_find"]({"q": "влада"})


def test_bad_date_is_explained(settings):
    settings.VK_API_TOKEN = "x"
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        TOOLS["vk_find"]({"q": "влада", "date_from": "01.09.2026"})


def test_groups_search_table():
    items = [{"id": 42, "name": "Новини Якутії", "members_count": 1000,
              "screen_name": "sakhanews", "is_closed": 0}]
    with mock.patch.object(vk, "groups_search", return_value={"count": 1, "items": items}):
        out = TOOLS["vk_groups"]({"q": "Якутія"})
    assert "Новини Якутії" in out and "https://vk.com/sakhanews" in out and "відкрита" in out


def test_groups_needs_an_argument():
    with pytest.raises(ToolError, match="q .*refs|refs"):
        TOOLS["vk_groups"]({})


def test_wall_search_mode():
    with mock.patch.object(vk, "resolve_owner", return_value=(-42, "group")), \
            mock.patch.object(vk, "wall_search", return_value={"count": 1, "items": [
                {"owner_id": -42, "id": 7, "date": 1759000000, "text": "про дороги"}]}):
        out = TOOLS["vk_wall"]({"group": "https://vk.com/club42", "q": "дороги"})
    assert "про дороги" in out and "пошук «дороги»" in out


def test_comments_parses_post_link():
    with mock.patch.object(vk, "all_comments", return_value=(
            [{"id": 1, "date": 1759000000, "text": "ганьба", "from_id": 7}],
            {7: {"first_name": "Іван", "last_name": "П"}})) as c:
        out = TOOLS["vk_comments"]({"post": "https://vk.com/wall-42_7"})
    assert c.call_args.args[:2] == (-42, 7)
    assert "ганьба" in out and "Іван П" in out


def test_comments_rejects_garbage_link():
    with pytest.raises(ToolError, match="wall-123_456"):
        TOOLS["vk_comments"]({"post": "https://vk.com/club42"})


def test_closed_community_is_explained():
    with mock.patch.object(vk, "resolve_owner", return_value=(-42, "group")), \
            mock.patch.object(vk, "wall_posts_between",
                              side_effect=vk.VkAccessDenied("Access denied", 15)):
        with pytest.raises(ToolError, match="Закрита спільнота"):
            TOOLS["vk_wall"]({"group": "club42"})
