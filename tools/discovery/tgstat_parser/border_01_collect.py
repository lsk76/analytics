"""Крок 1 задачі «прикордонні чати»: збір хендлів із TGStat по 47 точках.

Відмінності від 01_collect_handles.py (той лишається як є, під регіони):
  * запит — не назва республіки, а НАСЕЛЕНИЙ ПУНКТ із сітки
    backend/analysis/fixtures/border_points.json (міста + прикордонні райцентри);
  * пороги на точку: для села на 3-14 тис. `participantsCountFrom=800` відсікає все,
    тому поріг береться з поля `min_subs` (100-800);
  * `inAbout=1` для малих точок — «Подслушано» часто без назви міста в заголовку;
  * шукаємо не лише канали, а й ЧАТИ (`/chats/search`); якщо TGStat такого розділу
    не віддає — скрипт це фіксує й далі йде лише по каналах;
  * профіль браузера НЕ в /tmp — інакше його стирає кожне перезавантаження;
  * ідемпотентний: уже зібрані (точка, запит, розділ) пропускаються, тож прогін
    можна зупиняти й продовжувати.

Сесія TGStat прив'язана до IP і TLS-fingerprint браузера, тому запити йдуть із
самої сторінки (fetch + CSRF-токен), а логін робиться руками один раз.

Запуск:
    python3 -u border_01_collect.py > collect.log 2>&1 &
    # відкриється Chrome — залогінитись на tgstat.ru, потім:
    touch ~/.tgstat_go.flag

Env: TGSTAT_PROFILE (дефолт ~/.tgstat_profile), TGSTAT_OUT, TGSTAT_GO_FLAG,
     TGSTAT_POINTS_FILE,
     TGSTAT_POINTS (фільтр: кома-перелік точок), TGSTAT_MAX_PAGES.
"""
import asyncio
import json
import os
import re
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve()
# Скрипт має працювати і з репозиторію, і з робочої копії поза ним: у ~/Documents
# діє TCC — термінал користувача не може читати звідти файли ("Operation not
# permitted"), тому робоча копія живе в ~/tgstat-border. Порядок пошуку шляхів:
# env -> репозиторій (якщо доступний) -> тека самого скрипта.
REPO = HERE.parents[3]
LOCAL = HERE.parent


def _path(env_key, repo_rel, local_name, must_exist=False):
    v = os.environ.get(env_key)
    if v:
        return Path(v)
    cand = REPO.joinpath(*repo_rel)
    try:
        if must_exist:
            if cand.is_file():
                cand.open("rb").close()
                return cand
        elif cand.parent.is_dir() and os.access(cand.parent, os.R_OK | os.W_OK):
            return cand
    except OSError:
        pass
    return LOCAL / local_name

POINTS_FILE = _path("TGSTAT_POINTS_FILE",
                    ("backend", "analysis", "fixtures", "border_points.json"),
                    "border_points.json", must_exist=True)
PROFILE_DIR = Path(os.environ.get("TGSTAT_PROFILE", Path.home() / ".tgstat_profile"))
GO_FLAG = Path(os.environ.get("TGSTAT_GO_FLAG", Path.home() / ".tgstat_go.flag"))
OUT_FILE = _path("TGSTAT_OUT", ("backend", "_dir", "border_tgstat_raw.json"),
                 "border_tgstat_raw.json")
MAX_PAGES = int(os.environ.get("TGSTAT_MAX_PAGES", 40))
ONLY = [p.strip() for p in os.environ.get("TGSTAT_POINTS", "").split(",") if p.strip()]

SECTIONS = [("channel", "/channels/search"), ("chat", "/chats/search")]
CHAT_FAIL_LIMIT = 3   # стільки збоїв поспіль -> розділ чатів вимикається

FETCH_JS = """
async ({endpoint, query, minSubs, inAbout, page, offset}) => {
    const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    const p = new URLSearchParams();
    p.set('_tgstat_csrk', csrf);
    p.set('view', 'list');
    p.set('sort', 'participants');
    p.set('q', query);
    p.set('inAbout', String(inAbout));
    p.append('countries[]', '1');
    ['categories','languages','channelType','participantsCountTo',
     'avgReachFrom','avgReachTo','avgReach24From','avgReach24To','ciFrom','ciTo'].forEach(k => p.set(k,''));
    p.set('participantsCountFrom', String(minSubs));
    p.set('age', '0-120'); p.set('err', '0-100'); p.set('er','0');
    p.set('male','0'); p.set('female','0');
    p.set('isVerified','0'); p.set('isRknVerified','0'); p.set('isStoriesAvailable','0');
    ['noRedLabel','noScam','noDead'].forEach(k => { p.append(k,'0'); p.append(k,'1'); });
    p.set('page', String(page));
    p.set('offset', String(offset));

    const r = await fetch(endpoint, {
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

# TGStat лінкує канали як /channel/@handle, чати як /chat/@handle
LINK_RE = re.compile(r"/(channel|chat)/@(\w+)")


def parse_html(html):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for item in soup.select('div.col-12, div[class*="peer"], div[class*="channel"]'):
        link = item.select_one('a[href*="/channel/@"], a[href*="/chat/@"]')
        if not link:
            continue
        m = LINK_RE.search(link["href"])
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        name_el = item.select_one('[class*="name"], [class*="title"], b')
        subs_el = item.select_one('[class*="participants"], [class*="members"], [class*="counter"]')
        out.append({
            "handle": m.group(2),
            "tgstat_kind": m.group(1),
            "name": name_el.get_text(strip=True) if name_el else "",
            "subs": subs_el.get_text(strip=True) if subs_el else "",
        })
    return out


async def scrape(browser_page, endpoint, query, min_subs, in_about):
    """-> (список знахідок, None) або ([], 'причина зупинки')."""
    found, seen = [], set()
    page_num, offset = 0, 0
    while page_num < MAX_PAGES:
        data = await browser_page.evaluate(FETCH_JS, {
            "endpoint": endpoint, "query": query, "minSubs": min_subs,
            "inAbout": in_about, "page": page_num, "offset": offset,
        })
        if "error" in data:
            return found, f"{data['error']}"
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


def load_points():
    pts = json.loads(POINTS_FILE.read_text(encoding="utf-8"))["points"]
    return [p for p in pts if not ONLY or p["point"] in ONLY]


async def main():
    points = load_points()
    tasks = [(p, q, sec, url) for p in points for q in p["queries"] for sec, url in SECTIONS]
    done = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else {}
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    print(f"сітка: {POINTS_FILE}\nвихід: {OUT_FILE}", flush=True)
    print(f"точок: {len(points)}, запитів до TGStat: {len(tasks)}, "
          f"вже зроблено: {len(done)}", flush=True)

    chats_supported = True

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://tgstat.ru/channels/search", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        print(f">>> Залогінься на tgstat.ru, потім: touch {GO_FLAG}", flush=True)
        while not GO_FLAG.exists():
            await asyncio.sleep(1)
        GO_FLAG.unlink()
        print(f"старт (профіль: {PROFILE_DIR})\n", flush=True)

        chat_fails = 0
        for i, (p, query, section, url) in enumerate(tasks, 1):
            key = f"{p['point']}|{query}|{section}"
            if key in done:
                continue
            if section == "chat" and not chats_supported:
                continue

            items, stop = await scrape(page, url, query, p["min_subs"], p["in_about"])
            # Розділ чатів вимикаємо за ФАКТОМ повторних збоїв, а не за кодом:
            # /chats/search віддає 500 (наш набір полів йому не підходить), і бити
            # в нього ще 60 разів — зайвий ризик для сесії.
            if section == "chat" and not items and stop:
                chat_fails += 1
                if chat_fails >= CHAT_FAIL_LIMIT:
                    chats_supported = False
                    print(f"  ⚠ /chats/search не відповідає ({stop}) — вимикаю розділ "
                          f"чатів. Канали збираються далі; чати доберемо "
                          f"linked-групами і TeleZip", flush=True)
                continue
            if section == "chat":
                chat_fails = 0

            # Порожній результат ПЛЮС причина зупинки = запит не відпрацював
            # (злетіла сесія, 403, не-JSON). Такий у кеш не пишемо — інакше
            # наступний прогін вважатиме точку зробленою і мовчки її пропустить.
            failed = stop is not None and not items
            if not failed:
                done[key] = {
                    "point": p["point"], "region_id": p["region_id"], "region": p["region"],
                    "query": query, "section": section, "min_subs": p["min_subs"],
                    "ambiguous": p["ambiguous"], "stop": stop, "items": items,
                }
                OUT_FILE.write_text(json.dumps(done, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
            mark = ("ЗБІЙ: " + str(stop)) if failed else (
                f"{len(items):>4}" + (f"  ({stop})" if stop else ""))
            print(f"  [{i}/{len(tasks)}] {p['point'][:18]:<19}{section:<8}"
                  f"«{query[:28]}»{'':<3}{mark}", flush=True)
            await asyncio.sleep(2)

        uniq = {c["handle"] for v in done.values() for c in v["items"]}
        print(f"\n✓ готово: {len(done)} запитів, {len(uniq)} унікальних хендлів "
              f"-> {OUT_FILE}", flush=True)
        await ctx.close()


asyncio.run(main())
