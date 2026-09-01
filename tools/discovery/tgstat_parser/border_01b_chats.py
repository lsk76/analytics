"""Крок 1в: ЧАТИ з регіональних підбірок TGStat (tgstat.ru/tags/geo).

Чому окремо від border_01_collect.py: пошук `/channels/search` віддає лише канали,
а `/chats/search` не існує (500). Каталог чатів живе в підбірках по регіонах:
сторінка /tag/<регіон> має перемикач «каналы | чаты», який шле

    POST /tag/<регіон>/items
    _tgstat_csrk=<csrf>&peerType=chat&sortChannel=members&sortChat=members
    &categoryId=0&page=0&offset=0

Це рівно те, чого бракувало: групи, де люди пишуть. Точка (місто) тут не
задається — прив'язку до населеного пункту робить крок 2 за назвою/описом.

Запуск:  python3 -u border_01b_chats.py > chats.log 2>&1 &
         touch ~/.tgstat_go.flag        # якщо потрібен логін
Env: TGSTAT_PROFILE, TGSTAT_GO_FLAG, TGSTAT_CHATS_OUT, TGSTAT_MAX_PAGES.
"""
import asyncio
import json
import os
import re
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]
LOCAL = HERE.parent


def _path(env_key, repo_rel, local_name):
    v = os.environ.get(env_key)
    if v:
        return Path(v)
    cand = REPO.joinpath(*repo_rel)
    try:
        if cand.parent.is_dir() and os.access(cand.parent, os.R_OK | os.W_OK):
            return cand
    except OSError:
        pass
    return LOCAL / local_name


PROFILE_DIR = Path(os.environ.get("TGSTAT_PROFILE", Path.home() / ".tgstat_profile"))
GO_FLAG = Path(os.environ.get("TGSTAT_GO_FLAG", Path.home() / ".tgstat_go.flag"))
OUT_FILE = _path("TGSTAT_CHATS_OUT", ("backend", "_dir", "border_tgstat_chats.json"),
                 "border_tgstat_chats.json")
MAX_PAGES = int(os.environ.get("TGSTAT_MAX_PAGES", 40))

# Теги регіональних підбірок TGStat -> наші Region.id
TAGS = [
    ("tyumen-region", 74, "Тюменська область"),
    ("hmao-region", 82, "Ханти-Мансійський АО — Югра"),
    ("yamal-region", 84, "Ямало-Ненецький АО"),
    ("omsk-region", 58, "Омська область"),
    ("novosibirsk-region", 57, "Новосибірська область"),
    ("buratia-region", 4, "Бурятія"),
    ("altai-region", 23, "Алтайський край"),
    ("zabaikal-region", 24, "Забайкальський край"),
    ("primorsk-region", 29, "Приморський край"),
    ("altai", 2, "Алтай"),
    ("tiva-region", 18, "Тива"),
]

FETCH_JS = """
async ({tag, page, offset}) => {
    const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    const p = new URLSearchParams();
    p.set('_tgstat_csrk', csrf);
    p.set('peerType', 'chat');           // <- ключове поле
    p.set('sortChannel', 'members');
    p.set('sortChat', 'members');
    p.set('categoryId', '0');
    p.set('page', String(page));
    p.set('offset', String(offset));
    const r = await fetch(`/tag/${tag}/items`, {
        method: 'POST',
        headers: {'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                  'X-Requested-With':'XMLHttpRequest'},
        body: p.toString(),
        credentials: 'include',
    });
    if (!r.ok) return {error: r.status};
    const txt = await r.text();
    try { return JSON.parse(txt); }
    catch (e) { return {error: 'not-json', head: txt.slice(0, 200)}; }
}
"""

LINK_RE = re.compile(r"/(channel|chat)/@(\w+)")


def parse_html(html):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select('a[href*="/chat/@"], a[href*="/channel/@"]'):
        m = LINK_RE.search(a["href"])
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        card = a.find_parent("div") or a
        text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))[:200]
        out.append({"handle": m.group(2), "tgstat_kind": m.group(1), "card": text})
    return out


async def scrape(browser_page, tag):
    found, seen = [], set()
    page_num, offset = 0, 0
    while page_num < MAX_PAGES:
        data = await browser_page.evaluate(FETCH_JS,
                                           {"tag": tag, "page": page_num, "offset": offset})
        if "error" in data:
            return found, str(data["error"])
        if data.get("status") != "ok":
            return found, str(data)[:120]
        chunk = [c for c in parse_html(data.get("html", "")) if c["handle"] not in seen]
        for c in chunk:
            seen.add(c["handle"])
        found.extend(chunk)
        if not data.get("hasMore") or not chunk:
            return found, None
        page_num = data.get("nextPage", page_num + 1)
        offset = data.get("nextOffset", offset + 30)
        await asyncio.sleep(1.5)
    return found, "стеля сторінок"


async def main():
    done = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else {}
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    print(f"регіонів: {len(TAGS)}, вже зроблено: {len(done)}\nвихід: {OUT_FILE}", flush=True)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled"])
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        for tag, region_id, region in TAGS:
            if tag in done:
                continue
            # CSRF-токен береться зі сторінки самого тега, тому переходимо на неї
            await page.goto(f"https://tgstat.ru/tag/{tag}",
                            wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
            if GO_FLAG.exists():
                GO_FLAG.unlink()

            items, stop = await scrape(page, tag)
            if not items and stop:
                print(f"  {region[:26]:<28}ЗБІЙ: {stop}", flush=True)
                continue
            done[tag] = {"tag": tag, "region_id": region_id, "region": region,
                         "stop": stop, "items": items}
            OUT_FILE.write_text(json.dumps(done, ensure_ascii=False, indent=1),
                                encoding="utf-8")
            chats = sum(1 for c in items if c["tgstat_kind"] == "chat")
            print(f"  {region[:26]:<28}{len(items):>4} (чатів: {chats})"
                  + (f"  ({stop})" if stop else ""), flush=True)
            await asyncio.sleep(2)

        uniq = {c["handle"] for v in done.values() for c in v["items"]}
        print(f"\n✓ регіонів: {len(done)}, унікальних хендлів: {len(uniq)} -> {OUT_FILE}",
              flush=True)
        await ctx.close()


asyncio.run(main())
