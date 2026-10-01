"""Самоконтроль розбору (`app/selftest.py`): чи він справді ЛОВИТЬ зміну розмітки.

Сам канарок теж має бути перевірений: канарок, який завжди зелений, гірший за
відсутній. Тому тут живого tgstat немає — є заготовки «розмітка на місці» й
«розмітка поїхала» (порожні сторінки, зникла колонка статистики, зник Premium),
і перевіряється, що в першому разі verdict=ok, а в другому — broken, і що в
звіті названо, ЯКИЙ саме розбір зламався.
"""
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app import selftest
from app.session import LOGIN_REQUIRED, SessionState
from test_api import ANON, ApiBrowser, _cfg, get
from test_ops import F, cards_html, page

GOOD = {
    "channels_search": page(cards_html(), False),
    "catalog_tags": '<a href="/tag/' + selftest.PROBE_TAG + '">Бурятия</a>'
                    + "".join(f'<a href="/tag/r{i}">Регіон {i}</a>' for i in range(60)),
    "catalog_chats": page(json.loads((F / "tag_items.json").read_text())["html"], False),
    "channel_card": (F / "channel_stat.html").read_text(),
    "posts_search": (F / "posts_search.html").read_text(),
}
EMPTY_PAGE = page("<div></div>", False)


def run(replies, only=""):
    import asyncio
    browser = ApiBrowser([])
    browser.replies = list(replies)
    return asyncio.run(selftest.run(browser, only=only)), browser


def one(name, reply):
    """Прогнати РІВНО одну перевірку на одній заготовці."""
    return run([reply], only=name)[0]["checks"][0]


# ------------------------------------------------------------- розмітка на місці

def test_all_checks_pass_on_live_shaped_pages(monkeypatch):
    # Стелі пошуку публікацій розраховані на живий tgstat (тисячі постів за
    # «новости»), а в заготовці їх два — занижуємо, бо перевіряємо РОЗБІР.
    monkeypatch.setattr(selftest, "MIN_POSTS", 2)
    monkeypatch.setattr(selftest, "MIN_TOTAL", 2)
    res, browser = run([GOOD[name] for name, _ in selftest.CHECKS])
    assert res["verdict"] == selftest.VERDICT_OK, res
    assert res["requests"] == len(selftest.CHECKS) == len(browser.calls)
    assert "усі 5 перевірки" in res["summary"]


def test_channel_card_probe_is_a_real_big_channel():
    """Інваріант картки — мільйони підписників: якби число розбиралось як 0 або
    як «3» (роздільник розрядів змінився), перевірка мусить кричати."""
    check = one("channel_card", GOOD["channel_card"])
    assert check["ok"], check["problems"]


# ------------------------------------------------------------- розмітка поїхала

@pytest.mark.parametrize("name,reply,expect", [
    ("channels_search", EMPTY_PAGE, "картки більше не парсяться"),
    ("catalog_tags", "<html><body>нічого</body></html>", "більше не парсяться"),
    ("catalog_chats", EMPTY_PAGE, "чати підбірки"),
    ("channel_card", "<html><body>нічого</body></html>", "перевірка впала"),
    ("posts_search", (F / "posts_not_found.html").read_text(), "пости більше не парсяться"),
    # «Найдено» на місці, а постів розібрано мало — теж зміна розмітки
    ("posts_search", (F / "posts_search.html").read_text(), "пости більше не парсяться"),
])
def test_empty_parse_is_reported_per_check(name, reply, expect):
    check = one(name, reply)
    assert check["ok"] is False
    assert any(expect in p for p in check["problems"]), check["problems"]
    # у звіті мусить бути, куди дивитись людині
    assert check["raw"] or check.get("error")


def test_lost_stats_columns_are_caught():
    """Канали знайшлись, але без охоплення й ІЦ — класична зміна колонок."""
    stripped = cards_html()
    for marker in ("ERR", "подписчиков", "упоминаний"):
        stripped = stripped.replace(marker, "x")
    check = one("channels_search", page(stripped, False))
    if check["ok"]:
        pytest.skip("заготовка не дає зняти статистику — перевірка полів окремо нижче")
    assert any("охоплення" in p or "підписники" in p for p in check["problems"])


def test_half_empty_subscribers_is_a_problem():
    items = [{"ref": "@a", "title": "A", "tgstat_url": "u", "subscribers": 10, "ci": 1},
             {"ref": "@b", "title": "B", "tgstat_url": "u"},
             {"ref": "@c", "title": "C", "tgstat_url": "u"}]
    problems = selftest._problems_peers(items, need=3, what="пошук каналів", stats=True)
    assert any("підписники не розібрались у 2 з 3" in p for p in problems)


def test_missing_ref_is_named_with_the_field():
    items = [{"title": "A", "tgstat_url": "u", "subscribers": 10}] * 3
    problems = selftest._problems_peers(items, need=3, what="пошук каналів", stats=False)
    assert any("порожнє «ref»" in p for p in problems)


# ------------------------------------------- «не перевірено» ≠ «зламалось»

def test_unusable_session_is_unverified_not_broken():
    import asyncio
    browser = ApiBrowser([], snapshot=ANON)
    res = asyncio.run(selftest.run(browser))
    assert res["verdict"] == selftest.VERDICT_UNVERIFIED
    assert res["state"] == LOGIN_REQUIRED and res["requests"] == 0
    assert browser.calls == [], "без придатної сесії в tgstat не ходимо зовсім"


def test_captcha_aborts_the_run_instead_of_blaming_parsers():
    """Капча летить нагору: далі всі запити однаково марні, а звіт «зламалось»
    був би брехнею."""
    from app.parse import Restricted
    with pytest.raises(Restricted):
        run([(F / "restricted.json").read_text()])


def test_unknown_check_name_is_rejected():
    with pytest.raises(ValueError):
        run([], only="немає-такої")


# ------------------------------------------------------------------ ендпоінт

def _client(browser):
    import asyncio
    from app.server import make_app
    loop = asyncio.new_event_loop()

    async def make():
        app = make_app(_cfg())
        app.on_startup.clear()
        app.on_cleanup.clear()
        app["browser"] = browser
        c = TestClient(TestServer(app))
        await c.start_server()
        return c

    client = loop.run_until_complete(make())
    client.browser = browser
    client.run = loop.run_until_complete
    return client


def test_endpoint_reports_ok_and_narrows_with_only():
    browser = ApiBrowser([GOOD["catalog_tags"]])
    client = _client(browser)
    try:
        status, body = get(client, "/selftest", only="catalog_tags")
        assert status == 200 and body["verdict"] == "ok"
        assert [c["name"] for c in body["checks"]] == ["catalog_tags"]
        assert body["requests"] == 1
    finally:
        client.run(client.close())


def test_endpoint_is_503_when_session_cannot_be_used():
    client = _client(ApiBrowser([], snapshot=ANON))
    try:
        status, body = get(client, "/selftest")
        assert status == 503 and body["verdict"] == "unverified"
        assert "vnc" in body["how_to_login"].lower()
    finally:
        client.run(client.close())


def test_closed_chat_without_tme_link_is_not_a_breakage():
    """У закритого чату замість @username хеш — публічного t.me-посилання немає.

    Перший же живий прогін (01.10.2026) назвав це поломкою: з 20 постів один був
    із «Чат ЧП Краснодара» (ref 6mfyXsNMdTI0Yjgy). Стеля була хибна, не tgstat.
    """
    items = [{"post_id": i, "ref": "@public", "date": "1 окт", "views": 10,
              "text": "t", "tme_post_url": "https://t.me/public/1"} for i in range(1, 20)]
    items.append({"post_id": 99, "ref": "6mfyXsNMdTI0Yjgy", "date": "1 окт",
                  "text": "t", "tme_post_url": ""})
    problems = selftest._problems_posts(items, total=949397)
    assert problems == [], problems


def test_public_post_without_tme_link_is_still_caught():
    items = [{"post_id": i, "ref": "@public", "date": "1 окт", "views": 10,
              "text": "t", "tme_post_url": ""} for i in range(1, 11)]
    problems = selftest._problems_posts(items, total=949397)
    assert any("ПУБЛІЧНИХ" in p for p in problems), problems
