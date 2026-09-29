"""Операції над tgstat: запити через Browser.request + парсери з parse.py.

Кожна сторінка результатів = окремий запит до tgstat, тож max_pages — це
прямо «скільки запитів» (tgstat відповідає капчею на частоту, див. config).
"""
import re
from datetime import date
from typing import Optional

from . import filters, parse
from .browser import Browser

MAX_PAGES_CAP = 20
LIMIT_CAP = 1000


def _pick(table: dict, value: Optional[str], what: str) -> str:
    """Назва («Политика») або id («38») -> id; порожнє -> ''."""
    if not value:
        return ""
    value = value.strip()
    if value.isdigit():
        return value
    for name, id_ in table.items():
        if name.lower() == value.lower():
            return id_
    raise ValueError(f"невідоме значення {what}: {value!r}")


def _clamp(value: int, cap: int) -> int:
    return max(1, min(int(value), cap))


def peer_ref(value: str) -> tuple[Optional[str], str]:
    """'@h', 'h', 't.me/h', 'https://tgstat.ru/chat/@h/stat' -> (kind|None, '@h')."""
    value = value.strip()
    m = parse.PEER_URL_RE.search(value)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^(?:https?://)?t\.me/([\w]+)", value)
    if m:
        return None, "@" + m.group(1)
    if re.fullmatch(r"@?[A-Za-z]\w{3,}", value):
        return None, "@" + value.lstrip("@")
    raise ValueError(f"не схоже на канал/чат: {value!r}")


async def _paged(browser: Browser, path: str, form: list, limit: int,
                 max_pages: int) -> dict:
    """Спільна пагінація JSON-ендпоінтів tgstat (page/offset/hasMore)."""
    items, seen, pages = [], set(), 0
    page_no, offset, has_more = 0, 0, False
    base = browser.cfg.base_url
    while pages < max_pages and len(items) < limit:
        data = parse.parse_json(await browser.request(
            "POST", path, form + [("page", str(page_no)), ("offset", str(offset))]))
        pages += 1
        chunk = [p for p in parse.parse_peers(data.get("html", ""), base)
                 if p["ref"] not in seen]
        seen.update(p["ref"] for p in chunk)
        items.extend(chunk)
        has_more = bool(data.get("hasMore")) and bool(chunk)
        if not has_more:
            break
        page_no = int(data.get("nextPage") or page_no + 1)
        offset = int(data.get("nextOffset") or offset + len(chunk))
    return {"items": items[:limit], "count": min(len(items), limit),
            "pages": pages, "has_more": has_more or len(items) > limit}


async def search_channels(browser: Browser, q: str, in_about: bool = False,
                          min_subs: Optional[int] = None,
                          max_subs: Optional[int] = None,
                          country: Optional[str] = "Россия",
                          category: Optional[str] = None,
                          language: Optional[str] = None,
                          sort: str = "participants",
                          limit: int = 100, max_pages: int = 3) -> dict:
    """Пошук каналів за словами в назві (і описі, in_about) — /channels/search.
    Поля форми — ті самі, що в tools/discovery/tgstat_parser (перевірені)."""
    if not q.strip():
        raise ValueError("q: потрібне слово для пошуку")
    if sort not in ("participants", "avg_reach", "ci_index", "members_t",
                    "members_y", "members_7d", "members_30d"):
        raise ValueError(f"sort: невідоме {sort!r}")
    form = [
        ("view", "list"), ("sort", sort), ("q", q.strip()),
        ("inAbout", "1" if in_about else "0"),
        ("categories", ""), ("languages", ""), ("countries", ""),
        ("channelType", ""),
        ("participantsCountFrom", str(min_subs or "")),
        ("participantsCountTo", str(max_subs or "")),
        ("avgReachFrom", ""), ("avgReachTo", ""),
        ("avgReach24From", ""), ("avgReach24To", ""), ("ciFrom", ""), ("ciTo", ""),
        ("age", "0-120"), ("err", "0-100"), ("er", "0"), ("male", "0"),
        ("female", "0"), ("isVerified", "0"), ("isRknVerified", "0"),
        ("isStoriesAvailable", "0"),
    ]
    for key in ("noRedLabel", "noScam", "noDead"):
        form += [(key, "0"), (key, "1")]
    for value, table, field in ((country, filters.COUNTRIES, "countries[]"),
                                (category, filters.CATEGORIES, "categories[]"),
                                (language, filters.LANGUAGES, "languages[]")):
        id_ = _pick(table, value, field)
        if id_:
            form.append((field, id_))
    res = await _paged(browser, "/channels/search", form,
                       _clamp(limit, LIMIT_CAP), _clamp(max_pages, MAX_PAGES_CAP))
    return {"query": q, **res}


async def catalog(browser: Browser, tag: str, kind: str = "channel",
                  category_id: int = 0, limit: int = 200,
                  max_pages: int = 5) -> dict:
    """Канали або чати тематичної/регіональної підбірки — /tag/<тег>/items.
    ЄДИНИЙ спосіб дістати чати: окремого пошуку чатів у tgstat немає."""
    if kind not in ("channel", "chat"):
        raise ValueError("kind: channel або chat")
    if not re.fullmatch(r"[\w-]+", tag or ""):
        raise ValueError(f"tag: slug підбірки, напр. buratia-region, а не {tag!r}")
    form = [("peerType", kind), ("sortChannel", "members"),
            ("sortChat", "members"), ("categoryId", str(category_id))]
    res = await _paged(browser, f"/tag/{tag}/items", form,
                       _clamp(limit, LIMIT_CAP), _clamp(max_pages, MAX_PAGES_CAP))
    return {"tag": tag, "kind": kind,
            "tgstat_url": f"{browser.cfg.base_url}/tag/{tag}", **res}


async def tags(browser: Browser, kind: str = "geo") -> dict:
    """Список підбірок: geo — регіональні, theme — тематичні."""
    if kind not in ("geo", "theme"):
        raise ValueError("kind: geo або theme")
    html = await browser.request("GET", f"/tags/{kind}")
    items = parse.parse_tags(html)
    return {"kind": kind, "items": items, "count": len(items)}


async def channel(browser: Browser, ref: str, kind: Optional[str] = None) -> dict:
    """Картка каналу/чату зі сторінки статистики tgstat."""
    found_kind, ref = peer_ref(ref)
    kind = kind or found_kind or "channel"
    html = await browser.request("GET", f"/{kind}/{ref}/stat")
    data = parse.parse_channel_stat(html, browser.cfg.base_url)
    if not data.get("title") and not data.get("stats"):
        raise LookupError(f"tgstat не знає {kind} {ref}")
    data.setdefault("kind", kind)
    data.setdefault("ref", ref)
    return data


def _ddmmyyyy(value: Optional[str]) -> str:
    return date.fromisoformat(value).strftime("%d.%m.%Y") if value else ""


async def search_posts(browser: Browser, q: str,
                       date_from: Optional[str] = None,
                       date_to: Optional[str] = None,
                       peer_type: str = "all", sort: str = "date",
                       hide_forwards: bool = False, strong: bool = False,
                       extended: bool = False, minus_words: str = "",
                       limit: int = 100, max_pages: int = 3) -> dict:
    """Пошук публікацій (Premium): POST /search — перша сторінка HTML, далі
    POST /search/list з тими ж полями і page/offset зі сторінки (як у sm-analytics)."""
    if not q.strip():
        raise ValueError("q: потрібен запит")
    if peer_type not in ("all", "channel", "chat"):
        raise ValueError("peer_type: all, channel або chat")
    if sort not in ("date", "views"):
        raise ValueError("sort: date або views")
    limit, max_pages = _clamp(limit, LIMIT_CAP), _clamp(max_pages, MAX_PAGES_CAP)
    base = browser.cfg.base_url
    flags = [("strongSearch", strong), ("hideForwards", hide_forwards),
             ("extendedSyntax", extended), ("hideDeleted", False),
             ("onlyMentioned", False)]
    form = [("q", q.strip()), ("startDate", _ddmmyyyy(date_from)),
            ("endDate", _ddmmyyyy(date_to)), ("peerType", peer_type),
            ("sort", sort), ("minusWords", minus_words),
            ("country", ""), ("language", ""), ("category", "")]
    form += [(k, "1" if v else "0") for k, v in flags]

    html = await browser.request("POST", "/search", form)
    total = parse.found_count(html)
    items = [] if parse.nothing_found(html) else parse.parse_posts(html, base)
    state, pages, has_more = parse.search_form_state(html), 1, bool(items)
    seen = {(p["ref"], p["post_id"]) for p in items}
    if total is not None:
        has_more = len(items) < total
    while has_more and pages < max_pages and len(items) < limit and state:
        data = parse.parse_json(await browser.request(
            "POST", "/search/list",
            form + [("page", state.get("page", "1")), ("offset", state.get("offset", "0"))]))
        pages += 1
        chunk = [p for p in parse.parse_posts(data.get("html", ""), base)
                 if (p["ref"], p["post_id"]) not in seen]
        seen.update((p["ref"], p["post_id"]) for p in chunk)
        items.extend(chunk)
        has_more = bool(data.get("hasMore")) and bool(chunk)
        state = {"page": str(data.get("nextPage", "")),
                 "offset": str(data.get("nextOffset", ""))}
    return {"query": q, "total": total, "items": items[:limit],
            "count": min(len(items), limit), "pages": pages,
            "has_more": has_more or len(items) > limit,
            "tgstat_search_url": f"{base}/search"}


def links(ref: str, kind: Optional[str] = None, post_id: Optional[int] = None,
          base: str = "https://tgstat.ru") -> dict:
    """Посилання на tgstat/Telegram без жодного запиту до tgstat."""
    found_kind, ref = peer_ref(ref)
    return parse.links(kind or found_kind or "channel", ref, base, post_id)
