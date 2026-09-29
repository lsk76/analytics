import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import ops
from app.parse import Restricted, check_restricted

F = Path(__file__).parent / "fixtures"


class FakeBrowser:
    """Відповідає заготовками замість tgstat і пише, що в нього просили."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.cfg = SimpleNamespace(base_url="https://tgstat.ru")

    async def request(self, method, path, form=None):
        self.calls.append((method, path, dict(form or [])))
        text = self.replies.pop(0)
        check_restricted(text)
        return text


def page(html, has_more, next_page=1, next_offset=30):
    return json.dumps({"status": "ok", "html": html, "hasMore": has_more,
                       "nextPage": next_page, "nextOffset": next_offset})


def run(coro):
    return asyncio.run(coro)


def cards_html():
    return json.loads((F / "channels_search.json").read_text())["html"]


def test_channel_search_stops_at_max_pages_and_builds_form():
    b = FakeBrowser([page(cards_html(), True), page(cards_html(), True)])
    res = run(ops.search_channels(b, "Бурятия", min_subs=1000, category="Политика",
                                  max_pages=1))
    assert res["pages"] == 1 and res["count"] == 30 and res["has_more"]
    method, path, form = b.calls[0]
    assert (method, path) == ("POST", "/channels/search")
    assert form["q"] == "Бурятия" and form["participantsCountFrom"] == "1000"
    assert form["categories[]"] == "38" and form["countries[]"] == "1"


def test_duplicate_page_ends_paging():
    # друга сторінка з тими самими картками — нових немає, далі не йдемо
    b = FakeBrowser([page(cards_html(), True), page(cards_html(), True)])
    res = run(ops.search_channels(b, "x", max_pages=5))
    assert res["pages"] == 2 and res["count"] == 30 and not res["has_more"]


def test_unknown_filter_name_is_rejected():
    with pytest.raises(ValueError):
        run(ops.search_channels(FakeBrowser([]), "x", category="Нема такої"))


def test_catalog_requests_chats():
    html = json.loads((F / "tag_items.json").read_text())["html"]
    b = FakeBrowser([page(html, False)])
    res = run(ops.catalog(b, "buratia-region", kind="chat"))
    assert b.calls[0][1] == "/tag/buratia-region/items"
    assert b.calls[0][2]["peerType"] == "chat"
    assert res["count"] == len(res["items"]) > 0


def test_posts_search_continues_with_list_endpoint():
    first = (F / "posts_search.html").read_text().replace(
        "Найдено: <b>2</b>", "Найдено: <b>40</b>")
    more = (F / "posts_search.html").read_text().replace("122908", "122999")
    b = FakeBrowser([first, page(more, False, 2, 40)])
    res = run(ops.search_posts(b, "Ds", date_from="2026-09-01", date_to="2026-09-10",
                               max_pages=3))
    assert [c[1] for c in b.calls] == ["/search", "/search/list"]
    assert b.calls[0][2]["startDate"] == "01.09.2026"
    assert (b.calls[1][2]["page"], b.calls[1][2]["offset"]) == ("1", "20")
    assert res["total"] == 40 and res["pages"] == 2 and not res["has_more"]
    assert len({i["post_id"] for i in res["items"]}) == len(res["items"])


def test_captcha_surfaces_as_restricted():
    b = FakeBrowser([(F / "restricted.json").read_text()])
    with pytest.raises(Restricted):
        run(ops.search_channels(b, "x"))


@pytest.mark.parametrize("raw,expected", [
    ("@rian_ru", (None, "@rian_ru")),
    ("rian_ru", (None, "@rian_ru")),
    ("https://t.me/rian_ru", (None, "@rian_ru")),
    ("https://tgstat.ru/chat/@ulan/stat", ("chat", "@ulan")),
    ("https://tgstat.ru/chat/uVK0KI5dQewzYzNi", ("chat", "uVK0KI5dQewzYzNi")),
])
def test_peer_ref(raw, expected):
    assert ops.peer_ref(raw) == expected


def test_links_without_request():
    l = ops.links("https://t.me/rian_ru", post_id=5)
    assert l["tgstat_post_url"] == "https://tgstat.ru/channel/@rian_ru/5"
    assert l["tme_post_url"] == "https://t.me/rian_ru/5"
