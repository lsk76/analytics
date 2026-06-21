"""
Telethon client wrapper over TelegramAccount — auth (code flow) + enrichment.

Enrichment used by the pipeline:
  * get_message_date(url)   — reliable publish date (UTC)
  * get_channel_meta(handle)— title/description/subscribers for region fallback & cache
"""
import asyncio
import re
from typing import Optional, Tuple

from telethon import TelegramClient
from telethon.sessions import StringSession


def run_async(coro):
    """Run a coroutine from sync Django code, even if a loop already exists."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import threading
            result = {}

            def _runner():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    result["value"] = new_loop.run_until_complete(coro)
                finally:
                    new_loop.close()

            t = threading.Thread(target=_runner)
            t.start()
            t.join()
            return result.get("value")
    except RuntimeError:
        pass
    return asyncio.run(coro)


class TelegramUserClient:
    """Thin wrapper using a TelegramAccount's stored StringSession + proxy."""

    @staticmethod
    def parse_telegram_url(url: str) -> Optional[Tuple[str, int]]:
        """t.me/<handle>/<id> or t.me/c/<internal>/<id> -> (handle_or_cid, msg_id)."""
        m = re.match(r"https?://t\.me/c/(\d+)/(\d+)", url)
        if m:
            return (f"-100{m.group(1)}", int(m.group(2)))
        m = re.match(r"https?://t\.me/([A-Za-z0-9_]+)/(\d+)", url)
        if m:
            return (m.group(1), int(m.group(2)))
        return None

    @classmethod
    def _client(cls, account) -> TelegramClient:
        proxy = account.proxy.to_telethon_proxy() if account.proxy else None
        return TelegramClient(
            StringSession(account.session_string or ""),
            int(account.api_id), account.api_hash, proxy=proxy,
        )

    # ---- auth (code flow) ----
    @classmethod
    def send_code_sync(cls, account) -> dict:
        async def _run():
            client = cls._client(account)
            await client.connect()
            try:
                sent = await client.send_code_request(account.phone_number)
                return {"success": True, "phone_code_hash": sent.phone_code_hash,
                        "code_type": type(sent.type).__name__,
                        "next_type": type(sent.next_type).__name__ if sent.next_type else None,
                        "session_string": client.session.save()}
            finally:
                await client.disconnect()
        res = run_async(_run())
        if res and res.get("success"):
            account.session_string = res["session_string"]
            account.auth_code_hash = res["phone_code_hash"]
            account.save(update_fields=["session_string", "auth_code_hash"])
        return res or {"success": False}

    @classmethod
    def verify_code_sync(cls, account, code: str, password: str = None) -> dict:
        async def _run():
            client = cls._client(account)
            await client.connect()
            try:
                try:
                    await client.sign_in(account.phone_number, code,
                                         phone_code_hash=account.auth_code_hash)
                except Exception as e:
                    if "password" in str(e).lower() and password:
                        await client.sign_in(password=password)
                    else:
                        raise
                return {"success": True, "session_string": client.session.save()}
            finally:
                await client.disconnect()
        res = run_async(_run())
        if res and res.get("success"):
            account.session_string = res["session_string"]
            account.is_authenticated = True
            account.auth_code_hash = ""
            account.save(update_fields=["session_string", "is_authenticated", "auth_code_hash"])
        return res or {"success": False}

    # ---- enrichment ----
    @classmethod
    async def _with_client(cls, account, fn):
        client = cls._client(account)
        await client.connect()
        try:
            return await fn(client)
        finally:
            await client.disconnect()

    @classmethod
    async def get_message_date(cls, account, url: str):
        parsed = cls.parse_telegram_url(url)
        if not parsed:
            return None
        handle, msg_id = parsed
        async def fn(client):
            entity = int(handle) if str(handle).lstrip("-").isdigit() else handle
            msg = await client.get_messages(entity, ids=msg_id)
            from datetime import timezone
            return msg.date.astimezone(timezone.utc) if msg and msg.date else None
        return await cls._with_client(account, fn)

    @classmethod
    async def get_channel_meta(cls, account, handle: str) -> dict:
        async def fn(client):
            entity = int(handle) if str(handle).lstrip("-").isdigit() else handle
            ent = await client.get_entity(entity)
            full = None
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                full = await client(GetFullChannelRequest(ent))
            except Exception:
                pass
            return {
                "tg_id": getattr(ent, "id", None),
                "username": getattr(ent, "username", None),
                "title": getattr(ent, "title", None),
                "description": getattr(full.full_chat, "about", "") if full else "",
                "subscribers": getattr(full.full_chat, "participants_count", 0) if full else 0,
            }
        return await cls._with_client(account, fn)
