"""
Lean async TeleZip Search API client (v3) — collection + channel metadata.

Only the bits the pipeline needs:
  * find_posts() — POST /Find with unique + language filter (returns all matches)
  * get_channel() — GET /Channels by id (is_channel flag + title/about for region fallback)

================================================================================
QUERY SYNTAX (the `searchTerm` string / AnalysisTask.telezip_query)
================================================================================
`searchTerm` uses the `/find text=` dialect of the TeleZip bot (NOT the `/messages`
site dialect, whose operators are inverted). Source: the official "TeleZip v2"
guide + the live, battle-tested query on task=1 (`ethnic-clashes`). Verified
2026-08-10.

Operators (default operator between terms is OR):
  space | `|`  OR   — `дрон FPV` == `дрон | FPV`   → дрон OR FPV (default when none)
  `+`          AND  — `+термін` / `+(a b)`          → term/group REQUIRED (ANDed in)
  `-`          NOT  — `-mavic` / `-"сектор газа"`   → exclude; write it ATTACHED, no space
  ( … )             — grouping / precedence: `дрон +(тасс -риа)`
  word              — matches ALL word-forms/declensions of the lemma, no wildcard:
                      `дрон` → дрон/дрона/дрону/дронах; `депутат` → депутата/депутатов/…
                      (so listing declensions by hand is redundant).
  слово*            — PREFIX search — `дрон*` → дрон, дронников (any continuation).
                      Use for word FAMILIES across parts of speech:
                      `мобилиз*` → мобилизация / мобилизационный / мобилизовать.
                      A bare stem like `мобилизаци` (task=1 style) is NOT a documented
                      form — prefer a real word (declensions) or `*` (prefix).
  "фраза"           — phrase; word-forms STILL apply — `"дрон FPV"` hits «дроны-FPV».
  "a b"~N           — proximity: a & b within N words, any order; write `~N` with NO space.
  exact="…"         — literal, NO declensions (abbreviations / model codes, e.g. "сво").
                      NOTE: this lean client sends only `searchTerm` (text= mode); it does
                      NOT send exact=/channeltext=/regex=. Add them upstream if needed.

Proven shape (task=1) — TOPIC ∧ ACTION, minus noise:
  (мигрант диаспora "лицо кавказской" …) +(драка избил напал "с ножом" …) -(всу фронт военкомат …)
  ⇒ (any topic term) AND (any action term) NOT (any excluded term). AND-logic is
  what cuts keyword floods (task=1 pilot: 11 404 → 367 candidates).

Rejection ("відлуп" — returns empty / errors) when a search:
  * runs > 3 min, or  * matches > 300 000 messages, or  * hits > 10 000 channels,
  * or has a malformed query.
  → NARROW it: chunk the window by day (find_posts_range already halves heavy
    windows), add a `+(…)` required group, or quote phrases. A bare topic term
    over a long window across the whole index will trip this.

Caveats
  * Negation `-(…)` over a broad window is SLOW (500/timeout/429, ~68× slower) —
    prefer positive `+(…)` filters and let the classifier drop the rest.
  * `unique=true` (the `unique=` kwarg) dedups reposts of the same message.
  * `languages=["ru"]` restricts to Russian; empty = no language filter.
  * No `channelIds` here ⇒ the query runs over the WHOLE index (all channels).
  Full guide: https://docs.google.com/document/d/1oKag8XfmpOnKapbkayRZzq8JHbpB8GaKvSgC1judnvg
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone as djtz

logger = logging.getLogger(__name__)

MAX_RETRIES = 5      # 429 / connection / DNS — flaky, worth retrying with backoff
MAX_RETRIES_5XX = 1  # 500 / timeout = window too heavy → NO retry, bail at once
                     # so find_posts_range splits the window immediately


def _is_overload_error(e: Exception) -> bool:
    """True only for 'window too heavy' signals (5xx / internal search timeout),
    which SPLITTING the time-window can fix. 429 (rate-limit) is excluded —
    splitting it just makes more requests; that's for the caller's backoff."""
    s = str(e).lower()
    if "429" in s:
        return False
    return ("telezip 5" in s            # TeleZip 500/502/503/504
            or "timeout" in s
            or "timed out" in s
            or "context canceled" in s)

# CROSS-PROCESS cap on concurrent TeleZip requests. Backed by N rows in
# analysis_telezip_slot (lease table) so the limit holds across EVERY worker
# process — a per-process asyncio.Semaphore let each extra process add its own
# slots, which is exactly what tripped TeleZip 429s. Crash-safe via lease expiry.
SLOT_LEASE = 180        # seconds before a dead holder's slot is reclaimed
_slots_seeded = False


def _slot_count() -> int:
    return max(1, int(getattr(settings, "TELEZIP_MAX_CONCURRENCY", 2) or 2))


def _ensure_slots_sync() -> None:
    global _slots_seeded
    from analysis.models import TelezipSlot
    # The slot TABLE is the source of truth for the global cap (so it can be
    # tuned live — e.g. drop to 1 row during a throttle — and survive restarts).
    # Only seed from the setting when the table is still empty.
    if not TelezipSlot.objects.exists():
        for i in range(_slot_count()):
            TelezipSlot.objects.get_or_create(slot=i)
    _slots_seeded = True


@transaction.atomic
def _claim_slot_sync():
    from analysis.models import TelezipSlot
    now = djtz.now()
    row = (TelezipSlot.objects.select_for_update(skip_locked=True)
           .filter(Q(leased_until__isnull=True) | Q(leased_until__lt=now))
           .order_by("slot").first())
    if row is None:
        return None
    row.leased_until = now + timedelta(seconds=SLOT_LEASE)
    row.save(update_fields=["leased_until"])
    return row.slot


def _renew_slot_sync(slot: int) -> None:
    from analysis.models import TelezipSlot
    TelezipSlot.objects.filter(slot=slot).update(
        leased_until=djtz.now() + timedelta(seconds=SLOT_LEASE))


def _release_slot_sync(slot: int) -> None:
    from analysis.models import TelezipSlot
    TelezipSlot.objects.filter(slot=slot).update(leased_until=None)


async def _acquire_slot() -> int:
    """Block until a global TeleZip slot is free, then return its id."""
    if not _slots_seeded:
        await sync_to_async(_ensure_slots_sync)()
    while True:
        slot = await sync_to_async(_claim_slot_sync)()
        if slot is not None:
            return slot
        await asyncio.sleep(0.3)


class TelezipClient:
    def __init__(self, api_key: str, base_url: str, timeout: int = 180):
        self.api_key = api_key
        self.base_url = (base_url or "https://api.telezip.net/v3").rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json",
            },
        )
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    def _url(self, endpoint: str) -> str:
        """v3-ендпоінти висять на base_url (…/v3), v4 — на корені хоста."""
        if endpoint.startswith("/v4/"):
            root = self.base_url.rsplit("/v3", 1)[0] if self.base_url.endswith("/v3") \
                else self.base_url
            return f"{root}{endpoint}"
        return f"{self.base_url}{endpoint}"

    async def _request(self, method: str, endpoint: str, params=None, json_data=None):
        url = self._url(endpoint)
        last_exc = None
        # hold one GLOBAL slot for the whole request (incl. retries) => never more
        # than TELEZIP_MAX_CONCURRENCY in flight across ALL processes
        slot = await _acquire_slot()
        try:
            for attempt in range(MAX_RETRIES):
                await sync_to_async(_renew_slot_sync)(slot)
                try:
                    async with self._session.request(method, url, params=params, json=json_data) as resp:
                        if resp.status >= 500 or resp.status == 429:
                            raise RuntimeError(f"TeleZip {resp.status}")
                        if resp.status >= 400:
                            text = await resp.text()
                            raise RuntimeError(f"TeleZip {resp.status}: {text[:300]}")
                        return await resp.json()
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                    last_exc = e
                    # 4xx (крім 429) — відповідь сервера «так не можна»: пустий
                    # юзернейм, чужий ендпоінт, немає прав. Повтор нічого не
                    # змінить, лише зжере квоту й 15 с чекання в інтерактиві.
                    if isinstance(e, RuntimeError) and str(e).startswith("TeleZip 4") \
                            and "429" not in str(e):
                        break
                    # 500/timeout = window too heavy → fail fast (1 retry) so
                    # find_posts_range can split it; 429/connection keep full budget.
                    overload = isinstance(e, asyncio.TimeoutError) or \
                        (isinstance(e, RuntimeError) and str(e).startswith("TeleZip 5"))
                    limit = MAX_RETRIES_5XX if overload else MAX_RETRIES
                    if attempt + 1 >= limit:
                        break
                    wait = 2 ** attempt
                    logger.warning("TeleZip %s attempt %d/%d failed (%s), retrying in %ds",
                                   endpoint, attempt + 1, limit, e, wait)
                    await asyncio.sleep(wait)
        finally:
            await sync_to_async(_release_slot_sync)(slot)
        # repr (not str): asyncio.TimeoutError stringifies to "" — repr keeps the
        # type name so find_posts_range's _is_overload_error can detect the timeout
        # and split the window instead of failing the whole chunk.
        raise RuntimeError(f"TeleZip {endpoint} failed after {attempt + 1} attempts: {last_exc!r}")

    @staticmethod
    def _parse_msg(d: dict) -> Dict[str, Any]:
        date_str = d.get("date") or d.get("Date")
        return {
            "mid": d.get("mid") or d.get("MID"),
            "channel_id": d.get("channelId") or d.get("ChannelId"),
            "channel_name": d.get("channelName") or d.get("ChannelName"),
            "message_id": d.get("messageId") or d.get("MessageId"),
            "message_url": d.get("messageUrl") or d.get("MessageUrl"),
            "date": date_str,
            "content_hash": d.get("contentHash") or d.get("ContentHash"),
            "content": d.get("content") or d.get("Content"),
            # opinion-monitor: author + reply context
            "from_user_id":   d.get("fromUserId") or d.get("FromUserId"),
            "from_user_name": d.get("fromUserName") or d.get("FromUserName"),
            "reply_to":       d.get("replyTo") or d.get("ReplyTo"),
            "top_message_id": d.get("topMessageId") or d.get("TopMessageId"),
            "has_media":      d.get("hasMedia") if "hasMedia" in d else d.get("HasMedia"),
            "edit_date":      d.get("editDate") or d.get("EditDate"),
        }

    async def find_posts(self, query: str, date_from: datetime, date_to: datetime,
                         languages: Optional[List[str]] = None, unique: bool = True,
                         channel_ids: Optional[List[int]] = None,
                         channel_names: Optional[List[str]] = None,
                         extra_body: Optional[Dict[str, Any]] = None,
                         ) -> List[Dict[str, Any]]:
        """Search /Find. Optionally restrict to a list of channel ids/usernames.

        `query` uses the `/find text=` syntax — see this module's docstring
        (space=OR, +=AND, -=NOT, "phrase", declension/prefix matching, limits).

        TeleZip API quirks (probed 2026-06):
          * `channelIds`   — array of int, case-insensitive key alias `ChannelIds`.
          * `channelNames` — array of str (usernames, no @).
          * `channelId` / `channel:foo` inline / `channels:` are SILENTLY IGNORED.
          * Internal search timeout ~kicks in around 6h windows for broad terms;
            chunk callers to ~1h slices.
        """
        body: Dict[str, Any] = {
            "searchTerm": query,
            "fromDate": date_from.isoformat(),
            "toDate": date_to.isoformat(),
        }
        if unique:
            body["unique"] = True
        if languages:
            body["languages"] = languages
        if channel_ids:
            body["channelIds"] = list(channel_ids)
        if channel_names:
            body["channelNames"] = list(channel_names)
        if extra_body:
            # Режими запиту понад text= (exact=/channeltext=/regex= у діалекті бота)
            # документовані, але НЕ перевірені на цьому API. Тому не вигадуємо
            # іменовані параметри, а даємо прокинути перевірене розвідкою
            # (`tz_probe`) поле як є — і одразу бачимо відповідь сервера.
            body.update(extra_body)
        data = await self._request("POST", "/Find", json_data=body)
        return [self._parse_msg(m) for m in data]

    async def find_posts_range(self, query: str, date_from: datetime, date_to: datetime,
                               languages: Optional[List[str]] = None, unique: bool = True,
                               channel_ids: Optional[List[int]] = None,
                               channel_names: Optional[List[str]] = None,
                               min_window: timedelta = timedelta(hours=2),
                               extra_body: Optional[Dict[str, Any]] = None,
                               ) -> List[Dict[str, Any]]:
        """Adaptive-window /Find. Try the WHOLE [date_from, date_to] first; on a
        500 / internal-search-timeout (window too heavy — the broad negation query
        over a busy chat-day overruns TeleZip's ~6h search budget) split the window
        in half and recurse on each half. Halving repeats (→4→8…) until every piece
        succeeds or a window would drop below `min_window` (default 2h), which is the
        floor. 429 is NOT split (propagated for the caller's backoff). Results from
        all sub-windows are concatenated and de-duped by message_url.

        Light days cost ONE request (full window OK); only heavy days fan out, and
        only as deep as they must — so request count stays low (kinder to rate limits)
        while no single request is ever too heavy."""
        try:
            return await self.find_posts(query, date_from, date_to, languages,
                                         unique, channel_ids, channel_names, extra_body)
        except RuntimeError as e:
            span = date_to - date_from
            # Don't split a 429, and never produce a window < min_window (so we only
            # halve while both resulting halves stay >= the 4h floor).
            if not _is_overload_error(e) or span < 2 * min_window:
                raise
            mid = date_from + span / 2
            logger.info("TeleZip range: %s heavy, splitting %s..%s at %s",
                        e, date_from.isoformat(), date_to.isoformat(), mid.isoformat())
            halves = await asyncio.gather(
                self.find_posts_range(query, date_from, mid, languages, unique,
                                      channel_ids, channel_names, min_window, extra_body),
                self.find_posts_range(query, mid, date_to, languages, unique,
                                      channel_ids, channel_names, min_window, extra_body),
            )
            out: List[Dict[str, Any]] = []
            seen: set = set()
            for batch in halves:
                for r in batch:
                    u = r.get("message_url")
                    if u and u in seen:
                        continue
                    if u:
                        seen.add(u)
                    out.append(r)
            return out

    async def get_channel(self, channel_id: int) -> Optional[Dict[str, Any]]:
        try:
            data = await self._request("GET", "/Channels", params={"id": channel_id})
        except Exception as e:  # noqa: BLE001
            logger.warning("get_channel(%s) failed: %s", channel_id, e)
            return None
        if not data:
            return None
        c = data[0]
        return {
            "tg_id": c.get("Id") or c.get("id"),
            "username": c.get("Name") or c.get("name") or "",
            "title": c.get("Title") or c.get("title") or "",
            "about": c.get("About") or c.get("about") or "",
            "subscribers": c.get("UserCount") or c.get("userCount") or 0,
            "language": c.get("Language") or c.get("language") or "",
            "is_channel": c.get("IsChannel") if "IsChannel" in c else c.get("isChannel"),
        }

    async def raw(self, method: str, endpoint: str,
                  params: Optional[Dict[str, Any]] = None,
                  json_data: Optional[Dict[str, Any]] = None) -> Any:
        """Сирий виклик довільного ендпоінта — розвідка API (`tz_probe`).

        Офіційний гайд лежить за Google-логіном, а клієнт покриває лише
        перевірені `/Find` і `/Channels`. Замість того щоб ВИГАДУВАТИ решту
        сигнатур, даємо спитати сам сервер і побачити сиру відповідь.
        """
        return await self._request(method.upper(), endpoint, params=params,
                                   json_data=json_data)

    async def find_channel_by_name(self, username: str, days: int = 7
                                   ) -> Optional[Dict[str, Any]]:
        """Картка каналу за @юзернеймом.

        `/Channels` шукає лише за числовим id, тож username спершу зводимо до id
        через `/Find` з `channelNames` (той самий фільтр, яким збирає конвеєр):
        будь-який свіжий пост каналу несе `channelId`.
        """
        now = djtz.now()
        rows = await self.find_posts("*", now - timedelta(days=days), now,
                                     unique=True,
                                     channel_names=[username.lstrip("@")])
        cid = next((r.get("channel_id") for r in rows if r.get("channel_id")), None)
        if not cid:
            return None
        meta = await self.get_channel(cid)
        if meta:
            meta["recent_posts"] = len(rows)
        return meta

    # ------------------------------------------------------------------ v4 API
    # Повний пошуковий контракт (див. docs/telezip-api.md): усі режими запиту
    # (text/exact/regex/опис каналу), фільтри автора, каналу, тегів і медіа,
    # пагінація й семплювання. Конвеєр лишається на v3 /Find — тут працює
    # операторська розвідка, якій потрібні ліміти й сторінки.

    @staticmethod
    def build_criteria(*, date_from=None, date_to=None, term: str = "",
                       exact: str = "", regex: str = "", channel_term: str = "",
                       channel_ids=None, channel_names=None,
                       user_ids=None, user_names=None,
                       languages=None, required_tags=None, excluded_tags=None,
                       has_media=None, unique=None, source: str = "",
                       top_message_id=None, extra=None) -> Dict[str, Any]:
        """Тіло запиту v4 із «людських» аргументів (порожні поля не шлемо)."""
        body: Dict[str, Any] = {}
        if date_from is not None:
            body["fromDate"] = date_from.isoformat()
        if date_to is not None:
            body["toDate"] = date_to.isoformat()
        if term:
            body["searchTerm"] = term
        if exact:
            body["exactTerm"] = exact
        if regex:
            body["regexPattern"] = regex
        if channel_term:
            body["channelTerm"] = channel_term
        if channel_ids:
            body["channelIds"] = list(channel_ids)
        if channel_names:
            body["channelNames"] = list(channel_names)
        if user_ids:
            body["fromUserId"] = list(user_ids)
        if user_names:
            body["fromUserName"] = list(user_names)
        if languages:
            body["languages"] = list(languages)
        if required_tags:
            body["requiredTags"] = list(required_tags)
        if excluded_tags:
            body["excludedTags"] = list(excluded_tags)
        if has_media is not None:
            body["hasMedia"] = bool(has_media)
        if unique is not None:
            body["unique"] = bool(unique)
        if source:
            body["source"] = source
        if top_message_id:
            body["topMessageId"] = int(top_message_id)
        if extra:
            body.update(extra)
        return body

    async def search(self, criteria: Dict[str, Any], limit: int = 0,
                     page_size: int = 0, page_token: str = "",
                     sample_only: bool = False, group_by_channel: bool = False
                     ) -> Dict[str, Any]:
        """POST /v4/messages — пошук із лімітом або пагінацією.

        `limit` і `pageSize` взаємовиключні (так вимагає API). Повертає
        {total, next_page_token, messages[]} у нормалізованому вигляді.
        """
        body = dict(criteria)
        if page_size:
            body["pageSize"] = int(page_size)
            if page_token:
                body["pageToken"] = page_token
        elif limit:
            body["limit"] = int(limit)
            if sample_only:
                body["sampleOnly"] = True
        if group_by_channel:
            body["groupByChannel"] = True
        data = await self._request("POST", "/v4/messages", json_data=body)
        return {
            "total": data.get("totalMessages", 0),
            "next_page_token": data.get("nextPageToken"),
            "messages": [self._parse_msg(m) for m in (data.get("messages") or [])],
        }

    async def search_stats(self, criteria: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v4/messages/stats — скільки/де/коли БЕЗ викачування повідомлень."""
        return await self._request("POST", "/v4/messages/stats", json_data=dict(criteria))

    async def search_channels(self, *, channel_ids=None, channel_names=None,
                              title: str = "", about: str = "", channel_term: str = "",
                              source: str = "", page_size: int = 0,
                              page_token: str = "") -> Dict[str, Any]:
        """GET /v4/channels — пошук каналів за id/іменем/назвою/описом."""
        params: List[tuple] = []
        for cid in (channel_ids or []):
            params.append(("channelIds", int(cid)))
        for name in (channel_names or []):
            params.append(("channelNames", name))
        if title:
            params.append(("title", title))
        if about:
            params.append(("about", about))
        if channel_term:
            params.append(("channelTerm", channel_term))
        if source:
            params.append(("source", source))
        if page_size:
            params.append(("pageSize", int(page_size)))
        if page_token:
            params.append(("pageToken", page_token))
        return await self._request("GET", "/v4/channels", params=params)

    async def search_users(self, *, user_ids=None, usernames=None, term: str = "",
                           is_bot=None, is_active=None, page_size: int = 20,
                           page_token: str = "") -> Dict[str, Any]:
        """GET /v4/users — профілі юзерів за id/іменем/вільним текстом."""
        params: List[tuple] = [("pageSize", int(page_size))]
        for uid in (user_ids or []):
            params.append(("userIds", int(uid)))
        for name in (usernames or []):
            params.append(("usernames", name))
        if term:
            params.append(("userTerm", term))
        if is_bot is not None:
            params.append(("isBot", str(bool(is_bot)).lower()))
        if is_active is not None:
            params.append(("isActive", str(bool(is_active)).lower()))
        if page_token:
            params.append(("pageToken", page_token))
        return await self._request("GET", "/v4/users", params=params)

    async def users_by_username(self, usernames) -> Dict[str, Any]:
        """GET /v4/users/by-username — юзернейм → TelegramID (масово)."""
        params = [("username", u.lstrip("@")) for u in usernames]
        return await self._request("GET", "/v4/users/by-username", params=params)

    async def message_context(self, channel_id: int, message_id: int,
                              anchor_date: str = "", before: int = 20,
                              after: int = 20) -> Dict[str, Any]:
        """GET /v4/messages/context — сусідні повідомлення навколо знайденого."""
        params: List[tuple] = [("channelId", int(channel_id)),
                               ("messageId", int(message_id)),
                               ("before", max(0, min(int(before), 100))),
                               ("after", max(0, min(int(after), 100)))]
        if anchor_date:
            params.append(("anchorDate", anchor_date))
        return await self._request("GET", "/v4/messages/context", params=params)

    async def index_stats(self) -> Dict[str, Any]:
        """GET /v4/stats — розмір індексу, ЛАГ індексації і ГЛИБИНА пошуку."""
        return await self._request("GET", "/v4/stats")

    async def search_macros(self) -> List[Dict[str, Any]]:
        """GET /SearchMacros — серверні макроси (##ім'я → готовий підзапит)."""
        return await self._request("GET", "/SearchMacros")
