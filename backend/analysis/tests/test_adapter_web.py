"""Web-адаптер: discovery (селектор/regex/евристика), extraction, watermark, 404."""
from pathlib import Path

import httpx
import pytest

from analysis.services.infospace.adapters import get_adapter, web
from analysis.services.infospace.adapters.web import WebAdapter
from analysis.services.infospace.scrapers import SCRAPERS, register_scraper

FIX = Path(__file__).parent / "fixtures"
LISTING = (FIX / "listing.html").read_text(encoding="utf-8")
ARTICLE = (FIX / "article.html").read_text(encoding="utf-8")

LISTING_URL = "https://news.example/section/"
ART1 = "https://news.example/news/1010/"
ART2 = "https://news.example/news/1020/"


class _Src:
    def __init__(self, config=None, scraper_key="", poll_cursor=None):
        self.url = LISTING_URL
        self.config = config or {}
        self.scraper_key = scraper_key
        self.poll_cursor = poll_cursor or {}


def _fake_get(pages, not_found=()):
    def _get(url, opts=None):
        if url in not_found:
            raise httpx.HTTPStatusError("404", request=None, response=None)
        try:
            return pages[url]
        except KeyError:
            raise httpx.HTTPStatusError("404", request=None, response=None)
    return _get


def _serve(monkeypatch, pages, not_found=()):
    monkeypatch.setattr(web, "_get", _fake_get(pages, not_found))


def test_web_registered():
    assert isinstance(get_adapter("web"), WebAdapter)


def test_same_host_prefix_not_charset():
    # removeprefix, не lstrip: домени з початковими w/крапкою не калічаться
    assert web._same_host("https://wired.com/a", "https://www.wired.com/b")
    assert web._same_host("https://web.example.com/1", "https://web.example.com/2")
    assert not web._same_host("https://wired.com/a", "https://tired.com/b")
    assert not web._same_host("https://web.example.com/1", "https://eb.example.com/2")


def test_discovery_by_selector_and_extraction_selectors(monkeypatch):
    _serve(monkeypatch, {LISTING_URL: LISTING, ART1: ARTICLE, ART2: ARTICLE})
    src = _Src(config={
        "link_selector": "a.article-link",
        "selectors": {"title": "h1.headline", "body": ".article-body",
                      "date": "time.pub-date"},
    })
    items = WebAdapter().fetch(src)
    urls = {i.url for i in items}
    assert urls == {ART1, ART2}                  # чужий домен і /about/ відсіяно
    a = next(i for i in items if i.url == ART1)
    assert a.title.startswith("Задержан руководитель")
    assert "взятк" in a.text
    assert a.posted_at is not None and a.posted_at.year == 2026


def test_discovery_dedupes_utm_variant(monkeypatch):
    # /news/102/?utm_source=rss і /news/102/ — один лінк після canonical_url
    _serve(monkeypatch, {LISTING_URL: LISTING, ART1: ARTICLE, ART2: ARTICLE})
    src = _Src(config={"link_selector": "a.article-link"})
    items = WebAdapter().fetch(src)
    assert sum(1 for i in items if i.url == ART2) == 1


def test_discovery_by_regex(monkeypatch):
    _serve(monkeypatch, {LISTING_URL: LISTING, ART1: ARTICLE, ART2: ARTICLE})
    src = _Src(config={"link_pattern": r'href="(/news/\d+/[^"]*)"',
                       "selectors": {"title": "h1.headline", "body": ".article-body"}})
    items = WebAdapter().fetch(src)
    assert {i.url for i in items} == {ART1, ART2}


def test_discovery_excludes_comments_tag_author(monkeypatch):
    # /comments/, /tag/, /author/ — не статті (trafilatura дає спільний віджет),
    # мають відсіюватись, лишається лише сама стаття
    listing = ('<a href="/news/1010/">стаття</a>'
               '<a href="/text/sport/2026/07/13/76524491/comments/">коментарі</a>'
               '<a href="/text/2026/07/13/76530092/comments/?discuss=1">обговорення</a>'
               '<a href="/tag/2026/">тег</a>'
               '<a href="/author/12345/">автор</a>')
    _serve(monkeypatch, {LISTING_URL: listing, ART1: ARTICLE})
    src = _Src(config={"selectors": {"title": "h1.headline", "body": ".article-body"}})
    items = WebAdapter().fetch(src)
    assert {i.url for i in items} == {ART1}   # лише стаття, без /comments/ /tag/ /author/


def test_discovery_heuristic_ignores_pagination_query(monkeypatch):
    # цифри в query (?PAGEN_1=17894) — НЕ стаття (архівна пагінація 2009 р.);
    # евристика має дивитись лише на ШЛЯХ
    listing = ('<a href="/news/1010/">стаття</a>'
               '<a href="/news/?PAGEN_1=17894">архів ст.17894</a>'
               '<a href="/news/?page=99999">сторінка</a>')
    pages = {LISTING_URL: listing, ART1: ARTICLE}
    _serve(monkeypatch, pages)
    src = _Src(config={"selectors": {"title": "h1.headline", "body": ".article-body"}})
    items = WebAdapter().fetch(src)
    assert {i.url for i in items} == {ART1}   # пагінація відсіяна


def test_discovery_heuristic_filters_nav_and_foreign(monkeypatch):
    # без конфіга: евристика (цифра у шляху) бере /news/101,102; не /about, не чужий домен
    _serve(monkeypatch, {LISTING_URL: LISTING, ART1: ARTICLE, ART2: ARTICLE})
    src = _Src(config={"selectors": {"title": "h1.headline", "body": ".article-body"}})
    items = WebAdapter().fetch(src)
    assert {i.url for i in items} == {ART1, ART2}


def test_trafilatura_extraction_default(monkeypatch):
    _serve(monkeypatch, {LISTING_URL: LISTING, ART1: ARTICLE, ART2: ARTICLE})
    src = _Src(config={"link_selector": "a.article-link"})  # без selectors → trafilatura
    items = WebAdapter().fetch(src)
    assert items and all(len(i.text) > 50 for i in items)   # автоекстракція дала текст


def test_listing_404_raises(monkeypatch):
    _serve(monkeypatch, {}, not_found=(LISTING_URL,))
    with pytest.raises(httpx.HTTPStatusError):
        WebAdapter().fetch(_Src(config={"link_selector": "a.article-link"}))


def test_discovery_excludes_foreign_via_same_host(monkeypatch):
    # link_selector навмисно ловить ЧУЖОДОМЕННИЙ лінк (aside a) → _same_host відсіює
    _serve(monkeypatch, {LISTING_URL: LISTING})
    src = _Src(config={"link_selector": "aside a"})   # тільки https://other.example/...
    assert WebAdapter().fetch(src) == []


def test_broken_article_skipped_then_retried_next_poll(monkeypatch):
    # ART1 транзієнтно 404, ART2 норм → полінг не падає, лишається ART2,
    # ART1 НЕ в seen (щоб не втратити назавжди)
    src = _Src(config={"link_selector": "a.article-link",
                       "selectors": {"title": "h1.headline", "body": ".article-body"}})
    _serve(monkeypatch, {LISTING_URL: LISTING, ART2: ARTICLE}, not_found=(ART1,))
    items = WebAdapter().fetch(src)
    assert {i.url for i in items} == {ART2}
    assert ART1 not in set(src.poll_cursor["seen_ids"])     # збійна стаття НЕ позначена seen
    # наступний полінг: ART1 уже доступний → підбирається (не втрачено)
    _serve(monkeypatch, {LISTING_URL: LISTING, ART1: ARTICLE, ART2: ARTICLE})
    items2 = WebAdapter().fetch(src)
    assert {i.url for i in items2} == {ART1}


def test_watermark_skips_seen_on_second_poll(monkeypatch):
    _serve(monkeypatch, {LISTING_URL: LISTING, ART1: ARTICLE, ART2: ARTICLE})
    src = _Src(config={"link_selector": "a.article-link",
                       "selectors": {"title": "h1.headline", "body": ".article-body"}})
    WebAdapter().fetch(src)                       # перший полінг — обидві статті
    assert set(src.poll_cursor["seen_ids"]) == {ART1, ART2}
    items2 = WebAdapter().fetch(src)              # другий — нічого нового
    assert items2 == []


def test_custom_scraper_via_scraper_key(monkeypatch):
    @register_scraper("_test_web")
    class Dummy:
        def extract(self, url, html):
            return {"title": "custom", "text": "тіло від кастомного скрапера", "date": None}
    try:
        _serve(monkeypatch, {LISTING_URL: LISTING, ART1: ARTICLE, ART2: ARTICLE})
        src = _Src(config={"link_selector": "a.article-link"}, scraper_key="_test_web")
        items = WebAdapter().fetch(src)
        assert all(i.text == "тіло від кастомного скрапера" for i in items)
    finally:
        SCRAPERS.pop("_test_web", None)
