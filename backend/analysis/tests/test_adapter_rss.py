"""RSS-адаптер: парсинг, watermark (seen_ids), backfill, 304."""
from pathlib import Path
from unittest import mock

import pytest

from analysis.services.infospace.adapters import get_adapter
from analysis.services.infospace.adapters.rss import RssAdapter

FIXture = Path(__file__).parent / "fixtures" / "sample.rss.xml"
FEED_XML = FIXture.read_text(encoding="utf-8")


class _Src:
    """Мінімальний дубль Source (адаптер не пише в БД)."""
    def __init__(self, poll_cursor=None, config=None):
        self.url = "https://example.org/feed.xml"
        self.poll_cursor = poll_cursor or {}
        self.config = config or {}


def _parsed(status=200, etag=None, modified=None):
    import feedparser
    d = feedparser.parse(FEED_XML)
    d.status = status
    if etag:
        d.etag = etag
    if modified:
        d.modified = modified
    return d


def test_rss_registered_via_import():
    assert isinstance(get_adapter("rss"), RssAdapter)


def test_parses_entries_and_canonicalizes_url():
    src = _Src()
    with mock.patch("feedparser.parse", return_value=_parsed()):
        items = RssAdapter().fetch(src)
    assert len(items) == 2
    # utm зрізано (canonical_url)
    assert items[0].url == "https://example.org/news/1"
    assert items[0].title.startswith("У Бурятії")
    assert items[0].posted_at is not None
    # watermark збережено в poll_cursor
    assert set(src.poll_cursor["seen_ids"]) == {
        "https://example.org/news/1", "https://example.org/news/2"}


def test_second_poll_skips_seen():
    src = _Src(poll_cursor={"seen_ids": ["https://example.org/news/1",
                                   "https://example.org/news/2"]})
    with mock.patch("feedparser.parse", return_value=_parsed()):
        items = RssAdapter().fetch(src)
    assert items == []


def test_new_entry_after_seen_emitted():
    src = _Src(poll_cursor={"seen_ids": ["https://example.org/news/1"]})
    with mock.patch("feedparser.parse", return_value=_parsed()):
        items = RssAdapter().fetch(src)
    assert [i.url for i in items] == ["https://example.org/news/2"]


def test_304_returns_empty_and_keeps_state():
    src = _Src(poll_cursor={"etag": "abc", "seen_ids": ["x"]})
    with mock.patch("feedparser.parse", return_value=_parsed(status=304)):
        items = RssAdapter().fetch(src)
    assert items == []
    assert src.poll_cursor == {"etag": "abc", "seen_ids": ["x"]}  # незмінний


def test_strip_html_body():
    from analysis.services.infospace.adapters.rss import _strip_html
    assert _strip_html("<p>Текст <a href='x'>лінк</a></p>") == "Текст лінк"
    assert _strip_html("без тегів") == "без тегів"


def test_full_text_fetches_when_body_short(monkeypatch):
    # config.full_text + короткий RSS-опис → дотягує статтю
    from analysis.services.infospace.adapters import rss as rss_mod
    calls = []
    monkeypatch.setattr(rss_mod, "_fetch_full_text",
                        lambda url, opts=None: calls.append(url) or ("повний текст статті " * 20))
    src = _Src(config={"full_text": True})
    with mock.patch("feedparser.parse", return_value=_parsed()):
        items = RssAdapter().fetch(src)
    assert calls                                    # дотягувало за лінком
    assert all(len(i.text) >= 200 for i in items)   # тепер повне тіло


def test_full_text_off_keeps_rss_body(monkeypatch):
    # full_text дефолтно УВІМКНЕНО (тонкий опис дотягується); вимикається лише явним false
    from analysis.services.infospace.adapters import rss as rss_mod

    def _boom(url, opts=None):
        raise AssertionError("не мало дотягувати при config.full_text=false")
    monkeypatch.setattr(rss_mod, "_fetch_full_text", _boom)
    src = _Src(config={"full_text": False})
    with mock.patch("feedparser.parse", return_value=_parsed()):
        items = RssAdapter().fetch(src)
    assert len(items) == 2


def test_backfill_limit_on_first_poll():
    src = _Src(config={"backfill_limit": 1})
    with mock.patch("feedparser.parse", return_value=_parsed()):
        items = RssAdapter().fetch(src)
    assert len(items) == 1  # лише найновіший backfill_limit
