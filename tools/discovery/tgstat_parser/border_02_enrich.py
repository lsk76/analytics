"""Крок 1б: збагачення хендлів із border_01_collect.py -> CSV (без Google Sheets).

Чому не Sheets, як у 02_enrich_and_upload.py: менше рухомих частин (ключ сервісного
акаунта лежить у ~/Downloads, таблиця спільна з іншою задачею), а далі CSV усе одно
йде в `Channel` своїм імпортером — з `region_subject` і `settlement`, чого
`import_gsheet_channels` не вміє (він пише лише текстовий `inferred_region`).

Дані на кожен хендл: сторінка TGStat (охоплення, ERR%, постів/тиждень, категорія,
мова, опис) + прев'ю t.me (назва, опис, підписники — безкоштовно й без сесії).

Ідемпотентний: уже збагачені хендли пропускаються (кеш поруч із CSV).

Запуск:
    python3 -u border_02_enrich.py > enrich.log 2>&1 &
    touch ~/.tgstat_go.flag        # якщо потрібен логін

Env: TGSTAT_PROFILE, TGSTAT_GO_FLAG, TGSTAT_IN, TGSTAT_CSV, TGSTAT_LIMIT.
"""
import asyncio
import csv
import html as htmllib
import json
import os
import re
from pathlib import Path

import requests
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

PROFILE_DIR = Path(os.environ.get("TGSTAT_PROFILE", Path.home() / ".tgstat_profile"))
GO_FLAG = Path(os.environ.get("TGSTAT_GO_FLAG", Path.home() / ".tgstat_go.flag"))
IN_FILE = _path("TGSTAT_IN", ("backend", "_dir", "border_tgstat_raw.json"),
                "border_tgstat_raw.json", must_exist=True)
CSV_FILE = _path("TGSTAT_CSV", ("backend", "_dir", "border_tgstat_channels.csv"),
                 "border_tgstat_channels.csv")
CACHE_FILE = CSV_FILE.with_suffix(".cache.json")
LIMIT = int(os.environ.get("TGSTAT_LIMIT", 0))

HEADERS = [
    "Точка", "Регіон", "Назва", "Платформа", "Посилання", "TGStat", "Тип",
    "Підписники", "Серед. охоплення", "ERR%", "Постів/тижд.", "Категорія",
    "Мова", "Опис (Telegram)", "Опис (TGStat)", "Хендл", "Запит", "Неоднозначна",
]


def parse_subs(raw):
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else ""


def tg_info(handle):
    """Прев'ю t.me: назва, опис, підписники. Безкоштовно, без сесії TGStat."""
    try:
        r = requests.get(f"https://t.me/{handle}",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        t = r.text
        title_m = re.search(r'<meta property="og:title" content="([^"]+)"', t)
        desc_m = re.search(r'<meta property="og:description" content="([^"]+)"', t)
        subs_m = re.search(r'<div class="tgme_page_extra">([^<]+)</div>', t)
        title = htmllib.unescape(title_m.group(1)) if title_m else ""
        desc = re.sub(r"\s+", " ", htmllib.unescape(desc_m.group(1))).strip() if desc_m else ""
        subs = subs_m.group(1).strip() if subs_m else ""
        if "Telegram: Contact" in title:
            title = ""
        return title, desc, subs
    except Exception:
        return "", "", ""


def parse_tgstat_page(html):
    """Ті самі селектори, що в 02_enrich_and_upload.py — вони перевірені на TGStat."""
    soup = BeautifulSoup(html, "html.parser")
    data = {}

    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        data["name"] = htmllib.unescape(og["content"])
    else:
        title_tag = soup.find("title")
        raw_title = title_tag.get_text(strip=True) if title_tag else ""
        data["name"] = raw_title.split(" - TGStat")[0].split(" — TGStat")[0].strip()

    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        data["description"] = re.sub(r"\s+", " ", htmllib.unescape(og_desc["content"])).strip()
    else:
        desc_el = soup.select_one('.channel-description, [class*="description"], .about')
        data["description"] = desc_el.get_text(strip=True) if desc_el else ""

    counters = {}
    for block in soup.select('[class*="count"], [class*="stat"], [class*="counter"], .card-body'):
        label_el = block.select_one('[class*="label"], [class*="title"], small, span')
        value_el = block.select_one('[class*="value"], [class*="number"], b, strong')
        if label_el and value_el:
            counters[label_el.get_text(strip=True).lower()] = value_el.get_text(strip=True)

    def pick(keys):
        for want in keys:
            for k, v in counters.items():
                if want in k:
                    return v
        return ""

    data["subscribers"] = pick(["subscribers", "подписчик", "участник", "members"])
    data["avg_reach"] = pick(["reach", "охват", "coverage"])
    data["err"] = pick(["err", "вовлечен", "engagement"])
    data["posts_per_week"] = pick(["публикац", "пост", "post"])

    for tag in soup.select('.badge, [class*="category"], [class*="tag"], [class*="lang"]'):
        txt = tag.get_text(strip=True)
        if txt and len(txt) < 40:
            if not data.get("category"):
                data["category"] = txt
            elif not data.get("language"):
                data["language"] = txt
    return data


def queue():
    """Унікальні хендли зі збору: перша точка, що їх знайшла, лишається власником."""
    raw = json.loads(IN_FILE.read_text(encoding="utf-8"))
    seen, out = set(), []
    for v in raw.values():
        for c in v["items"]:
            h = c["handle"]
            if h in seen:
                continue
            seen.add(h)
            out.append({
                "handle": h, "point": v["point"], "region": v["region"],
                "query": v["query"], "ambiguous": v["ambiguous"],
                "kind": c.get("tgstat_kind", "channel"),
                "name_hint": c.get("name", ""),
            })
    return out


async def main():
    items = queue()
    cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    todo = [c for c in items if c["handle"] not in cache]
    if LIMIT:
        todo = todo[:LIMIT]
    print(f"хендлів усього: {len(items)}, до збагачення: {len(todo)}", flush=True)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://tgstat.ru", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        if GO_FLAG.exists():
            GO_FLAG.unlink()

        errors = 0
        for i, ch in enumerate(todo, 1):
            handle, kind = ch["handle"], ch["kind"]
            tgstat_url = f"https://tgstat.ru/{kind}/@{handle}"
            tg_name, tg_desc, tg_subs = tg_info(handle)

            tgs = {}
            try:
                await page.goto(tgstat_url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1000)
                tgs = parse_tgstat_page(await page.content())
            except Exception:
                errors += 1

            tgs_name = tgs.get("name", "")
            cache[handle] = {
                "Точка": ch["point"], "Регіон": ch["region"],
                "Назва": tg_name or (tgs_name if len(tgs_name) > 2 else "") or handle,
                "Платформа": "TG",
                "Посилання": f"https://t.me/{handle}",
                "TGStat": tgstat_url,
                "Тип": "чат" if kind == "chat" else "канал",
                "Підписники": parse_subs(tgs.get("subscribers") or tg_subs),
                "Серед. охоплення": tgs.get("avg_reach", ""),
                "ERR%": tgs.get("err", ""),
                "Постів/тижд.": tgs.get("posts_per_week", ""),
                "Категорія": tgs.get("category", ""),
                "Мова": tgs.get("language", ""),
                "Опис (Telegram)": tg_desc,
                "Опис (TGStat)": tgs.get("description", ""),
                "Хендл": handle, "Запит": ch["query"],
                "Неоднозначна": "так" if ch["ambiguous"] else "",
            }
            if i % 10 == 0 or i == len(todo):
                CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
                print(f"  [{i}/{len(todo)}] {handle[:28]:<30}"
                      f"{cache[handle]['Підписники']}", flush=True)
            await asyncio.sleep(1.2)

        await ctx.close()

    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    with CSV_FILE.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writeheader()
        for h in cache.values():
            w.writerow(h)
    print(f"\n✓ {len(cache)} рядків -> {CSV_FILE} (помилок сторінки: {errors})", flush=True)


asyncio.run(main())
