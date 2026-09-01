"""Крок 1б-фаза-А: метадані з прев'ю t.me для всіх хендлів зі збору TGStat.

Навіщо окремо від border_02_enrich.py: збагачення через СТОРІНКИ TGStat вимагає
браузера з живою сесією і коштує ~2.5 с на хендл — на 2800 хендлів це ~2 години.
А прев'ю `t.me/<handle>` — звичайний HTTP-запит без авторизації, дає назву, опис і
кількість підписників, тобто рівно те, що потрібно LLM-профілюванню (крок 3).

Тому порядок такий: спершу дешева фаза (ця, ~5 хв на 2800 хендлів у 6 потоків) →
профілювання ріже ~половину → і лише вцілілих женемо через дорогу фазу TGStat
(охоплення, ERR%, категорія), якщо вона взагалі знадобиться.

Побічно фаза відсіює мертве: якщо t.me не віддає og:title, канал приватний,
видалений або хендл не існує.

Ідемпотентний: уже отримані хендли пропускаються.

Запуск:  python3 -u border_02a_tme.py
Env: TGSTAT_IN, TME_OUT, TME_WORKERS (дефолт 6), TME_LIMIT.
"""
import html as htmllib
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
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


IN_FILE = _path("TGSTAT_IN", ("backend", "_dir", "border_tgstat_raw.json"),
                "border_tgstat_raw.json", must_exist=True)
OUT_FILE = _path("TME_OUT", ("backend", "_dir", "border_tme.json"), "border_tme.json")
WORKERS = int(os.environ.get("TME_WORKERS", 6))
LIMIT = int(os.environ.get("TME_LIMIT", 0))

# Accept-Language ОБОВ'ЯЗКОВИЙ: без нього t.me віддає обрізану сторінку без
# og-метаданих, і живий канал не відрізниш від видаленого (ловили на 1798 хендлах —
# 55% улову хибно позначились як мертві).
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

RE_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')
RE_DESC = re.compile(r'<meta property="og:description" content="([^"]*)"')
RE_EXTRA = re.compile(r'<div class="tgme_page_extra">([^<]+)</div>')

_lock = threading.Lock()


def parse_subs(raw):
    """'11 287 subscribers' / '4 512 members, 87 online' -> 11287 / 4512."""
    m = re.search(r"[\d\s ]+", raw or "")
    digits = re.sub(r"[^\d]", "", m.group(0)) if m else ""
    return int(digits) if digits else None


def fetch(handle, session, tries=3):
    for attempt in range(tries):
        try:
            r = session.get(f"https://t.me/{handle}", headers=UA, timeout=10)
            if r.status_code != 200:
                return {"ok": False, "error": f"http {r.status_code}"}
            t = r.text
            title = htmllib.unescape((RE_TITLE.search(t) or [None, ""])[1])
            desc_m = RE_DESC.search(t)
            desc = re.sub(r"\s+", " ", htmllib.unescape(desc_m.group(1))).strip() if desc_m else ""
            extra = (RE_EXTRA.search(t) or [None, ""])[1].strip()
            # t.me для неіснуючого/приватного віддає болванку "Telegram: Contact @x"
            if not title or title.startswith("Telegram: Contact"):
                # Може бути і справді видалений, і обрізана відповідь — даємо
                # ще одну спробу, перш ніж списувати хендл.
                # t.me тротлить при кількох потоках і віддає ту саму обрізану
                # сторінку, що й для видаленого каналу. Відрізнити не можна, тому
                # відступаємо все довше — 2 с, 5 с — перш ніж списувати хендл.
                if attempt + 1 < tries:
                    time.sleep(2.0 + 3.0 * attempt)
                    continue
                return {"ok": False, "error": "немає прев'ю (приватний/видалений)"}
            return {
                "ok": True, "title": title, "description": desc,
                "subscribers": parse_subs(extra), "extra": extra,
                # у прев'ю групи пишуть "members", у каналу — "subscribers"
                "looks_like": "chat" if "member" in extra.lower() else "channel",
            }
        except requests.RequestException as e:
            if attempt + 1 == tries:
                return {"ok": False, "error": f"{type(e).__name__}"}
            time.sleep(1.5)
    return {"ok": False, "error": "unknown"}


def queue():
    raw = json.loads(IN_FILE.read_text(encoding="utf-8"))
    seen, out = set(), []
    for v in raw.values():
        for c in v["items"]:
            h = c["handle"]
            if h.lower() in seen:
                continue
            seen.add(h.lower())
            out.append({"handle": h, "point": v["point"], "region": v["region"],
                        "region_id": v["region_id"], "query": v["query"],
                        "ambiguous": v["ambiguous"],
                        "tgstat_kind": c.get("tgstat_kind", "channel")})
    return out


def main():
    items = queue()
    done = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else {}
    todo = [c for c in items if c["handle"] not in done]
    if LIMIT:
        todo = todo[:LIMIT]
    print(f"хендлів: {len(items)}, до опитування: {len(todo)}, потоків: {WORKERS}",
          flush=True)
    if not todo:
        return

    session = requests.Session()
    counter = {"n": 0}

    def work(c):
        time.sleep(random.uniform(0, 0.4))          # рознести старти потоків
        res = fetch(c["handle"], session)
        with _lock:
            done[c["handle"]] = {**c, **res}
            counter["n"] += 1
            n = counter["n"]
            if n % 100 == 0 or n == len(todo):
                OUT_FILE.write_text(json.dumps(done, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
                alive = sum(1 for v in done.values() if v.get("ok"))
                print(f"  {n}/{len(todo)}  живих: {alive}", flush=True)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(work, todo))

    OUT_FILE.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
    alive = [v for v in done.values() if v.get("ok")]
    chats = sum(1 for v in alive if v["looks_like"] == "chat")
    print(f"\n✓ {len(done)} хендлів за {time.time()-t0:.0f} с -> {OUT_FILE}\n"
          f"  живих: {len(alive)} (з них схожих на групи: {chats}), "
          f"мертвих/приватних: {len(done)-len(alive)}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
