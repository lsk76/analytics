"""
Lean async TeleZip Search API client (v3) — collection + channel metadata.

Only the bits the pipeline needs:
  * find_posts() — POST /Find with unique + language filter (returns all matches)
  * get_channel() — GET /Channels by id (is_channel flag + title/about for region fallback)
"""
import asyncio
import logging
import weakref
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp
from django.conf import settings

logger = logging.getLogger(__name__)

MAX_RETRIES = 5

# Global cap on CONCURRENT TeleZip requests (API allows very few — default 2).
# Enforced at the client level so EVERY caller is throttled, no matter the worker.
# asyncio.Semaphore is loop-bound, so we keep one per running loop.
_sem_by_loop: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _sem_by_loop.get(loop)
    if sem is None:
        n = max(1, int(getattr(settings, "TELEZIP_MAX_CONCURRENCY", 2) or 2))
        sem = asyncio.Semaphore(n)
        _sem_by_loop[loop] = sem
    return sem


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
        # one global slot held for the whole request (incl. retries) => never more
        # than TELEZIP_MAX_CONCURRENCY connections in flight across all callers
        async with _gate():
            for attempt in range(MAX_RETRIES):
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
                    wait = 2 ** attempt
                    logger.warning("TeleZip %s attempt %d/%d failed (%s), retrying in %ds",
                                   endpoint, attempt + 1, MAX_RETRIES, e, wait)
                    await asyncio.sleep(wait)
        raise RuntimeError(f"TeleZip {endpoint} failed after {MAX_RETRIES} attempts: {last_exc}")

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
