"""Web-адаптер: discovery (лінки статей на лістингу) + extraction (стаття → текст).

Два кроки, обидва конфігуруються з БД (Source.config), без деплою на новий сайт:

  DISCOVERY (де брати лінки статей на source.url):
    config["link_selector"] — CSS-селектор <a> (selectolax); АБО
    config["link_pattern"]  — regex по href; АБО
    (дефолт) евристика: same-domain лінки з цифрою у шляху (id статті).

  EXTRACTION (стаття → title/text/date):
    source.scraper_key       — кастомний клас із реєстру SCRAPERS (пагінація/API); АБО
    config["selectors"]      — {"title","body","date"} CSS-селектори; АБО
    (дефолт) trafilatura     — автоекстракція (нуль конфігурації).

Watermark у source.state["seen_ids"] (canonical-URL). Мережеві помилки лістингу →
виняток (health рахує стадія); збій ОКРЕМОЇ статті — пропускаємо (не валимо полінг).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser

from ..scrapers import get_scraper
from ..utils import canonical_url
from . import register
from .base import BaseSourceAdapter, RawItem

logger = logging.getLogger(__name__)

USER_AGENT = "tg-event-analytics infospace monitor (+https://example.org/bot)"
SEEN_CAP = 500
HTTP_TIMEOUT = 20.0
# Евристика дефолтного discovery: id статті — це прогін ≥4 цифр у href
# (напр. /news/16/525591/ або /2026/04/…), а НЕ короткі id розділів (/news/19/),
# з яких trafilatura витягла б cookie-банер замість статті. Сайти зі
# slug-URL без цифр потребують link_selector/link_pattern у config.
_ARTICLE_ID = re.compile(r"\d{4,}")
# НЕ-статті: сторінки коментарів, тегів, авторів, пошуку — на них trafilatura
# витягує спільний віджет («популярне»), тож усі дають ІДЕНТИЧНИЙ сміттєвий текст
_NON_ARTICLE = re.compile(r"/(comments?|tags?|author|search|login|rss|feed|page)(/|$)")


def _get(url: str) -> str:
    """GET сторінки як текст (httpx сам визначає кодування, напр. windows-1251)."""
    r = httpx.get(url, headers={"User-Agent": USER_AGENT},
                  timeout=HTTP_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _host(u: str) -> str:
    # removeprefix, НЕ lstrip: lstrip зрізає набір символів {w,.} і скалічив би
    # "wired.com"→"ired.com" / "web.x.com"→"eb.x.com" (хибний збіг доменів).
    return urlsplit(u).netloc.lower().removeprefix("www.")


def _same_host(a: str, b: str) -> bool:
    return _host(a) == _host(b)


@register
class WebAdapter(BaseSourceAdapter):
    kind = "web"

    # ------------------------------------------------------------ discovery
    def _discover(self, source, html) -> list[str]:
        cfg = source.config or {}
        base = source.url
        hrefs: list[str] = []
        if cfg.get("link_selector"):
            tree = HTMLParser(html)
            for node in tree.css(cfg["link_selector"]):
                href = node.attributes.get("href")
                if href:
                    hrefs.append(href)
        elif cfg.get("link_pattern"):
            hrefs = re.findall(cfg["link_pattern"], html)
        else:
            # евристика: усі <a> з цифрою у шляху (id статті) на тому ж домені
            tree = HTMLParser(html)
            for node in tree.css("a"):
                href = node.attributes.get("href")
                # цифри шукаємо лише у ШЛЯХУ, не в query — інакше евристика ловить
                # пагінацію («?PAGEN_1=17894», «?page=…») як «статтю» (архів 2009!)
                if href and _ARTICLE_ID.search(urlsplit(href).path):
                    hrefs.append(href)

        seen, out = set(), []
        for h in hrefs:
            absu = canonical_url(urljoin(base, h))
            if not absu.startswith("http") or not _same_host(absu, base):
                continue
            if _NON_ARTICLE.search(urlsplit(absu).path):
                continue   # /comments/, /tag/, /author/ … — не стаття
            if absu == canonical_url(base) or absu in seen:
                continue
            seen.add(absu)
            out.append(absu)
        return out

    # ------------------------------------------------------------ extraction
    def _extract(self, source, url, html) -> dict:
        cfg = source.config or {}
        if source.scraper_key:
            return get_scraper(source.scraper_key).extract(url, html)
        if cfg.get("selectors"):
            return self._extract_selectors(html, cfg["selectors"])
        return self._extract_trafilatura(html)

    @staticmethod
    def _extract_selectors(html, sel) -> dict:
        tree = HTMLParser(html)

        def _txt(key):
            node = tree.css_first(sel[key]) if sel.get(key) else None
            return node.text(strip=True) if node else ""

        # дата: реальні сайти кладуть ISO у <time datetime> / <meta content>,
        # а текст може бути «8 июля 2026» — пробуємо атрибути, потім текст
        date_node = tree.css_first(sel["date"]) if sel.get("date") else None
        date_raw = ""
        if date_node is not None:
            a = date_node.attributes
            date_raw = a.get("datetime") or a.get("content") or date_node.text(strip=True)
        return {"title": _txt("title"), "text": _txt("body"),
                "date": _parse_date(date_raw)}

    @staticmethod
    def _extract_trafilatura(html) -> dict:
        import json
        import trafilatura
        raw = trafilatura.extract(html, output_format="json", with_metadata=True,
                                  favor_precision=True)
        if not raw:
            return {"title": "", "text": "", "date": None}
        d = json.loads(raw)
        return {"title": d.get("title") or "", "text": d.get("text") or "",
                "date": _parse_date(d.get("date"))}

    # ------------------------------------------------------------ fetch
    def fetch(self, source) -> list[RawItem]:
        state = dict(source.state or {})
        first_poll = not state
        listing = _get(source.url)                 # помилка лістингу → виняток
        links = self._discover(source, listing)
        seen = set(state.get("seen_ids") or [])
        limit = self.backfill_limit(source) if first_poll else self.max_items(source)
        fresh = [u for u in links if first_poll or u not in seen][:limit]

        items: list[RawItem] = []
        done: list[str] = []   # у watermark лише УСПІШНО завантажені (див. рев'ю)
        for url in fresh:
            try:
                art = _get(url)
                data = self._extract(source, url, art)
            except Exception as e:  # noqa: BLE001 — транзієнтний збій статті:
                logger.warning("web.extract %s: %r", url, e)
                continue           # НЕ в seen → ретрай наступного полінгу
            done.append(url)       # завантажено (навіть без тексту — це не стаття,
                                   # більше не смикаємо), але в items лише з текстом
            text = (data.get("text") or "").strip()
            if not text:
                continue
            items.append(RawItem(
                external_id=url, url=url,
                title=(data.get("title") or "").strip(),
                text=text, posted_at=data.get("date"), meta={}))

        source.state = {"seen_ids": (done + list(seen))[:SEEN_CAP]}
        return items


def _parse_date(s):
    """YYYY-MM-DD[...] → aware UTC datetime (північ) або None."""
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(s))
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
    except ValueError:
        return None
