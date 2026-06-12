#!/usr/bin/env python3
"""Telegram user-client helpers (Telethon)."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel, Chat, ChatInvite, ChatInviteAlready, User

ROOT = Path(__file__).resolve().parent

# SECRETS LIVE OUTSIDE THE REPO. Copying a .session/.env into the project tree
# trips the harness secret-scanner and locks the whole project, so the Telethon
# session + API creds are kept in ~/tg-secrets (override with TG_SECRETS_DIR).
SECRETS_DIR = Path(os.environ.get("TG_SECRETS_DIR", Path.home() / "tg-secrets")).expanduser()
load_dotenv(SECRETS_DIR / ".env")
# allow a local .env for NON-secret overrides only (never put creds/session here)
load_dotenv(ROOT / ".env")

API_ID = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")
SESSION_NAME = os.getenv("TG_SESSION", "analyzer")
SESSION_PATH = str(SECRETS_DIR / SESSION_NAME)


async def call_with_flood_wait(coro_fn):
    """Retry Telegram request after FloodWait."""
    while True:
        try:
            return await coro_fn()
        except FloodWaitError as exc:
            wait = exc.seconds + 1
            print(f"Telegram FloodWait: чекаємо {wait} с...")
            await asyncio.sleep(wait)


def get_client() -> TelegramClient:
    if not API_ID or not API_HASH:
        raise SystemExit(
            f"TG_API_ID / TG_API_HASH не задані (шукав у {SECRETS_DIR}/.env).\n"
            "Секрети тримаємо ПОЗА репо. Створіть ~/tg-secrets/.env з\n"
            "  TG_API_ID, TG_API_HASH (https://my.telegram.org/apps),\n"
            "  TG_SESSION=analyzer\n"
            "і покладіть поряд авторизований analyzer.session.\n"
            "Інший шлях — env-змінна TG_SECRETS_DIR=/шлях/до/секретів."
        )
    return TelegramClient(SESSION_PATH, int(API_ID), API_HASH)


def normalize_link(link: str) -> str:
    link = (link or "").strip()
    if link.startswith("t.me/"):
        return "https://" + link
    return link


def invite_hash(link: str) -> str | None:
    m = re.search(r"t\.me/\+([A-Za-z0-9_-]+)", normalize_link(link))
    return m.group(1) if m else None


def public_username(link: str) -> str | None:
    m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]{4,})", normalize_link(link))
    if not m:
        return None
    username = m.group(1)
    if username.lower() in {"joinchat", "addstickers", "share", "proxy", "socks"}:
        return None
    return username


async def fetch_entity_info(client: TelegramClient, link: str) -> dict:
    """Return channel/chat stats using an authorized Telegram account."""
    link = normalize_link(link)
    result: dict = {
        "input_link": link,
        "title": None,
        "username": None,
        "resolved_link": None,
        "subscribers": None,
        "last_post": None,
        "entity_type": None,
        "status": "ok",
    }

    try:
        if invite := invite_hash(link):
            try:
                invite_info = await call_with_flood_wait(
                    lambda: client(CheckChatInviteRequest(invite))
                )
            except (InviteHashInvalidError, InviteHashExpiredError) as exc:
                result["status"] = "invite_invalid"
                result["error"] = str(exc)
                return result

            if isinstance(invite_info, ChatInviteAlready):
                entity = invite_info.chat
            elif isinstance(invite_info, ChatInvite):
                result["title"] = invite_info.title
                result["subscribers"] = invite_info.participants_count
                result["entity_type"] = "invite_preview"
                result["status"] = "invite_preview_only"
                return result
            else:
                entity = getattr(invite_info, "chat", None)
                if entity is None:
                    result["status"] = "invite_preview_only"
                    return result
        else:
            username = public_username(link)
            if not username:
                result["status"] = "bad_link"
                return result
            entity = await call_with_flood_wait(lambda: client.get_entity(username))
            result["resolved_link"] = f"https://t.me/{username}"

        if isinstance(entity, Channel):
            full = await client(GetFullChannelRequest(entity))
            channel = full.chats[0]
            result["title"] = channel.title
            result["username"] = channel.username
            result["entity_type"] = "channel" if channel.broadcast else "supergroup"
            result["subscribers"] = full.full_chat.participants_count
            if channel.username:
                result["resolved_link"] = f"https://t.me/{channel.username}"

            messages = await client.get_messages(channel, limit=1)
            if messages:
                dt = messages[0].date.astimezone(timezone.utc)
                result["last_post"] = dt.strftime("%Y-%m-%d %H:%M")
        elif isinstance(entity, Chat):
            full = await client.get_entity(entity)
            result["title"] = full.title
            result["entity_type"] = "group"
            result["subscribers"] = getattr(full, "participants_count", None)
        elif isinstance(entity, User):
            result["title"] = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
            result["entity_type"] = "user"
            result["status"] = "not_a_channel"
        else:
            result["status"] = "unknown_entity"

    except ChannelPrivateError:
        result["status"] = "private"
    except (UsernameInvalidError, UsernameNotOccupiedError, ChannelInvalidError, ValueError) as exc:
        result["status"] = "not_found"
        result["error"] = str(exc)

    return result
