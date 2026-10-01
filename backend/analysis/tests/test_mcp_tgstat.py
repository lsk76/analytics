"""Інструменти tgstat_* у MCP-шарі Django: транспорт, стани сесії, права.

Сам сайт і браузер тут не потрібні — усе спілкування з сервісом `tgstat` іде
по HTTP (мок respx), а перевіряємо ми саме те, що ламається в бою: чи переклали
машинний стан у пораду людині й чи не падає інструмент сирим трейсбеком, коли
контейнера немає.
"""
import json
from pathlib import Path

import httpx
import pytest
import respx

from analysis.services.mcp_api import TOOLS, perms, registry
from analysis.services.mcp_api.registry import Actor, ToolError

API = "http://tgstat:8020"

TOOL_NAMES = ("tgstat_status", "tgstat_channels_search", "tgstat_catalog_tags",
              "tgstat_catalog", "tgstat_channel", "tgstat_posts_search",
              "tgstat_links", "tgstat_manual_login", "tgstat_manual_finish")


def test_tools_registered_with_perms():
    for name in TOOL_NAMES:
        assert name in TOOLS, f"інструмент {name} не зареєстровано"
        assert perms.required(name), f"{name} без права в perms.PERMS"
    # ручний вхід спиняє ВСІ запити до tgstat — він мусить бути адмінським і змінним
    for name in ("tgstat_manual_login", "tgstat_manual_finish"):
        assert TOOLS[name].mutates
        assert TOOLS[name].scope == registry.SCOPE_ADMIN


@respx.mock
def test_status_usable():
    respx.get(f"{API}/health").mock(return_value=httpx.Response(
        200, json={"ok": True, "browser": True, "manual": False}))
    respx.get(f"{API}/auth/status").mock(return_value=httpx.Response(
        200, json={"state": "ok", "user": "ivan", "plan": "Premium",
                   "usable": True, "checked_at": "2026-10-01T07:00:00+00:00"}))
    out = TOOLS["tgstat_status"]({})
    assert "ivan (Premium)" in out and "що робити" not in out


@respx.mock
def test_status_manual_login_tells_what_to_do():
    respx.get(f"{API}/health").mock(return_value=httpx.Response(
        200, json={"ok": True, "browser": False, "manual": True}))
    respx.get(f"{API}/auth/status").mock(return_value=httpx.Response(
        200, json={"state": "manual", "usable": False, "detail": "іде ручний вхід"}))
    out = TOOLS["tgstat_status"]({})
    assert "іде ручний вхід" in out and "tgstat_manual_finish" in out


@respx.mock
def test_captcha_becomes_advice_not_traceback():
    respx.get(f"{API}/channels/search").mock(return_value=httpx.Response(
        503, json={"state": "captcha", "error": "Подозрение на робота"}))
    with pytest.raises(ToolError) as e:
        TOOLS["tgstat_channels_search"]({"q": "Бурятия"})
    assert "капч" in str(e.value) and "tgstat_manual_login" in str(e.value)


@respx.mock
def test_service_down_names_the_container():
    respx.get(f"{API}/catalog/tags").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(ToolError) as e:
        TOOLS["tgstat_catalog_tags"]({})
    assert "не відповідає" in str(e.value) and "tgstat" in str(e.value)


@respx.mock
def test_channels_search_passes_filters_and_renders():
    route = respx.get(f"{API}/channels/search").mock(return_value=httpx.Response(
        200, json={"count": 1, "pages": 1, "has_more": False, "items": [
            {"title": "Ариг Ус", "ref": "@arigus", "subscribers": 42000,
             "avg_post_reach": 9000, "ci": 55, "category": "Новости и СМИ",
             "description": "Бурятия", "tgstat_url": "https://tgstat.ru/channel/@arigus",
             "tme_url": "https://t.me/arigus"}]}))
    out = TOOLS["tgstat_channels_search"]({"q": "Бурятия", "in_about": True,
                                           "min_subs": 1000, "max_pages": 1})
    q = route.calls[0].request.url.params
    assert q["in_about"] == "true" and q["min_subs"] == "1000" and q["max_pages"] == "1"
    # порожні фільтри не їдуть у сервіс — інакше він шукає «категорія=''»
    assert "category" not in q
    assert "42 000 підп." in out and "@arigus" in out


@respx.mock
def test_catalog_chat_kind_and_empty_result():
    respx.get(f"{API}/catalog/buratia-region").mock(return_value=httpx.Response(
        200, json={"count": 0, "pages": 2, "has_more": False, "items": [],
                   "tgstat_url": "https://tgstat.ru/chat/buratia-region"}))
    out = TOOLS["tgstat_catalog"]({"tag": "buratia-region", "kind": "chat"})
    assert "нічого не знайдено" in out and "сторінок 2" in out


@respx.mock
def test_channel_card_renders_stats():
    respx.get(f"{API}/channel/@arigus").mock(return_value=httpx.Response(
        200, json={"title": "Ариг Ус", "ref": "@arigus", "verified": True,
                   "category": "Новости", "geo_lang": "Россия / Русский",
                   "rkn_registered": True, "stats": {
                       "subscribers": {"value": 42000, "details": {"за місяць": 300}},
                       "age": {"value": "7 років"}},
                   "tgstat_stat_url": "https://tgstat.ru/channel/@arigus/stat"}))
    out = TOOLS["tgstat_channel"]({"ref": "@arigus"})
    assert "підписники: 42 000 (за місяць 300)" in out
    assert "вік: 7 років" in out and "РКН" in out


@respx.mock
def test_links_needs_no_tgstat_request():
    respx.get(f"{API}/links/@arigus").mock(return_value=httpx.Response(
        200, json={"tgstat": "https://tgstat.ru/channel/@arigus", "tme": "", "post": None}))
    out = TOOLS["tgstat_links"]({"ref": "@arigus"})
    assert out == "tgstat: https://tgstat.ru/channel/@arigus"


@respx.mock
def test_manual_login_blocked_for_reader(django_user_model):
    respx.post(f"{API}/auth/manual").mock(return_value=httpx.Response(200, json={"ok": True}))
    reader = django_user_model.objects.create(username="reader")
    who = Actor(user=reader, scopes=[registry.SCOPE_READ], role="reader")
    with pytest.raises(ToolError) as e:
        registry.call("tgstat_manual_login", {}, who=who)
    assert "mcp:admin" in str(e.value)


@pytest.mark.django_db
def test_posts_search_visible_to_reader_scope(django_user_model):
    """Читання tgstat — звичайний mcp:read; відмова приходить із прав Django."""
    reader = django_user_model.objects.create(username="r2")
    who = Actor(user=reader, scopes=[registry.SCOPE_READ], role="reader")
    with pytest.raises(ToolError) as e:
        registry.call("tgstat_posts_search", {"q": "x"}, who=who)
    assert "права в адмінці" in str(e.value)


@respx.mock
def test_connect_timeout_is_short_not_the_read_one():
    """Довгий тайм-аут — на ЧИТАННЯ; на з'єднання 5 с, інакше виклик висить 10 хв."""
    captured = {}

    def grab(request):
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"count": 0, "items": []})

    respx.get(f"{API}/catalog/tags").mock(side_effect=grab)
    TOOLS["tgstat_catalog_tags"]({})
    assert captured["timeout"]["connect"] == 5.0
    assert captured["timeout"]["read"] > 60


# --- контракт із живим сервісом ------------------------------------------------
# Мок бреше ровно настільки, наскільки вигадана його відповідь. Нижче відповіді
# НЕ вигадані: це знімки справжніх маршрутів і парсерів сервісу tgstat, зроблені
# `tgstat_service/tests/dump_api_fixtures.py` (там же — як перегенерувати).
# Якщо формат API зміниться, а інструменти — ні, впадуть саме ці тести.

GOLDEN = Path(__file__).parent / "fixtures" / "tgstat_api"


def golden(name: str) -> dict:
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


def serve(*names):
    """Підняти на моку ті самі URL, що віддав справжній застосунок."""
    for name in names:
        g = golden(name)
        respx.get(f"{API}{g['url']}").mock(
            return_value=httpx.Response(g["status"], json=g["body"]))


@respx.mock
def test_golden_status_renders_real_session():
    serve("health", "auth_status")
    out = TOOLS["tgstat_status"]({})
    assert "ivan (Premium)" in out and "браузер" in out


@respx.mock
def test_golden_channel_card_renders_real_stats():
    serve("channel_card")
    out = TOOLS["tgstat_channel"]({"ref": "@rian_ru"})
    assert "РИА Новости" in out and "@rian_ru ✔" in out
    assert "підписники: 3 039 296" in out          # з приростами в дужках
    assert "індекс цитування: 20 200" in out
    assert "Новости и СМИ" in out and "РКН" in out
    assert "https://tgstat.ru/channel/@rian_ru/stat" in out


@respx.mock
def test_golden_channels_search_renders_every_card():
    serve("channels_search")
    g = golden("channels_search")
    out = TOOLS["tgstat_channels_search"]({"q": "Бурятия", "max_pages": 1})
    assert f"Канали за «Бурятия»: {g['body']['count']}" in out
    first = g["body"]["items"][0]
    assert first["ref"] in out and first["title"] in out
    # кожен знайдений канал має опинитись у відповіді, а не лише перші рядки
    assert all(item["ref"] in out for item in g["body"]["items"])


@respx.mock
def test_golden_catalog_chats_and_tags():
    serve("catalog_tags", "catalog_chats")
    tags = TOOLS["tgstat_catalog_tags"]({"kind": "geo"})
    assert "buratia-region — Бурятия" in tags

    out = TOOLS["tgstat_catalog"]({"tag": "buratia-region", "kind": "chat",
                                   "max_pages": 1})
    body = golden("catalog_chats")["body"]
    assert out.startswith(f"Чати підбірки buratia-region ({body['tgstat_url']})")
    assert body["items"][0]["ref"] in out


@respx.mock
def test_golden_posts_search_found_and_empty():
    serve("posts_search")
    body = golden("posts_search")["body"]
    out = TOOLS["tgstat_posts_search"]({"q": "Ds", "max_pages": 1})
    assert f"знайдено {body['total']}" in out and "показано" in out
    assert body["items"][0]["ref"] in out
    assert body["items"][0]["tme_post_url"] in out

    respx.get(f"{API}/posts/search").mock(return_value=httpx.Response(
        200, json=golden("posts_not_found")["body"]))
    empty = TOOLS["tgstat_posts_search"]({"q": "абракадабра"})
    assert "знайдено 0" in empty and "\n1." not in empty


@respx.mock
def test_golden_links_pass_through():
    serve("links")
    out = TOOLS["tgstat_links"]({"ref": "@rian_ru", "post_id": 5})
    for value in golden("links")["body"].values():
        assert value in out
