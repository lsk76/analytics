"""RSS-адаптер: feedparser + умовний GET (etag/modified → 304 задарма).

Watermark у source.poll_cursor:
  etag / modified — для умовного GET (сервер віддає 304, якщо не змінилось);
  seen_ids       — обмежений список останніх guid, щоб не переемітити ті самі
                   елементи (друга лінія — unique(task, url) на вставці).
Перший полінг (порожній poll_cursor) — лише backfill_limit найновіших елементів.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone

import feedparser

from ..utils import canonical_url
from . import register
from .base import BaseSourceAdapter, RawItem

USER_AGENT = "tg-event-analytics infospace monitor (+https://example.org/bot)"
SEEN_CAP = 400  # скільки останніх guid тримати у watermark
MIN_RSS_BODY = 200  # якщо тіло з RSS коротше і config.full_text — дотягуємо статтю


def _strip_html(s: str) -> str:
    """RSS-описи часто містять HTML (<p>, <a>). Витягуємо чистий текст."""
    if "<" not in s:
        return s
    from selectolax.parser import HTMLParser
    return HTMLParser(s).text(separator=" ", strip=True)


def _fetch_full_text(url: str) -> str:
    """Дотягнути повний текст статті за посиланням (trafilatura, реюз web-адаптера)."""
    from .web import WebAdapter, _get
    try:
        return (WebAdapter._extract_trafilatura(_get(url)) or {}).get("text") or ""
    except Exception:  # noqa: BLE001 — не валимо весь полінг через одну статтю
        return ""


def _entry_id(e) -> str:
    return (getattr(e, "id", "") or getattr(e, "guid", "")
            or getattr(e, "link", "") or "").strip()


def _entry_dt(e):
    """published/updated → aware UTC datetime або None."""
    for attr in ("published_parsed", "updated_parsed"):
        st = getattr(e, attr, None)
        if st:
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    return None


@register
class RssAdapter(BaseSourceAdapter):
    kind = "rss"

    def fetch(self, source) -> list[RawItem]:
        poll_cursor = dict(source.poll_cursor or {})
        first_poll = not poll_cursor
        d = feedparser.parse(
            source.url,
            etag=poll_cursor.get("etag"),
            modified=poll_cursor.get("modified"),
            agent=USER_AGENT,
        )
        # feedparser кладе HTTP-статус у d.status; 304 = не змінилось
        if getattr(d, "status", None) == 304:
            return []
        # bozo=1 + виняток парсингу на рівні транспорту (не просто кривий XML) → підняти
        bozo_exc = getattr(d, "bozo_exception", None)
        if getattr(d, "bozo", 0) and isinstance(bozo_exc, (OSError, ConnectionError)):
            raise bozo_exc

        entries = list(d.entries or [])
        seen = set(poll_cursor.get("seen_ids") or [])
        limit = self.backfill_limit(source) if first_poll else self.max_items(source)

        items: list[RawItem] = []
        for e in entries:
            eid = _entry_id(e)
            if not eid:
                continue
            if not first_poll and eid in seen:
                continue
            link = canonical_url(getattr(e, "link", "") or eid)
            title = (getattr(e, "title", "") or "").strip()
            # summary/content: беремо найдовше доступне тіло
            body = (getattr(e, "summary", "") or "").strip()
            if getattr(e, "content", None):
                longest = max((c.get("value", "") for c in e.content), key=len, default="")
                if len(longest) > len(body):
                    body = longest.strip()
            body = _strip_html(body)
            # full_text: title-only/короткі стрічки → дотягуємо статтю за лінком
            if ((source.config or {}).get("full_text") and len(body) < MIN_RSS_BODY
                    and link.startswith("http")):
                body = _fetch_full_text(link) or body
            items.append(RawItem(
                external_id=eid, url=link, title=title, text=body,
                posted_at=_entry_dt(e), author=(getattr(e, "author", "") or "").strip(),
                meta={"feed_title": getattr(d.feed, "title", "")},
            ))
            if len(items) >= limit:
                break

        # оновлюємо watermark У ПАМ'ЯТІ; стадія збереже після успішного upsert
        new_state = {}
        if getattr(d, "etag", None):
            new_state["etag"] = d.etag
        if getattr(d, "modified", None):
            new_state["modified"] = d.modified
        # seen: усі поточні guid (обмежено), щоб наступний полінг їх не повторив
        current_ids = [i for i in (_entry_id(e) for e in entries) if i]
        new_state["seen_ids"] = (current_ids + list(seen))[:SEEN_CAP]
        source.poll_cursor = new_state
        return items
