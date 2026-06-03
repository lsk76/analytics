"""
Lean async TeleZip Search API client (v3) — collection + channel metadata.

Only the bits the pipeline needs:
  * find_posts() — POST /Find with unique + language filter (returns all matches)
  * get_channel() — GET /Channels by id (is_channel flag + title/about for region fallback)
"""
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

MAX_RETRIES = 5


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
        }

    async def find_posts(self, query: str, date_from: datetime, date_to: datetime,
                         languages: Optional[List[str]] = None, unique: bool = True
                         ) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "searchTerm": query,
            "fromDate": date_from.isoformat(),
            "toDate": date_to.isoformat(),
        }
        if unique:
            body["unique"] = True
        if languages:
            body["languages"] = languages
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
