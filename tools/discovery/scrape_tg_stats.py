#!/usr/bin/env python3
"""Fetch Telegram channel subscriber count and last post date from public t.me pages."""

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = "1J8vqDCMi_PUfr_3PoO054R15Px-KYyQunBQT9VjWMTo"
CREDS_PATH = "/Users/r2d2/Downloads/tg-vk-groups-baa6d186e7f5.json"
CACHE_PATH = Path(__file__).with_name("tg_stats_cache.json")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
REQUEST_DELAY = 0.35
WORKERS = 4


def fetch(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def extract_username(link: str) -> str | None:
    link = (link or "").strip()
    m = re.search(r"(?:https?://)?t\.me/(?:s/)?([A-Za-z0-9_]{4,})", link)
    if not m:
        return None
    username = m.group(1)
    if username.lower() in {"joinchat", "addstickers", "share", "proxy", "socks"}:
        return None
    return username


def is_private_link(link: str) -> bool:
    return "/+" in link or "/joinchat/" in link


def parse_subscribers(text: str) -> int | None:
    text = text.replace("\xa0", " ").replace("&nbsp;", " ").strip()
    m = re.search(
        r"([\d\s.,]+)\s*(?:subscribers?|members?|подписчик(?:а|ов)?|участник(?:а|ов)?)",
        text,
        re.I,
    )
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(",", ".")
    if raw.endswith("K") or raw.endswith("k"):
        return int(float(raw[:-1]) * 1000)
    if raw.endswith("M") or raw.endswith("m"):
        return int(float(raw[:-1]) * 1_000_000)
    if "." in raw and raw.count(".") == 1 and len(raw.split(".")[-1]) <= 2:
        return int(float(raw))
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else None


def parse_last_post(html: str) -> str | None:
    times = re.findall(r'<time datetime="([^"]+)"', html)
    if not times:
        return None
    dt = datetime.fromisoformat(times[-1].replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%d %H:%M")


def is_telegram_bot(link: str, result: dict | None = None) -> bool:
    if result and result.get("status") == "bot":
        return True
    username = (link or "").rstrip("/").split("/")[-1]
    return username.lower().endswith("bot")


def scrape_channel(link: str) -> dict:
    if is_private_link(link):
        return {"subscribers": None, "last_post": None, "status": "private/invite"}

    username = extract_username(link)
    if not username:
        return {"subscribers": None, "last_post": None, "status": "bad_link"}

    profile_html = fetch(f"https://t.me/{username}")
    time.sleep(REQUEST_DELAY)
    preview_html = fetch(f"https://t.me/s/{username}")

    subscribers = None
    status = "ok"

    if profile_html:
        extras = re.findall(r'<div class="tgme_page_extra">([^<]+)', profile_html)
        for extra in extras:
            subs = parse_subscribers(extra)
            if subs is not None:
                subscribers = subs
                break
        if is_telegram_bot(link) and subscribers is None:
            status = "bot"
    else:
        status = "profile_error"

    last_post = parse_last_post(preview_html) if preview_html else None
    if last_post is None and status == "ok":
        status = "no_posts"

    return {"subscribers": subscribers, "last_post": last_post, "status": status}


def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    creds = Credentials.from_service_account_file(
        CREDS_PATH, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    sheet = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1
    data = sheet.get_all_values()
    headers = data[0]

    link_idx = headers.index("Посилання на канал")
    type_idx = headers.index("Тип")
    subs_idx = headers.index("Підписники")

    if "Останній пост" in headers:
        last_post_idx = headers.index("Останній пост")
    else:
        last_post_idx = subs_idx + 1
        headers.insert(last_post_idx, "Останній пост")

    rows_to_scrape: list[tuple[int, str, str]] = []
    for i, row in enumerate(data[1:], start=2):
        row = row + [""] * (len(headers) - len(row))
        typ = row[type_idx].strip().lower()
        link = row[link_idx].strip()
        if typ == "telegram" and "t.me/" in link:
            rows_to_scrape.append((i, link, row[0][:60]))

    cache = load_cache()

    # Auto-mark bots in sheet data before scrape/update.
    for row in data[1:]:
        if len(row) <= type_idx:
            continue
        link = row[link_idx].strip() if len(row) > link_idx else ""
        typ = row[type_idx].strip().lower() if len(row) > type_idx else ""
        if typ == "telegram" and is_telegram_bot(link, cache.get(link)):
            row[type_idx] = "telegram_bot"
    print(f"Telegram channels to scrape: {len(rows_to_scrape)}")

    def job(item: tuple[int, str, str]) -> tuple[int, str, dict]:
        row_num, link, _ = item
        cached = cache.get(link)
        if cached and cached.get("subscribers") is not None:
            return row_num, link, cached
        if cached and cached.get("status") in {"private/invite", "bad_link", "bot"}:
            return row_num, link, cached
        result = scrape_channel(link)
        cache[link] = result
        save_cache(cache)
        return row_num, link, result

    results: dict[int, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(job, item) for item in rows_to_scrape]
        for fut in as_completed(futures):
            row_num, link, result = fut.result()
            results[row_num] = result
            done += 1
            if done % 20 == 0 or done == len(rows_to_scrape):
                print(f"  {done}/{len(rows_to_scrape)} — last: {link[:50]} -> {result}")

    # Build update grid
    updates: list[list[str]] = []
    for row in data[1:]:
        row = row + [""] * (len(headers) - len(row))
        link = row[link_idx].strip() if len(row) > link_idx else ""
        typ = row[type_idx].strip().lower() if len(row) > type_idx else ""
        result = cache.get(link, {}) if typ == "telegram" and "t.me/" in link else {}

        subs = result.get("subscribers")
        last_post = result.get("last_post") or ""

        if typ == "telegram" and "t.me/" in link:
            row[subs_idx] = str(subs) if subs is not None else ""
            row[last_post_idx] = last_post
        elif typ == "telegram_bot":
            pass
        else:
            if len(row) <= last_post_idx:
                row.extend([""] * (last_post_idx + 1 - len(row)))
            row[last_post_idx] = row[last_post_idx] if len(row) > last_post_idx else ""

        updates.append(row[: len(headers)])

    end_cell = gspread.utils.rowcol_to_a1(len(updates) + 1, len(headers))
    sheet.update(values=[headers] + updates, range_name=f"A1:{end_cell}", value_input_option="RAW")

    ok = sum(1 for r in cache.values() if r.get("subscribers") is not None)
    posts = sum(1 for r in cache.values() if r.get("last_post"))
    private = sum(1 for r in cache.values() if r.get("status") == "private/invite")
    print(f"\nDone. Subscribers: {ok}, last post: {posts}, private/invite: {private}")
    print(f"Cache: {CACHE_PATH}")


if __name__ == "__main__":
    main()
