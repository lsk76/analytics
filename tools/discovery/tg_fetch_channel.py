#!/usr/bin/env python3
"""Fetch one channel via authorized Telegram account."""

import asyncio
import json
import sys

from tg_client import fetch_entity_info, get_client


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python tg_fetch_channel.py https://t.me/channel_or_invite")

    link = sys.argv[1]
    client = get_client()
    await client.connect()

    if not await client.is_user_authorized():
        raise SystemExit("Не авторизовано. Спочатку: python tg_auth.py")

    info = await fetch_entity_info(client, link)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
