"""
Lean async TeleZip Search API client (v3) — collection + channel metadata.

Only the bits the pipeline needs:
  * find_posts() — POST /Find with unique + language filter (returns all matches)
  * get_channel() — GET /Channels by id (is_channel flag + title/about for region fallback)
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

    async def _request(self, method: str, endpoint: str, params=None, json_data=None):
        url = f"{self.base_url}{endpoint}"
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
                         ) -> List[Dict[str, Any]]:
        """Search /Find. Optionally restrict to a list of channel ids/usernames.

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
        data = await self._request("POST", "/Find", json_data=body)
        return [self._parse_msg(m) for m in data]

    async def find_posts_range(self, query: str, date_from: datetime, date_to: datetime,
                               languages: Optional[List[str]] = None, unique: bool = True,
                               channel_ids: Optional[List[int]] = None,
                               channel_names: Optional[List[str]] = None,
                               min_window: timedelta = timedelta(hours=2),
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
                                         unique, channel_ids, channel_names)
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
                                      channel_ids, channel_names, min_window),
                self.find_posts_range(query, mid, date_to, languages, unique,
                                      channel_ids, channel_names, min_window),
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
