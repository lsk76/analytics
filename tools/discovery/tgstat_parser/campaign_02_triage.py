"""Крок 2 задачі «виборча кампанія 2026»: тріаж зібраних хендлів.

Прогін 2 дає ~2 тис. хендлів на односкладових запитах («Тыва», «Казань»), і це
переважно побутові регіональні канали. TGStat-парсер назви не віддає (порожні
`name`/`subs` у CSV), тому назву й опис беремо з прев'ю t.me, а далі фільтруємо
за ключовими словами виборчої тематики.

Вихід — `campaign_shortlist.csv`: кандидати на додавання, відсортовані за
регіоном і кількістю влучань. Рішення про додавання лишається за людиною.

Запуск:
    python3 -u campaign_02_triage.py > campaign_triage.log 2>&1 &

Env: TGSTAT_CSV (вхід), TGSTAT_SHORTLIST (вихід), TGSTAT_SKIP (кома-перелік
     хендлів, які вже в базі), TGSTAT_WORKERS (дефолт 8).
"""
import csv
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LOCAL = Path(__file__).resolve().parent
IN_CSV = Path(os.environ.get("TGSTAT_CSV", LOCAL / "campaign_tgstat_channels.csv"))
OUT_CSV = Path(os.environ.get("TGSTAT_SHORTLIST", LOCAL / "campaign_shortlist.csv"))
CACHE = Path(os.environ.get("TGSTAT_TME_CACHE", LOCAL / "campaign_tme_cache.csv"))
SKIP = {h.strip().lower() for h in os.environ.get("TGSTAT_SKIP", "").split(",") if h.strip()}
WORKERS = int(os.environ.get("TGSTAT_WORKERS", 8))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")

# Влучання в назві/описі -> кандидат. Вага: профільні слова важать більше.
KEYWORDS = {
    3: ["выбор", "избирком", "избирательн", "избират", "кандидат", "бюллетен",
        "наблюдател", "цик ", "уик ", "тик "],
    2: ["депутат", "хурал", "курултай", "госсовет", "ил тумэн", "парламент",
        "кпрф", "лдпр", "справедлив", "новые люди", "яблоко", "партия",
        "штаб", "политик", "политич"],
    1: ["власт", "чиновник", "мэр", "глава республики", "губернатор",
        "коррупц", "суд", "прокуратур", "новости"],
}
# Явне сміття: товари, послуги, розваги — навіть якщо влучили в «новости»
STOP = ["ткани", "fashion", "маникюр", "доставка", "пицц", "суши", "аренда",
        "недвижим", "ремонт", "кредит", "займ", "работа ваканс", "знакомств",
        "гороскоп", "погода", "барахолк", "объявлени", "скидк", "промокод"]


def fetch_meta(handle):
    """(title, subs, desc) з прев'ю t.me; порожні поля, якщо канал недоступний."""
    try:
        req = urllib.request.Request("https://t.me/" + handle, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    except Exception as e:
        return "", "", "ПОМИЛКА: %s" % str(e)[:60]
    def grab(pat):
        m = re.search(pat, html)
        return (m.group(1) if m else "").replace("&#33;", "!").replace("&#039;", "'")
    return (grab(r'<meta property="og:title" content="([^"]*)"'),
            grab(r'<div class="tgme_page_extra">([^<]*)</div>').strip(),
            grab(r'<meta property="og:description" content="([^"]*)"')[:200].replace("\n", " "))


def subs_num(raw):
    m = re.search(r"([\d\s ]+)", raw or "")
    return int(re.sub(r"\D", "", m.group(1))) if m and re.sub(r"\D", "", m.group(1)) else 0


def score(title, desc):
    blob = ("%s %s" % (title, desc)).lower()
    if any(s in blob for s in STOP):
        return 0, []
    total, hits = 0, []
    for weight, words in KEYWORDS.items():
        for w in words:
            if w in blob:
                total += weight
                hits.append(w.strip())
    return total, hits


def load_cache():
    if not CACHE.exists():
        return {}
    with CACHE.open(encoding="utf-8") as f:
        return {r["handle"]: r for r in csv.DictReader(f)}


def save_cache(meta):
    with CACHE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["handle", "title", "subs", "desc"])
        w.writeheader()
        for h, m in sorted(meta.items()):
            w.writerow({"handle": h, "title": m["title"], "subs": m["subs"], "desc": m["desc"]})


def main():
    rows = list(csv.DictReader(IN_CSV.open(encoding="utf-8")))
    todo = [r for r in rows if r["handle"].lower() not in SKIP]
    print("хендлів у CSV: %d, після відсіву наявних у базі: %d" % (len(rows), len(todo)), flush=True)

    meta = load_cache()
    need = [r["handle"] for r in todo if r["handle"] not in meta]
    print("треба забрати з t.me: %d (у кеші вже %d)" % (len(need), len(meta)), flush=True)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, (h, res) in enumerate(zip(need, pool.map(fetch_meta, need)), 1):
            meta[h] = {"title": res[0], "subs": res[1], "desc": res[2]}
            if i % 100 == 0:
                print("  %d/%d" % (i, len(need)), flush=True)
                save_cache(meta)
    save_cache(meta)

    out = []
    for r in todo:
        m = meta.get(r["handle"], {"title": "", "subs": "", "desc": ""})
        s, hits = score(m["title"], m["desc"])
        if s < 3:                     # лишаємо лише тих, хто влучив у профільне слово
            continue
        out.append({
            "score": s, "handle": r["handle"], "title": m["title"],
            "subs": subs_num(m["subs"]), "regions": r["regions"],
            "hits": " ".join(sorted(set(hits))), "desc": m["desc"][:120],
            "url": "https://t.me/%s" % r["handle"],
        })
    out.sort(key=lambda x: (x["regions"], -x["score"], -x["subs"]))

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["score", "handle", "title", "subs",
                                          "regions", "hits", "desc", "url"])
        w.writeheader()
        w.writerows(out)
    print("\n✓ шорт-лист: %d кандидатів -> %s" % (len(out), OUT_CSV), flush=True)


if __name__ == "__main__":
    main()
