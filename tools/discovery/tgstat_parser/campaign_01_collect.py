"""Крок 1 задачі «виборча кампанія 2026»: збір хендлів із TGStat по 5 республіках.

Відмінності від 01_collect_handles.py (той лишається як є — загальна регіональна
підбірка) і від border_01_collect.py (той по населених пунктах):
  * сітка — 5 цільових республік × виборчі запити (виборчком, парламент,
    «вибори», партії, політика столиці), а не одна назва регіону;
  * поріг підписників НИЗЬКИЙ (300): канали спостерігачів, штабів і місцевих
    депутатів рідко бувають великими, дефолтні 800 їх зрізають;
  * `inAbout=1` — слово «выборы» частіше в описі каналу, ніж у заголовку;
  * шукаються і канали, і чати (`/chats/search`), як у border-версії;
  * ідемпотентний: уже зроблені (регіон, запит, розділ) пропускаються, прогін
    можна зупиняти й продовжувати;
  * вихід — JSON (сирий) + CSV зі зведеною таблицею унікальних хендлів.

Сесія TGStat прив'язана до IP і TLS-fingerprint браузера, тому запити йдуть із
самої сторінки (fetch + CSRF-токен), а логін робиться руками один раз.

Запуск:
    python3 -u campaign_01_collect.py > campaign_collect.log 2>&1 &
    # відкриється Chrome — просто залогінитись на tgstat.ru; скрипт САМ
    # перевіряє авторизацію раз на 30 с і стартує. `touch ~/.tgstat_go.flag` —
    # лише ручний обхід, якщо автодетект схибить.

Env: TGSTAT_PROFILE (дефолт ~/.tgstat_profile), TGSTAT_GO_FLAG,
     TGSTAT_OUT (JSON), TGSTAT_CSV, TGSTAT_REGIONS (фільтр: кома-перелік),
     TGSTAT_MIN_SUBS (дефолт 300), TGSTAT_MAX_PAGES.
"""
import asyncio
import csv
import json
import os
import re
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve()
LOCAL = HERE.parent

PROFILE_DIR = Path(os.environ.get("TGSTAT_PROFILE", Path.home() / ".tgstat_profile"))
GO_FLAG = Path(os.environ.get("TGSTAT_GO_FLAG", Path.home() / ".tgstat_go.flag"))
OUT_FILE = Path(os.environ.get("TGSTAT_OUT", LOCAL / "campaign_tgstat_raw.json"))
CSV_FILE = Path(os.environ.get("TGSTAT_CSV", LOCAL / "campaign_tgstat_channels.csv"))
MIN_SUBS = int(os.environ.get("TGSTAT_MIN_SUBS", 300))
MAX_PAGES = int(os.environ.get("TGSTAT_MAX_PAGES", 40))
ONLY = [r.strip() for r in os.environ.get("TGSTAT_REGIONS", "").split(",") if r.strip()]

# Регіон -> запити. Назва регіону = канонічна Region.name у БД (для імпорту).
# Запит — рядок (тоді поріг = MIN_SUBS) або пара (запит, свій_поріг).
#
# Прогін 1 (багатослівні: «Бурятия выборы», «избирком Якутия») дав мало: TGStat
# матчить кілька слів строго, і половина запитів повернула нуль. Прогін 2 —
# ОДНОСЛІВНІ токени + вищий поріг для великих міст (інакше «Казань» тягне
# тисячу побутових каналів) і НИЗЬКИЙ для виборчкомів (вони дрібні).
REGIONS = [
    {
        "region": "Бурятія",
        "queries": ["Бурятия выборы", "избирком Бурятия", "Народный Хурал",
                    "Улан-Удэ политика", "Бурятия депутат", "Бурятия КПРФ",
                    # прогін 2
                    "Бурятия", ("Улан-Удэ", 800), "Хурал"],
    },
    {
        "region": "Саха (Якутія)",
        "queries": ["Якутия выборы", "избирком Якутия", "Ил Тумэн",
                    "Якутск политика", "Якутия депутат", "Якутия КПРФ",
                    # прогін 2
                    "Якутия", "Саха", ("Якутск", 800)],
    },
    {
        "region": "Тива",
        "queries": ["Тыва выборы", "избирком Тыва", "Верховный Хурал Тыва",
                    "Кызыл политика", "Тыва депутат",
                    # прогін 2 — найслабша республіка, пороги мінімальні
                    ("Тыва", 150), ("Тува", 150), ("Кызыл", 150)],
    },
    {
        "region": "Татарстан",
        "queries": ["Татарстан выборы", "избирком Татарстан", "Госсовет Татарстана",
                    "Казань политика", "Татарстан депутат", "Татарстан КПРФ",
                    # прогін 2
                    ("Татарстан", 800), ("Казань", 2000), ("Челны", 800), "Госсовет"],
    },
    {
        "region": "Башкортостан",
        "queries": ["Башкортостан выборы", "избирком Башкирия", "Курултай Башкортостан",
                    "Уфа политика", "Башкирия депутат", "Башкирия КПРФ",
                    # прогін 2
                    ("Башкортостан", 800), ("Башкирия", 800), ("Уфа", 2000), "Курултай"],
    },
    {
        # Псевдорегіон: виборчкоми шукаються по всій РФ одним запитом, бо
        # «избирком <республіка>» строгим матчем дає нуль. Розкладаємо за
        # регіонами руками під час тріажу — у назві каналу республіка є.
        "region": "виборчкоми РФ",
        "queries": [("Избирательная комиссия", 150), ("Избирком", 150),
                    ("Выборы 2026", 150)],
    },
]

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


def write_csv(done):
    """Зведена таблиця унікальних хендлів: регіон + за якими запитами знайдено."""
    rows = {}
    for v in done.values():
        for c in v["items"]:
            r = rows.setdefault(c["handle"], {
                "handle": c["handle"], "name": c["name"], "subs": c["subs"],
                "kind": c["tgstat_kind"], "regions": set(), "queries": set(),
            })
            r["regions"].add(v["region"])
            r["queries"].add(v["query"])
            if not r["name"]:
                r["name"] = c["name"]
    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CSV_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["handle", "name", "subs", "kind", "regions", "queries", "url"])
        for r in sorted(rows.values(), key=lambda x: (sorted(x["regions"])[0], x["handle"])):
            w.writerow([r["handle"], r["name"], r["subs"], r["kind"],
                        " | ".join(sorted(r["regions"])),
                        " | ".join(sorted(r["queries"])),
                        f"https://t.me/{r['handle']}"])
    return len(rows)


def norm_queries(region):
    """Запит може бути рядком або парою (запит, свій_поріг) -> (запит, поріг)."""
    for q in region["queries"]:
        yield (q, MIN_SUBS) if isinstance(q, str) else (q[0], q[1])


async def main():
    regions = [r for r in REGIONS if not ONLY or r["region"] in ONLY]
    tasks = [(r, q, ms, sec, url) for r in regions for q, ms in norm_queries(r)
             for sec, url in SECTIONS]
    done = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else {}
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    print(f"вихід: {OUT_FILE}\n       {CSV_FILE}", flush=True)
    print(f"регіонів: {len(regions)}, запитів до TGStat: {len(tasks)}, "
          f"вже зроблено: {len(done)}, поріг підписників: {MIN_SUBS}", flush=True)

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

        # Старт БЕЗ ручного прапорця: раз на 30 с пробуємо той самий пошуковий
        # запит, що й у робочому циклі. status=='ok' -> сесія жива, працюємо.
        # Прапорець лишається як ручний обхід, якщо автодетект схибить.
        print(">>> Чекаю на авторизацію в Chrome (перевірка раз на 30 с). "
              f"Ручний обхід: touch {GO_FLAG}", flush=True)
        attempt = 0
        while True:
            if GO_FLAG.exists():
                GO_FLAG.unlink()
                print("  прапорець — стартую примусово", flush=True)
                break
            try:
                probe = await page.evaluate(FETCH_JS, {
                    "endpoint": "/channels/search", "query": "Бурятия",
                    "minSubs": 800, "inAbout": 0, "page": 0, "offset": 0,
                })
            except Exception as e:                       # сторінку перезавантажують
                probe = {"error": str(e)[:60]}
            if probe.get("status") == "ok":
                print("  авторизація ок", flush=True)
                break
            attempt += 1
            print(f"  [{attempt}] ще не авторизовано ({probe.get('error') or 'no-status'})"
                  f" — чекаю 30 с", flush=True)
            await asyncio.sleep(30)
        print(f"старт (профіль: {PROFILE_DIR})\n", flush=True)

        chat_fails = 0
        for i, (r, query, min_subs, section, url) in enumerate(tasks, 1):
            key = f"{r['region']}|{query}|{section}"
            if key in done:
                continue
            if section == "chat" and not chats_supported:
                continue

            items, stop = await scrape(page, url, query, min_subs, 1)
            # Розділ чатів вимикаємо за ФАКТОМ повторних збоїв, а не за кодом:
            # /chats/search у border-прогоні віддавав 500 на цей набір полів.
            if section == "chat" and not items and stop:
                chat_fails += 1
                if chat_fails >= CHAT_FAIL_LIMIT:
                    chats_supported = False
                    print(f"  ⚠ /chats/search не відповідає ({stop}) — вимикаю розділ "
                          f"чатів, канали збираються далі", flush=True)
                continue
            if section == "chat":
                chat_fails = 0

            # Порожньо ПЛЮС причина зупинки = запит не відпрацював (злетіла сесія,
            # 403, не-JSON). У кеш не пишемо, інакше наступний прогін мовчки
            # вважатиме запит зробленим.
            failed = stop is not None and not items
            if not failed:
                done[key] = {"region": r["region"], "query": query, "section": section,
                             "min_subs": min_subs, "stop": stop, "items": items}
                OUT_FILE.write_text(json.dumps(done, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
            mark = ("ЗБІЙ: " + str(stop)) if failed else (
                f"{len(items):>4}" + (f"  ({stop})" if stop else ""))
            print(f"  [{i}/{len(tasks)}] {r['region'][:14]:<15}{section:<8}"
                  f"«{query[:28]}»{'':<3}{mark}", flush=True)
            await asyncio.sleep(2)

        n_uniq = write_csv(done)
        print(f"\n✓ готово: {len(done)} запитів, {n_uniq} унікальних хендлів"
              f"\n  сирий JSON: {OUT_FILE}\n  таблиця:    {CSV_FILE}", flush=True)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
