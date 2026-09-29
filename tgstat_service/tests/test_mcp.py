import asyncio

import pytest

pytest.importorskip("mcp")

from aiohttp.test_utils import TestServer  # noqa: E402

from app import mcp_server  # noqa: E402
from app.config import Config  # noqa: E402
from app.server import make_app  # noqa: E402
from test_ops import F, FakeBrowser, cards_html, page  # noqa: E402


def call(replies, tool, **kwargs):
    """Інструмент MCP -> справжній HTTP-API сервісу -> підставний браузер."""
    async def go():
        app = make_app(Config())
        app.on_startup.clear()
        app.on_cleanup.clear()
        fb = FakeBrowser(replies)
        fb.manual, fb.running = False, True
        app["browser"] = fb
        async with TestServer(app) as srv:
            mcp_server.API = str(srv.make_url("")).rstrip("/")
            return await getattr(mcp_server, tool)(**kwargs), fb.calls
    return asyncio.run(go())


def test_channels_search_goes_through_service_with_links():
    text, calls = call([page(cards_html(), False)], "tgstat_channels_search",
                       q="Бурятия", max_pages=1)
    assert calls[0][1] == "/channels/search"
    assert "@arigus" in text and "https://t.me/arigus" in text


def test_captcha_becomes_instruction_not_empty_result():
    text, _ = call([(F / "restricted.json").read_text()], "tgstat_channels_search",
                   q="x")
    assert text.startswith("⚠") and "tgstat_manual_login" in text


def test_links_make_no_tgstat_request():
    text, calls = call([], "tgstat_links", ref="t.me/rian_ru", post_id=5)
    assert calls == [] and "https://t.me/rian_ru/5" in text
