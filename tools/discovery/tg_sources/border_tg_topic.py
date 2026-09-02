"""Пошук через TELEGRAM: у яких чатах реально говорять про перетин кордону.

Чому не TeleZip: він має внутрішній ліміт 2 хв на запит і ріже широкі запити, а
головне — не гарантує, що взагалі індексує ці 400+ дрібних груп. Telethon читає
першоджерело і дає точну відповідь по кожному чату.

Механіка: серверний пошук Telegram (`get_messages(search=...)`) по кожному чату
окремим словом, вікно 7 діб. Це дешевше за вичитування всієї стрічки: у жвавому
чаті за тиждень тисячі повідомлень, а пошук віддає лише влучення.

Беремо лише групи від MIN_MEMBERS учасників — у дрібніших статистика все одно
не показова.

Кожен чат читається ТИМ акаунтом, що його резолвив (резолв кешується в сесії).
Env: MIN_MEMBERS (100), DAYS (7), TG_CONCURRENCY (8), TG_PAUSE (1.2), TG_LIMIT.
Вихід: _dir/border_tg_topic.json
"""
import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import InputPeerChannel

from accounts.models import TelegramAccount

RAW = Path(os.environ.get("TG_RAW", "_dir/border_resolve_raw.json"))
DST = Path(os.environ.get("TG_DST", "_dir/border_tg_topic.json"))

MIN_MEMBERS = int(os.environ.get("MIN_MEMBERS", 100))
DAYS = int(os.environ.get("DAYS", 7))
CONCURRENCY = int(os.environ.get("TG_CONCURRENCY", 8))
PAUSE = float(os.environ.get("TG_PAUSE", 1.2))
LIMIT = int(os.environ.get("TG_LIMIT", 0))
PER_TERM = 100          # стеля влучень на слово (жвавий чат не вичитуємо цілком)

# Слова підібрані так, щоб кожне окремо було високосигнальним: Telegram не вміє
# OR у пошуку, тож кожне слово — окремий запит, і сміттєві сюди брати не можна.
TERMS = ["граница", "погранпереход", "таможня", "погранконтроль", "кпп",
         "выпустили", "невыездной"]

# Назва «свого» переходу додається до слів лише для чатів відповідного регіону —
# інакше це 16 зайвих запитів на кожен чат.
CROSSING_BY_REGION = {
    74: "Казанское", 58: "Исилькуль", 57: "Карасук", 23: "Кулунда",
    4: "Кяхта", 24: "Забайкальск", 29: "Полтавка", 2: "Ташанта", 18: "Хандагайты",
}

since = datetime.now(timezone.utc) - timedelta(days=DAYS)


def chats():
    raw = json.loads(RAW.read_text())
    out = []
    for r in raw.values():
        if not r.get("ok"):
            continue
        le = r.get("linked_entity")
        ent = le or r.get("entity") or {}
        full = ((r.get("linked_full") if le else r.get("full")) or {}).get("full_chat") or {}
        if not (le or ent.get("megagroup") or ent.get("gigagroup")):
            continue
        members = full.get("participants_count") or 0
        if members < MIN_MEMBERS:
            continue
        out.append({
            "tg_id": ent.get("id"), "username": ent.get("username") or "",
            # без юзернейма Telethon приймає голий id за PeerUser і падає;
            # access_hash із дампа робить із нього коректний InputPeerChannel
            "access_hash": ent.get("access_hash"),
            "title": ent.get("title") or "", "members": members,
            "point": r.get("point") or "", "region": r.get("region") or "",
            "region_id": r.get("region_id"), "account_id": r.get("account_id"),
            "parent": (r.get("entity") or {}).get("username") if le else "",
        })
    out.sort(key=lambda c: -c["members"])
    return out


all_chats = chats()
if LIMIT:
    all_chats = all_chats[:LIMIT]
done = json.loads(DST.read_text()) if DST.exists() else {}
todo = [c for c in all_chats if str(c["tg_id"]) not in done]

accounts = {a.id: a for a in TelegramAccount.objects.filter(
    is_authenticated=True, is_active=True).exclude(session_string="").select_related("proxy")}
by_acc = {}
for c in todo:
    aid = c["account_id"] if c["account_id"] in accounts else None
    if aid is None:                       # резолвив акаунт, якого вже нема
        aid = sorted(accounts)[c["tg_id"] % len(accounts)]
    by_acc.setdefault(aid, []).append(c)

print(f"чатів від {MIN_MEMBERS} учасників: {len(all_chats)}, до перевірки {len(todo)}; "
      f"вікно {DAYS} діб; акаунтів {len(accounts)}; слів {len(TERMS)}+1", flush=True)

_lock = asyncio.Lock()
stat = {"ok": 0, "err": 0, "hits": 0}


async def scan_chat(client, c):
    terms = list(TERMS)
    cr = CROSSING_BY_REGION.get(c["region_id"])
    if cr:
        terms.append(cr)
    if c["username"]:
        entity = c["username"]
    elif c.get("access_hash") is not None:
        entity = InputPeerChannel(int(c["tg_id"]), int(c["access_hash"]))
    else:
        return {"error": "немає ні юзернейма, ні access_hash"}
    per_term, samples, total = {}, [], 0
    for t in terms:
        try:
            msgs = await client.get_messages(entity, search=t, limit=PER_TERM)
        except FloodWaitError:
            raise
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {str(e)[:80]}"}
        n = 0
        for m in msgs:
            if not m.date or m.date < since:
                continue
            n += 1
            if (m.message or "").strip() and len(samples) < 40:
                samples.append({
                    "term": t, "mid": m.id, "date": m.date.isoformat()[:16],
                    "text": re.sub(r"\s+", " ", m.message)[:600],
                })
        if n:
            per_term[t] = n
            total += n
        await asyncio.sleep(PAUSE)
    return {"messages": total, "per_term": per_term, "samples": samples}


async def run_account(acc, chunk):
    client = TelegramClient(
        StringSession(acc.session_string), int(acc.api_id), acc.api_hash,
        proxy=acc.proxy.to_telethon_proxy() if acc.proxy else None,
        connection_retries=2, retry_delay=2, timeout=20, **acc.client_kwargs())
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(f"  акаунт #{acc.id}: не авторизований", flush=True)
            return
        for c in chunk:
            try:
                res = await scan_chat(client, c)
            except FloodWaitError as e:
                print(f"  акаунт #{acc.id}: FloodWait {e.seconds}s — стоп", flush=True)
                return
            rec = {**c, **res}
            async with _lock:
                done[str(c["tg_id"])] = rec
                stat["err" if res.get("error") else "ok"] += 1
                if res.get("messages"):
                    stat["hits"] += 1
                n = stat["ok"] + stat["err"]
                if n % 20 == 0:
                    DST.write_text(json.dumps(done, ensure_ascii=False, indent=1))
                    print(f"  {n}/{len(todo)}  з влученнями: {stat['hits']}, "
                          f"помилок: {stat['err']}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  акаунт #{acc.id} впав: {type(e).__name__}: {str(e)[:80]}", flush=True)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def main():
    sem = asyncio.Semaphore(CONCURRENCY)

    async def guarded(aid, chunk):
        async with sem:
            await run_account(accounts[aid], chunk)

    await asyncio.gather(*(guarded(a, ch) for a, ch in by_acc.items()))
    DST.write_text(json.dumps(done, ensure_ascii=False, indent=1))


asyncio.run(main())

rows = [v for v in done.values() if not v.get("error")]
rows.sort(key=lambda r: -(r.get("messages") or 0))
hit = [r for r in rows if r.get("messages")]
print(f"\nперевірено {len(rows)}, з влученнями {len(hit)}, "
      f"повідомлень усього {sum(r['messages'] for r in hit)}\n")
print(f"{'повідом.':>9}  {'учасн.':>7}  {'точка':<16}{'чат':<40}юзернейм")
for r in hit[:40]:
    print(f"{r['messages']:>9}  {r['members']:>7}  {(r['point'] or r['region'])[:15]:<16}"
          f"{r['title'][:38]:<40}{r['username'] or ('linked:' + (r['parent'] or ''))}")
print(f"\n-> {DST}")
