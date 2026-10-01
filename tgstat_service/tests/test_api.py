"""Інтеграційні тести HTTP-API сервісу tgstat: справжній aiohttp-застосунок,
справжні маршрути, парсери й мідлвар помилок — підставлений лише браузер.

Навіщо окремо від `test_ops.py`: там перевіряється робота з розміткою tgstat, а
тут — КОНТРАКТ, яким користуються MCP-інструменти (обидва: `tgstat_*` у
`tg-analytics` і окремий stdio-сервер). Саме в цьому шарі ламається те, що
дорого ламати в бою: назви й дефолти query-параметрів, коди станів сесії
(`captcha`/`login_required` → 503, ручний вхід → 409) і те, що `/links` взагалі
не ходить у tgstat.
"""
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.browser import TgstatAuthError
from app.parse import check_restricted
from app.server import make_app
from app.session import MANUAL, SessionState, classify
from test_ops import F, cards_html, page

PREMIUM = {"url": "https://tgstat.ru/", "title": "TGStat", "csrf": "t0ken",
           "logged_in": True, "user": "ivan", "plan": "Premium", "captcha": False}
ANON = {**PREMIUM, "logged_in": False, "user": "", "plan": ""}


class ApiBrowser:
    """Браузер, якого немає: віддає заготовки й записує, що в нього просили.

    Повторює ту частину поведінки справжнього `Browser`, на яку спирається API:
    під час ручного входу запити падають `RuntimeError` (мідлвар робить із цього
    409), а `check()` відповідає станом MANUAL, не чіпаючи сторінку.
    """

    def __init__(self, replies=(), snapshot=None, fail_with=None):
        self.replies = list(replies)
        # Чим браузер відповідає замість сторінки: TgstatAuthError (Cloudflare,
        # розлогін) справжній Browser кидає сам, ще до парсера.
        self.fail_with = fail_with
        self.calls = []
        self.snapshot = snapshot or PREMIUM
        self.running = True
        self.manual = False
        self.reloads = []
        self.cfg = type("C", (), {"base_url": "https://tgstat.ru"})()

    async def request(self, method, path, form=None):
        if self.manual:
            raise RuntimeError("іде ручний вхід (звичайний Chrome) — "
                               "спершу POST /auth/manual/finish")
        self.calls.append((method, path, dict(form or [])))
        if self.fail_with is not None:
            raise self.fail_with
        # Остання заготовка повторюється: сторінки в tgstat не «закінчуються», а
        # інакше тест про дефолтний max_pages падав би на порожньому списку
        # (IndexError → 404) і виглядав як відмова API.
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        check_restricted(text)
        return text

    async def check(self, reload: bool):
        self.reloads.append(reload)
        if self.manual:
            return SessionState(MANUAL, detail="іде ручний вхід у звичайному Chrome",
                                checked_at="2026-10-01T07:00:00+00:00")
        state = classify(self.snapshot)
        state.checked_at = "2026-10-01T07:00:00+00:00"
        return state

    async def start_manual(self):
        self.manual = True

    async def finish_manual(self):
        self.manual = False

    async def open_login(self):
        return "https://tgstat.ru/"

    async def screenshot(self):
        return b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def api(request):
    """Клієнт до справжнього застосунку; `browser` лишається доступним тесту."""
    marker = request.node.get_closest_marker("browser")
    browser = (marker.args[0] if marker else ApiBrowser())

    async def make():
        app = make_app(_cfg())
        app.on_startup.clear()      # Chrome і keepalive у тестах не потрібні
        app.on_cleanup.clear()
        app["browser"] = browser
        client = TestClient(TestServer(app))
        await client.start_server()
        return client

    import asyncio
    loop = asyncio.new_event_loop()
    client = loop.run_until_complete(make())
    client.browser = browser
    client.run = loop.run_until_complete
    yield client
    loop.run_until_complete(client.close())
    loop.close()


def _cfg():
    from app.config import Config
    return Config()


def get(api, url, **params):
    async def go():
        r = await api.get(url, params={k: str(v) for k, v in params.items()})
        return r.status, (await r.json() if r.content_type == "application/json"
                          else await r.text())
    return api.run(go())


def post(api, url):
    async def go():
        r = await api.post(url)
        return r.status, await r.json()
    return api.run(go())


def with_browser(browser):
    return pytest.mark.browser(browser)


# ----------------------------------------------------------------- сесія

def test_health_reports_browser_and_manual(api):
    status, body = get(api, "/health")
    assert status == 200 and body == {"ok": True, "browser": True, "manual": False}


def test_auth_status_premium_is_usable_without_instructions(api):
    status, body = get(api, "/auth/status")
    assert status == 200 and body["state"] == "ok" and body["usable"] is True
    assert body["user"] == "ivan" and body["plan"] == "Premium"
    # поради «як увійти» у придатної сесії бути не має — інакше модель лікує здорове
    assert "how_to_login" not in body
    assert api.browser.reloads == [False]


@with_browser(ApiBrowser(snapshot=ANON))
def test_auth_status_without_login_explains_how(api):
    status, body = get(api, "/auth/status")
    assert status == 200 and body["state"] == "login_required"
    assert body["usable"] is False and "vnc" in body["how_to_login"].lower()


def test_auth_status_reload_flag_reaches_browser(api):
    get(api, "/auth/status", reload=1)
    assert api.browser.reloads == [True]


def test_manual_login_blocks_data_and_finish_unblocks(api):
    assert post(api, "/auth/manual")[1]["manual"] is True
    assert get(api, "/health")[1]["manual"] is True
    # поки людина логіниться — жоден запит до tgstat не йде, і це 409, не 500
    status, body = get(api, "/catalog/tags")
    assert status == 409 and body["state"] == MANUAL
    # стан сесії при цьому читається (сторінку не чіпаємо)
    assert get(api, "/auth/status")[1]["state"] == MANUAL

    status, body = post(api, "/auth/manual/finish")
    assert status == 200 and body["state"] == "ok" and body["usable"] is True
    assert api.browser.manual is False


# ------------------------------------------------------------ дані: канали

@with_browser(ApiBrowser([page(cards_html(), True)]))
def test_channels_search_defaults_and_mapping(api):
    status, body = get(api, "/channels/search", q="Бурятия")
    assert status == 200 and body["count"] == 30 and body["items"]
    method, path, form = api.browser.calls[0]
    assert (method, path) == ("POST", "/channels/search")
    # дефолт країни — Россия (id 1): без нього пошук тягне весь світ
    assert form["q"] == "Бурятия" and form["countries[]"] == "1"
    item = body["items"][0]
    assert item["ref"].startswith("@") and item["tgstat_url"].startswith("https://tgstat.ru/")


@with_browser(ApiBrowser([page(cards_html(), True), page(cards_html(), True)]))
def test_channels_search_max_pages_is_honoured(api):
    status, body = get(api, "/channels/search", q="x", max_pages=1)
    assert status == 200 and body["pages"] == 1 and body["has_more"] is True
    assert len(api.browser.calls) == 1, "max_pages=1 — рівно один запит до tgstat"


@with_browser(ApiBrowser([page(cards_html(), True)]))
def test_channels_search_filters_go_to_tgstat(api):
    get(api, "/channels/search", q="x", in_about=1, min_subs=1000, max_subs=50000,
        category="Политика", sort="avg_reach")
    form = api.browser.calls[0][2]
    assert form["participantsCountFrom"] == "1000"
    assert form["participantsCountTo"] == "50000"
    assert form["categories[]"] == "38"
    assert form["inAbout"] == "1" and form["sort"] == "avg_reach"


def test_unknown_filter_is_400_with_the_reason(api):
    status, body = get(api, "/channels/search", q="x", category="Нема такої")
    assert status == 400 and "Нема такої" in body["error"]
    assert api.browser.calls == [], "поганий фільтр не має доходити до tgstat"


def test_bad_number_is_400_naming_the_parameter(api):
    status, body = get(api, "/channels/search", q="x", limit="багато")
    assert status == 400 and "limit" in body["error"]


# ----------------------------------------------------------- дані: підбірки

@with_browser(ApiBrowser([json.dumps({"status": "ok", "html": "<div></div>"})]))
def test_catalog_tags_kind_reaches_path(api):
    get(api, "/catalog/tags", kind="theme")
    assert api.browser.calls[0][1] == "/tags/theme"


def test_catalog_tags_rejects_unknown_kind(api):
    status, body = get(api, "/catalog/tags", kind="geo2")
    assert status == 400 and "geo" in body["error"]


@with_browser(ApiBrowser([page(json.loads((F / "tag_items.json").read_text())["html"],
                               False)]))
def test_catalog_chats_is_the_only_way_to_chats(api):
    status, body = get(api, "/catalog/buratia-region", kind="chat")
    assert status == 200 and body["count"] == len(body["items"]) > 0
    method, path, form = api.browser.calls[0]
    assert path == "/tag/buratia-region/items" and form["peerType"] == "chat"
    # сторінка підбірки одна на канали й чати — вид вибирає peerType у формі
    assert body["tgstat_url"] == "https://tgstat.ru/tag/buratia-region"


# -------------------------------------------------------------- дані: картка

@with_browser(ApiBrowser([(F / "channel_stat.html").read_text()]))
def test_channel_card_resolves_ref_and_returns_stats(api):
    status, body = get(api, "/channel/https://t.me/rian_ru")
    assert status == 200 and api.browser.calls[0][1] == "/channel/@rian_ru/stat"
    assert body["ref"] == "@rian_ru" and body["stats"]
    assert body["stats"]["subscribers"]["value"] > 0


@with_browser(ApiBrowser(["<html><body>нічого</body></html>"]))
def test_unknown_channel_is_404(api):
    status, body = get(api, "/channel/@nosuchchannel")
    assert status == 404 and "не знає" in body["error"]


@with_browser(ApiBrowser([(F / "channel_stat.html").read_text()]))
def test_channel_kind_chat_changes_the_path(api):
    get(api, "/channel/@ulan", kind="chat")
    assert api.browser.calls[0][1] == "/chat/@ulan/stat"


# ----------------------------------------------------------- дані: публікації

@with_browser(ApiBrowser([(F / "posts_search.html").read_text()]))
def test_posts_search_dates_become_tgstat_format(api):
    status, body = get(api, "/posts/search", q="Ds", **{"from": "2026-09-01",
                                                       "to": "2026-09-10"})
    assert status == 200 and body["total"] == 2
    form = api.browser.calls[0][2]
    assert form["startDate"] == "01.09.2026" and form["endDate"] == "10.09.2026"
    assert body["items"] and body["items"][0]["tme_post_url"].startswith("https://t.me/")


@with_browser(ApiBrowser([(F / "posts_not_found.html").read_text()]))
def test_posts_search_empty_is_200_not_an_error(api):
    status, body = get(api, "/posts/search", q="абракадабра")
    assert status == 200 and body["items"] == [] and body["total"] == 0


# ---------------------------------------------------------------- посилання

def test_links_never_touch_tgstat(api):
    status, body = get(api, "/links/@rian_ru", post_id=5)
    assert status == 200
    assert body["tgstat_post_url"] == "https://tgstat.ru/channel/@rian_ru/5"
    assert body["tme_post_url"] == "https://t.me/rian_ru/5"
    assert api.browser.calls == [], "/links складає URL сам, без запиту"


def test_links_keep_kind_from_the_url(api):
    _, body = get(api, "/links/https://tgstat.ru/chat/@ulan/stat")
    assert "/chat/@ulan" in body["tgstat_url"]


# ------------------------------------------------------- стани, що потребують людини

@with_browser(ApiBrowser([(F / "restricted.json").read_text()]))
def test_captcha_is_503_with_state_and_instructions(api):
    status, body = get(api, "/channels/search", q="x")
    assert status == 503 and body["state"] == "captcha"
    assert "vnc" in body["how_to_login"].lower()


@with_browser(ApiBrowser(["ignored"], fail_with=TgstatAuthError(
    "Cloudflare не пропускає — потрібен вхід через VNC")))
def test_cloudflare_is_503_login_required(api):
    status, body = get(api, "/channel/@rian_ru")
    assert status == 503 and body["state"] == "login_required"
    assert "Cloudflare" in body["error"] and "vnc" in body["how_to_login"].lower()


# ------------------------------------------------------------------- /raw

@with_browser(ApiBrowser(["<html>сире</html>"]))
def test_raw_returns_the_body_as_text(api):
    status, text = get(api, "/raw", path="/channel/@rian_ru/stat")
    assert status == 200 and text == "<html>сире</html>"


@pytest.mark.parametrize("path", ["channel/@x", "/accounts/logout", "/payments/pay"])
def test_raw_refuses_absolute_and_dangerous_paths(api, path):
    status, _ = get(api, "/raw", path=path)
    assert status == 400
    assert api.browser.calls == []
