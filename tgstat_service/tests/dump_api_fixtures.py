"""Записати ЕТАЛОННІ відповіді HTTP-API в `backend/analysis/tests/fixtures/tgstat_api/`.

Навіщо: MCP-інструменти `tgstat_*` у Django (`analysis/services/mcp_api/tgstat.py`)
живуть в іншому контейнері, де немає ні цього сервісу, ні Playwright, і тестуються
моками. Мок брехливий ровно настільки, наскільки вигадана його відповідь, — тому
відповіді беремо не з голови, а з цього самого застосунку (справжні маршрути й
парсери, підставлений лише браузер) і кладемо у файли, на яких стоять ті тести.

Перегенерувати після зміни парсерів або формату API:

    docker run --rm -v "$PWD:/repo" -w /repo/tgstat_service \
        tg-event-analytics-tgstat:latest \
        python tests/dump_api_fixtures.py   # (потрібен лише aiohttp із образу)

Файли комітяться: розбіжність між ними й кодом сервісу має ламати тест, а не бій.
"""
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

OUT = HERE.parents[1] / "backend/analysis/tests/fixtures/tgstat_api"
F = HERE / "fixtures"

# /tags/geo — звичайна сторінка, а не AJAX-обгортка зі «html» (на відміну від
# пошуку й підбірок), тож і заготовка тут сирий HTML.
TAGS_HTML = ('<html><body><a href="/tag/buratia-region">Бурятия</a>'
             '<a href="/tag/sakha-region">Якутия</a></body></html>')


def page(html, has_more=False, next_page=1, next_offset=30):
    return json.dumps({"status": "ok", "html": html, "hasMore": has_more,
                       "nextPage": next_page, "nextOffset": next_offset})


async def dump():
    from aiohttp.test_utils import TestClient, TestServer

    from app.config import Config
    from app.server import make_app
    from app.session import classify

    cards = json.loads((F / "channels_search.json").read_text())["html"]
    snapshot = {"url": "https://tgstat.ru/", "title": "TGStat", "csrf": "t0ken",
                "logged_in": True, "user": "ivan", "plan": "Premium", "captcha": False}

    class B:
        running, manual = True, False

        def __init__(self, replies):
            self.replies, self.cfg = list(replies), Config()

        async def request(self, method, path, form=None):
            return self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)

        async def check(self, reload):
            st = classify(snapshot)
            st.checked_at = "2026-10-01T07:00:00+00:00"
            return st

    cases = [
        ("health", "/health", {}, [""]),
        ("auth_status", "/auth/status", {}, [""]),
        ("channels_search", "/channels/search", {"q": "Бурятия", "max_pages": "1"},
         [page(cards, True)]),
        ("catalog_tags", "/catalog/tags", {"kind": "geo"}, [TAGS_HTML]),
        ("catalog_chats", "/catalog/buratia-region", {"kind": "chat", "max_pages": "1"},
         [page(json.loads((F / "tag_items.json").read_text())["html"])]),
        ("channel_card", "/channel/@rian_ru", {},
         [(F / "channel_stat.html").read_text()]),
        ("posts_search", "/posts/search", {"q": "Ds", "max_pages": "1"},
         [(F / "posts_search.html").read_text()]),
        ("posts_not_found", "/posts/search", {"q": "абракадабра", "max_pages": "1"},
         [(F / "posts_not_found.html").read_text()]),
        ("links", "/links/@rian_ru", {"post_id": "5"}, [""]),
    ]

    OUT.mkdir(parents=True, exist_ok=True)
    for name, url, params, replies in cases:
        app = make_app(Config())
        app.on_startup.clear()
        app.on_cleanup.clear()
        app["browser"] = B(replies)
        client = TestClient(TestServer(app))
        await client.start_server()
        r = await client.get(url, params=params)
        body = await r.json()
        (OUT / f"{name}.json").write_text(
            json.dumps({"url": url, "params": params, "status": r.status, "body": body},
                       ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"{name}: {r.status} {url}")
        await client.close()


if __name__ == "__main__":
    asyncio.run(dump())
