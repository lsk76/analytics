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
    def __init__(self, state=None, config=None):
        self.url = "https://example.org/feed.xml"
        self.state = state or {}
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
    # watermark збережено в state
    assert set(src.state["seen_ids"]) == {
        "https://example.org/news/1", "https://example.org/news/2"}


def test_second_poll_skips_seen():
    src = _Src(state={"seen_ids": ["https://example.org/news/1",
                                   "https://example.org/news/2"]})
    with mock.patch("feedparser.parse", return_value=_parsed()):
        items = RssAdapter().fetch(src)
    assert items == []


def test_new_entry_after_seen_emitted():
    src = _Src(state={"seen_ids": ["https://example.org/news/1"]})
    with mock.patch("feedparser.parse", return_value=_parsed()):
        items = RssAdapter().fetch(src)
    assert [i.url for i in items] == ["https://example.org/news/2"]


def test_304_returns_empty_and_keeps_state():
    src = _Src(state={"etag": "abc", "seen_ids": ["x"]})
    with mock.patch("feedparser.parse", return_value=_parsed(status=304)):
        items = RssAdapter().fetch(src)
    assert items == []
    assert src.state == {"etag": "abc", "seen_ids": ["x"]}  # незмінний


def test_backfill_limit_on_first_poll():
    src = _Src(config={"backfill_limit": 1})
    with mock.patch("feedparser.parse", return_value=_parsed()):
        items = RssAdapter().fetch(src)
    assert len(items) == 1  # лише найновіший backfill_limit
