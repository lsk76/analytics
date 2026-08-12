"""Проксі та заголовки пер-джерело (Source.config → httpx): utils.http_options
і його протягування в web/rss-адаптери. Без БД і без мережі."""
from unittest import mock

import pytest
from django.test import override_settings

from analysis.services.infospace.adapters import rss as rss_mod
from analysis.services.infospace.adapters import web as web_mod
from analysis.services.infospace.adapters.web import WebAdapter
from analysis.services.infospace.utils import DEFAULT_USER_AGENT, http_options

PROXY = "http://user:pass@proxy.example:44445"


class _Src:
    def __init__(self, config=None, url="https://news.example/section/"):
        self.url = url
        self.config = config or {}
        self.scraper_key = ""
        self.poll_cursor = {}


def test_defaults_no_proxy_bot_ua():
    o = http_options(_Src())
    assert o["proxy"] is None
    assert o["headers"]["User-Agent"] == DEFAULT_USER_AGENT


def test_explicit_proxy_string_wins():
    assert http_options(_Src({"proxy": PROXY}))["proxy"] == PROXY


@override_settings(INFOSPACE_PROXY_URL=PROXY)
@pytest.mark.parametrize("flag", [True, "default", "env"])
@pytest.mark.django_db
def test_proxy_true_takes_url_from_settings(flag):
    assert http_options(_Src({"proxy": flag}))["proxy"] == PROXY


@override_settings(INFOSPACE_PROXY_URL="")
@pytest.mark.django_db
def test_proxy_true_without_settings_is_none():
    # порожній конфіг не має перетворюватись на проксі "" (httpx би впав)
    assert http_options(_Src({"proxy": True}))["proxy"] is None


@override_settings(INFOSPACE_PROXY_URL="http://from-env:1/")
@pytest.mark.django_db
def test_setting_row_overrides_env():
    from analysis.models import Setting
    Setting.objects.create(key="infospace_proxy_url", value=PROXY)
    assert http_options(_Src({"proxy": True}))["proxy"] == PROXY


def test_headers_and_user_agent_from_config():
    o = http_options(_Src({"user_agent": "Mozilla/5.0", "headers": {"Cookie": "beget=begetok"}}))
    assert o["headers"]["User-Agent"] == "Mozilla/5.0"
    assert o["headers"]["Cookie"] == "beget=begetok"


def test_web_adapter_passes_opts_to_get(monkeypatch):
    seen = []

    def _fake_get(url, opts=None):
        seen.append((url, opts))
        return "<html><body><a href='/news/1010/'>x</a></body></html>"

    monkeypatch.setattr(web_mod, "_get", _fake_get)
    monkeypatch.setattr(WebAdapter, "_extract",
                        lambda self, s, u, h: {"title": "t", "text": "тіло" * 40, "date": None})
    src = _Src({"proxy": PROXY, "headers": {"Cookie": "beget=begetok"}})
    items = WebAdapter().fetch(src)

    assert items and len(seen) == 2                      # лістинг + стаття
    for _url, opts in seen:                              # проксі/кука йдуть в ОБИДВА запити
        assert opts["proxy"] == PROXY
        assert opts["headers"]["Cookie"] == "beget=begetok"


def test_rss_uses_httpx_path_only_when_configured(monkeypatch):
    called = {}

    def _fake_httpx(url, opts, cursor):
        called["opts"] = opts
        return _empty_feed()

    monkeypatch.setattr(rss_mod, "_fetch_via_httpx", _fake_httpx)

    # без проксі/заголовків — дефолтний шлях feedparser (httpx-гілку не чіпаємо)
    with mock.patch("feedparser.parse", return_value=_empty_feed()) as fp:
        rss_mod.RssAdapter().fetch(_Src(url="https://example.org/feed.xml"))
    assert fp.called and "opts" not in called

    # з проксі — httpx-гілка (feedparser сам через проксі не ходить)
    rss_mod.RssAdapter().fetch(_Src({"proxy": PROXY}, url="https://example.org/feed.xml"))
    assert called["opts"]["proxy"] == PROXY


def _empty_feed():
    import feedparser
    d = feedparser.util.FeedParserDict(entries=[], feed={}, bozo=0)
    d.status = 200
    return d
