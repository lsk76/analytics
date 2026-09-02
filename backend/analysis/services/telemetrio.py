"""
Lean async Telemetr.io Public API client (v1) — post search + channel/group messages.

Docs: https://api.tlmtr.io/rapidoc  (spec: https://api.tlmtr.io/api-docs/openapi.json)
Base: https://api.tlmtr.io   Auth: header `x-api-key: <key>`  (NOT Bearer)

WHY THIS EXISTS
    Candidate replacement / second source for TeleZip (`services/telezip.py`).
    Read `docs/telemetrio-vs-telezip.md` before assuming it is a drop-in — it is
    NOT: the query dialect, the quota model and the ID space are all different.

================================================================================
THE THREE THINGS THAT WILL BITE YOU
================================================================================
1. QUOTA IS TWO-DIMENSIONAL AND TERMS ARE THE SCARCE AXIS.
   `/v1/search/messages` bills against BOTH:
     * search_messages_requests — one per call (MEASURED on our test key: 100/mo);
     * search_terms — one per *distinct* `term` string, ever (MEASURED: 10/mo).
   Do not trust the numbers quoted when a key is issued — read /v1/usage/info.
   Searching "мобилизация" 200 times costs 200 requests but 1 term. Spending the
   term budget on one-off phrasings exhausts it for the month with no way back. Treat `term` as a non-renewable resource:
   pick the phrasings up front, run them through `TermLedger` (below), and never
   let a loop build terms dynamically.
   Channel-scoped endpoints bill a third axis, `channels` — one per *distinct*
   internal_id touched (MEASURED: 5 / month; re-reading the SAME channel later in
   the period is free). `/v1/channel/info`,
   `/v1/channels/info-batch`, `/v1/messages/channel`, `/v1/channel/stats` all
   spend it. `/v1/usage/info` is FREE and does not count — poll it, don't guess.

2. NO BOOLEAN QUERY LANGUAGE.
   `term` supports exactly one operator: double quotes for an exact phrase.
   There is no `+` (AND), no `-` (NOT), no `~N` proximity, no `*` prefix — the
   whole TOPIC ∧ ACTION − NOISE shape that makes TeleZip usable on a broad index
   (see telezip.py docstring) has no equivalent here. Narrowing is structural
   instead: `country`, `category`, `on_channel`, `date_from`/`date_to`.
   Consequence: one Telemetr.io term ≈ one TeleZip *positive* term, and the
   AND/NOT filtering has to move into our own screening stage.

3. TWO ID SPACES, AND SEARCH RESULTS HAVE NO t.me LINK.
   Everything here is keyed by Telemetr.io `internal_id` (an opaque string like
   "1B3CMw"), not the Telegram numeric id. Convert with
   `/v1/utils/resolve_telegram_id` (`resolve_telegram_id()`).
   `return_short_info=true` attaches ChatShort — title / country / language /
   members_count / verified, but NO username and NO link. The t.me permalink
   lives only on `/v1/channel/info`, which spends the channels quota. So a
   Post.url cannot be built from a search hit alone; `parse_message()` fills
   `message_url` only when you pass a `links` map (internal_id -> t.me link).

Other quirks worth knowing
  * Param names differ per endpoint for the same idea: search uses
    `return_short_info`, /v1/messages/* use `short_info`.
  * Date types differ too: search takes ISO-8601 strings (`date_from`/`date_to`),
    /v1/messages/* take INTEGER unix timestamps (`from_date`/`to_date`).
  * `/v1/search/messages` is an "!Alpha preview!" — disabled by default on a key;
    access is granted by @telemetrio_support. MEASURED 2026-09-01: a key without
    it answers HTTP 400 {"message":"Messages search is disabled"} — NOT 403 — and
    the call costs nothing (neither a request nor a term). `_get` maps that body
    to TelemetrioAccessError so callers can tell "no access" from "bad query".
  * Free/test keys see verified channels only, and only the last 7 days. MEASURED
    2026-09-01: touching an unverified chat answers HTTP 400 {"message":"For free
    you have access only for verified by Telegram or Telemetr.io chats"} — again a
    400 for what is really an access limit, so `_get` maps it to
    TelemetrioAccessError too. Note "or Telemetr.io" — their own verification is a
    second, wider whitelist than Telegram's blue check.
  * Pagination is cursor-based (`cursor` in, `cursor` in the response; null = end).
  * HTTP 426 = quota exceeded, body is QuotaLimit {name, used, limit}. Never retry it.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

MAX_RETRIES = 4          # 429 / 5xx / connection — transient, back off and retry
RETRY_STATUSES = {429, 500, 502, 503, 504}


class TelemetrioError(RuntimeError):
    """Any non-retryable API failure."""


class TelemetrioAccessError(TelemetrioError):
    """401/403 — bad key, or the endpoint is not enabled for this key.

    For /v1/search/messages a 403 most likely means the Alpha preview is not
    switched on; ask @telemetrio_support rather than retrying."""


class TelemetrioQuotaError(TelemetrioError):
    """426 — a quota axis is exhausted. `name` is one of requests /
    unique_channels / search_messages_requests / search_terms."""

    def __init__(self, name: str, used: int, limit: int):
        super().__init__(f"Telemetr.io quota '{name}' exhausted: {used}/{limit}")
        self.name, self.used, self.limit = name, used, limit


# ---------------------------------------------------------------------------
# Term ledger — local memory of which search terms we already paid for.
#
# The API reports HOW MANY unique terms are spent (/v1/usage/info) but never
# WHICH ones. Without a local record we cannot tell a free repeat from a new
# term that burns one of the five slots, so every experiment would be a gamble.
# This file is that record; it is advisory (delete it and you only lose the
# ability to reason about repeats, not the quota itself).
# ---------------------------------------------------------------------------
class TermLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._terms: Dict[str, str] = {}
        if self.path.exists():
            try:
                self._terms = json.loads(self.path.read_text("utf-8"))
            except Exception as e:  # noqa: BLE001
                logger.warning("TermLedger: unreadable %s (%s), starting empty", self.path, e)

    def is_spent(self, term: str) -> bool:
        return term in self._terms

    @property
    def terms(self) -> List[str]:
        return sorted(self._terms)

    def record(self, term: str) -> None:
        if term in self._terms:
            return
        self._terms[term] = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._terms, ensure_ascii=False, indent=1), "utf-8")
        logger.info("TermLedger: NEW term spent %r (total %d)", term, len(self._terms))


class TelemetrioClient:
    def __init__(self, api_key: str, base_url: str = "https://api.tlmtr.io",
                 timeout: int = 60):
        if not api_key:
            raise TelemetrioError("TELEMETRIO_API_KEY is empty")
        self.api_key = api_key
        self.base_url = (base_url or "https://api.tlmtr.io").rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        # Billable calls made by THIS client instance (usage/info excluded).
        # Cheap running check that a loop has not run away with the quota.
        self.requests_made = 0

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=self.timeout,
            headers={
                "x-api-key": self.api_key,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    # -- transport ----------------------------------------------------------
    async def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None,
                   billable: bool = True) -> Any:
        url = f"{self.base_url}{endpoint}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        # aiohttp serialises Python bools as "True"/"False"; the API wants json-ish
        for k, v in list(clean.items()):
            if isinstance(v, bool):
                clean[k] = "true" if v else "false"
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                async with self._session.get(url, params=clean) as resp:
                    if resp.status == 426:
                        body = await self._json_or_none(resp)
                        raise TelemetrioQuotaError(
                            (body or {}).get("name", "unknown"),
                            (body or {}).get("used", -1), (body or {}).get("limit", -1))
                    if resp.status in (401, 403):
                        raise TelemetrioAccessError(
                            f"Telemetr.io {resp.status} on {endpoint}: {(await resp.text())[:300]}")
                    if resp.status in RETRY_STATUSES:
                        raise RuntimeError(f"Telemetr.io {resp.status}")
                    if resp.status >= 400:
                        text = (await resp.text())[:300]
                        # ВИМІРЯНО 2026-09-01: вимкнений Alpha-preview пошуку
                        # віддається як 400 {"message":"Messages search is disabled"},
                        # а не як 403, попри те що це саме питання доступу.
                        # Без цієї гілки воно виглядало б як помилка запиту.
                        low = text.lower()
                        if "search is disabled" in low or "access only for verified" in low:
                            raise TelemetrioAccessError(
                                f"Telemetr.io {resp.status} on {endpoint}: {text}")
                        raise TelemetrioError(
                            f"Telemetr.io {resp.status} on {endpoint}: {text}")
                    if billable:
                        self.requests_made += 1
                    return await resp.json()
            except (TelemetrioQuotaError, TelemetrioAccessError, TelemetrioError):
                raise  # terminal: retrying only burns more quota / more 403s
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                last_exc = e
                if attempt + 1 >= MAX_RETRIES:
                    break
                wait = 2 ** attempt
                logger.warning("Telemetr.io %s attempt %d/%d failed (%s), retry in %ds",
                               endpoint, attempt + 1, MAX_RETRIES, e, wait)
                await asyncio.sleep(wait)
        raise TelemetrioError(f"Telemetr.io {endpoint} failed after {MAX_RETRIES}: {last_exc!r}")

    @staticmethod
    async def _json_or_none(resp) -> Optional[dict]:
        try:
            return await resp.json()
        except Exception:  # noqa: BLE001
            return None

    # -- account ------------------------------------------------------------
    async def usage(self) -> Dict[str, Any]:
        """/v1/usage/info — FREE (does not count towards any quota).

        Returns {status, requests, channels, search_messages_requests,
        search_terms, billing_start_date, billing_end_date} where each counter is
        {spent, limit}. Call this before and after any experiment."""
        return await self._get("/v1/usage/info", billable=False)

    # -- search -------------------------------------------------------------
    async def search_messages(
        self, term: str, *,
        period: Optional[str] = None,          # 7d|14d|30d|60d|90d|all
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        country: Optional[str] = None,
        category: Optional[str] = None,
        on_channel: Optional[str] = None,      # internal_id, restricts to one channel
        return_short_info: bool = True,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int, Optional[str]]:
        """One page of /v1/search/messages.

        `term` is plain text; wrap in double quotes for an exact phrase. There are
        no boolean operators (see module docstring #2). date_from+date_to together
        override `period`.

        Returns (messages, chats, total_count, next_cursor).
        COSTS: 1 request + (1 search term, if `term` is new to the account)."""
        params: Dict[str, Any] = {
            "term": term,
            "return_short_info": return_short_info,
            "cursor": cursor,
            "country": country,
            "category": category,
            "on_channel": on_channel,
        }
        if date_from and date_to:
            params["date_from"] = _iso(date_from)
            params["date_to"] = _iso(date_to)
        elif period:
            params["period"] = period
        data = await self._get("/v1/search/messages", params)
        return (data.get("messages") or [], data.get("chats") or [],
                int(data.get("count") or 0), data.get("cursor"))

    async def search_messages_all(
        self, term: str, *, max_pages: int = 10, **kw
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], int]:
        """Cursor-follow `search_messages` up to `max_pages`.

        max_pages is a HARD stop, not a hint: each page is a billable request and
        a runaway cursor loop is the easiest way to eat 1000 requests. When the
        cap is hit we log it loudly rather than pretend the set is complete.

        Returns (messages, chats_by_internal_id, total_count_reported_by_api)."""
        out: List[Dict[str, Any]] = []
        chats: Dict[str, Dict[str, Any]] = {}
        seen: set = set()
        cursor, total, pages = None, 0, 0
        while pages < max_pages:
            msgs, cs, count, cursor = await self.search_messages(term, cursor=cursor, **kw)
            pages += 1
            total = count or total
            for c in cs:
                chats.setdefault(c.get("internal_id"), c)
            for m in msgs:
                key = (m.get("peer_id"), m.get("message_id"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(m)
            if not cursor or not msgs:
                break
        if cursor:
            logger.warning("Telemetr.io search %r: stopped at max_pages=%d with a live "
                           "cursor — got %d of %d reported hits", term, max_pages, len(out), total)
        return out, chats, total

    # -- channel / group messages ------------------------------------------
    async def channel_messages(
        self, internal_id: str, *,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        short_info: bool = True,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
        """One page of /v1/messages/channel. NOTE the unix-timestamp dates and the
        `short_info` (not `return_short_info`) spelling.
        COSTS: 1 request + (1 unique channel, if internal_id is new to the account)."""
        data = await self._get("/v1/messages/channel", {
            "internal_id": internal_id,
            "from_date": _ts(from_date),
            "to_date": _ts(to_date),
            "short_info": short_info,
            "cursor": cursor,
        })
        return data.get("messages") or [], data.get("chats") or [], data.get("cursor")

    async def channel_messages_all(self, internal_id: str, *, max_pages: int = 20,
                                   **kw) -> List[Dict[str, Any]]:
        out, cursor, pages = [], None, 0
        while pages < max_pages:
            msgs, _chats, cursor = await self.channel_messages(internal_id, cursor=cursor, **kw)
            pages += 1
            out.extend(msgs)
            if not cursor or not msgs:
                break
        if cursor:
            logger.warning("Telemetr.io channel_messages %s: stopped at max_pages=%d "
                           "with a live cursor (%d msgs)", internal_id, max_pages, len(out))
        return out

    async def group_messages(
        self, internal_id: str, *,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        short_info: bool = True,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
        """/v1/messages/group — discussion-chat messages, i.e. the closest thing
        to the comments the monitor pipeline lives on. GroupMessage carries
        `from_peer` (sender) but, unlike TeleZip, NO reply_to / top_message_id —
        so a comment cannot be attributed to the post it hangs under."""
        data = await self._get("/v1/messages/group", {
            "internal_id": internal_id,
            "from_date": _ts(from_date),
            "to_date": _ts(to_date),
            "short_info": short_info,
            "cursor": cursor,
        })
        return data.get("messages") or [], data.get("chats") or [], data.get("cursor")

    # -- channels -----------------------------------------------------------
    async def resolve_telegram_id(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """Telegram numeric id -> {internal_id, tracked}. `tracked=false` means
        Telemetr.io knows the channel but collects no stats for it."""
        try:
            return await self._get("/v1/utils/resolve_telegram_id",
                                   {"telegram_id": int(telegram_id)})
        except TelemetrioError as e:
            logger.warning("resolve_telegram_id(%s) failed: %s", telegram_id, e)
            return None

    async def channel_info(self, internal_id: str) -> Optional[Dict[str, Any]]:
        """/v1/channel/info — the ONLY place with `link` (t.me permalink) and
        `telegram_id`. COSTS a unique-channel slot; on the free plan you get 5."""
        try:
            return await self._get("/v1/channel/info", {"internal_id": internal_id})
        except TelemetrioError as e:
            logger.warning("channel_info(%s) failed: %s", internal_id, e)
            return None

    async def group_info(self, internal_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await self._get("/v1/group/info", {"internal_id": internal_id})
        except TelemetrioError as e:
            logger.warning("group_info(%s) failed: %s", internal_id, e)
            return None

    async def search_channels(self, term: str, *, peer_type: Optional[str] = None,
                              country: Optional[str] = None, language: Optional[str] = None,
                              category: Optional[str] = None, search_in_about: bool = False,
                              limit: int = 20, skip: int = 0) -> List[Dict[str, Any]]:
        """/v1/channels/search — channel discovery by name/description/link.
        `term` also accepts @username or a t.me link. Does NOT spend search terms."""
        data = await self._get("/v1/channels/search", {
            "term": term, "peer_type": peer_type, "country": country,
            "language": language, "category": category,
            "search_in_about": search_in_about, "limit": limit, "skip": skip,
        })
        return data if isinstance(data, list) else (data.get("items") or data.get("channels") or [])

    # -- normalisation ------------------------------------------------------
    @staticmethod
    def parse_message(d: dict, chats: Optional[Dict[str, Dict[str, Any]]] = None,
                      links: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Telemetr.io message -> the dict shape TelezipClient._parse_msg emits,
        so both sources can feed one comparison/ingest path.

        `chats`  — internal_id -> ChatShort (from return_short_info), for the title.
        `links`  — internal_id -> t.me link (from channel_info), for message_url.
                   Without it `message_url` stays empty: search hits carry no
                   username, and inventing a permalink would poison Post.url."""
        peer = d.get("peer_id") or ""
        mid = d.get("message_id")
        chat = (chats or {}).get(peer) or {}
        link = (links or {}).get(peer) or ""
        url = f"{link.rstrip('/')}/{mid}" if link and mid is not None else ""
        reactions = d.get("reactions") or {}
        return {
            "mid": f"{peer}:{mid}",
            "channel_id": peer,                      # internal_id STRING, not tg int
            "channel_name": chat.get("title") or "",
            "message_id": mid,
            "message_url": url,
            "date": d.get("date"),
            "content_hash": None,                    # no server-side dedup hash here
            "content": d.get("text") or "",
            "from_user_id": _peer_id(d.get("from_peer")),   # groups only
            "from_user_name": None,                  # never returned
            "reply_to": None,                        # NOT available (see group_messages)
            "top_message_id": None,
            "has_media": bool(d.get("media")),
            "edit_date": d.get("edit_date"),
            # Telemetr.io extras TeleZip has no equivalent for:
            "views": d.get("views"),
            "forwards": d.get("forwards"),
            "comments": d.get("comments"),
            "reactions": reactions.get("count"),
            "is_ad": d.get("is_ad"),
            "deleted_at": d.get("deleted_at"),
            "members_count": chat.get("members_count"),
            "country": chat.get("country"),
            "language": chat.get("language"),
            "verified": chat.get("verified"),
        }


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _peer_id(peer: Optional[dict]) -> Optional[str]:
    if not peer:
        return None
    return peer.get("user_id") or peer.get("channel_id") or peer.get("chat_id")


def default_ledger_path() -> str:
    """Where the TermLedger lives. Overridable so a container run and a host run
    can share one file (mount it) instead of each burning its own view of reality."""
    from django.conf import settings
    return os.getenv("TELEMETRIO_TERMS_FILE",
                     str(Path(settings.BASE_DIR) / ".telemetrio_terms.json"))
