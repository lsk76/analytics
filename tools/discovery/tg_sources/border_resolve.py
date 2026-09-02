"""Крок 4a: резолв кандидатів + пошук linked-груп. Зберігає ВСЕ, що віддав Telegram.

Для кожного кандидата:
  get_entity(username) -> GetFullChannelRequest -> якщо у full є linked_chat_id,
  резолвимо і саму linked-групу (це і є «коментарі каналу»).

У файл кладеться сирий `to_dict()` кожного об'єкта — сутність, ChannelFull, і те
саме для linked-групи. Нічого не відкидається: пізніше з цього можна дістати
будь-яке поле, не переопитуючи Telegram (а резолв має добовий ліміт ~200/акаунт).

Ключове, що звідти беремо далі:
  participants_count      — скільки учасників;
  can_view_participants   — чи відкритий СПИСОК учасників;
  linked_chat_id          — чи ввімкнені коментарі в каналу;
  slowmode_enabled/seconds, restricted, join_request/join_to_send — режим письма.

Кандидат закріплюється за акаунтом детерміновано (за хешем юзернейма): резолв
кешується в сесії ТОГО акаунта, що резолвив, тож повторний прогін не платить удруге.
Вступів у закриті групи НЕ робимо (рішення замовника) — інвайт-посилання пропускаємо.

Env: RES_CONCURRENCY (акаунтів паралельно, дефолт 8), RES_PAUSE (сек між запитами
     в межах акаунта, дефолт 3.5), RES_LIMIT (стеля кандидатів за прогін).
Запуск: docker compose exec -T web python manage.py shell < _dir/border_resolve.py
Вихід:  _dir/border_resolve_raw.json  (ідемпотентний)
"""
import asyncio
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest

from accounts.models import TelegramAccount

SRC = Path("_dir/border_longlist.json")
PROF = Path("_dir/border_profiles.json")
DST = Path("_dir/border_resolve_raw.json")

CONCURRENCY = int(os.environ.get("RES_CONCURRENCY", 8))
PAUSE = float(os.environ.get("RES_PAUSE", 3.5))
LIMIT = int(os.environ.get("RES_LIMIT", 0))
FIT = ("news_regional", "city_talk", "problems")


def jsonable(o):
    """to_dict() Telethon містить datetime і bytes — робимо їх серіалізовними."""
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    if isinstance(o, bytes):
        return base64.b64encode(o).decode()
    return o


# ---- черга ----------------------------------------------------------------
items = [c for c in json.loads(SRC.read_text())["items"] if not c["junk_hint"]]
prof = json.loads(PROF.read_text())
queue = []
for c in items:
    v = prof.get(c["username"].lower())
    if not (v and v["region_ok"] and v["city_ok"] and v["profile"] in FIT):
        continue
    u = c["username"]
    if u.startswith("+"):          # закрита група за інвайтом — не вступаємо
        continue
    queue.append(c)

done = json.loads(DST.read_text()) if DST.exists() else {}
todo = [c for c in queue if c["username"].lower() not in done]
if LIMIT:
    todo = todo[:LIMIT]

accounts = list(TelegramAccount.objects.filter(is_authenticated=True, is_active=True)
                .exclude(session_string="").select_related("proxy").order_by("id"))
print(f"придатних: {len(queue)}, до резолву: {len(todo)}, акаунтів: {len(accounts)}, "
      f"паралельно: {CONCURRENCY}, пауза {PAUSE}с", flush=True)
if not todo or not accounts:
    raise SystemExit(0)

# детерміновий розподіл: той самий кандидат -> той самий акаунт (кеш резолву)
buckets = {a.id: [] for a in accounts}
for c in todo:
    h = int(hashlib.md5(c["username"].lower().encode()).hexdigest()[:8], 16)
    buckets[accounts[h % len(accounts)].id].append(c)
print("розподіл по акаунтах: " + ", ".join(f"#{k}:{len(v)}" for k, v in buckets.items()),
      flush=True)

_lock = asyncio.Lock()
stats = {"ok": 0, "linked": 0, "err": 0, "flood": 0}


async def resolve_one(client, handle):
    """-> (сирий dict сутності, сирий dict ChannelFull) або кидає."""
    ent = await client.get_entity(handle)
    full = await client(GetFullChannelRequest(ent))
    return jsonable(ent.to_dict()), jsonable(full.to_dict())


async def run_account(acc, chunk):
    if not chunk:
        return
    client = TelegramClient(
        StringSession(acc.session_string), int(acc.api_id), acc.api_hash,
        proxy=acc.proxy.to_telethon_proxy() if acc.proxy else None,
        connection_retries=2, retry_delay=2, timeout=20, **acc.client_kwargs())
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(f"  акаунт #{acc.id}: не авторизований — пропускаю", flush=True)
            return
        for c in chunk:
            u = c["username"]
            handle = u.split(":", 1)[1] if u.startswith("linked:") else u
            rec = {"username": u, "handle": handle, "point": c["point"],
                   "region": c["region"], "region_id": c["region_id"],
                   "chat_type_guess": c["chat_type"], "account_id": acc.id}
            try:
                ent_raw, full_raw = await resolve_one(client, handle)
                rec["entity"] = ent_raw
                rec["full"] = full_raw
                cid = (full_raw.get("full_chat") or {}).get("linked_chat_id")
                if cid:
                    await asyncio.sleep(PAUSE)
                    try:
                        l_ent, l_full = await resolve_one(client, cid)
                        rec["linked_entity"] = l_ent
                        rec["linked_full"] = l_full
                    except Exception as e:  # noqa: BLE001
                        rec["linked_error"] = f"{type(e).__name__}: {str(e)[:90]}"
                rec["ok"] = True
            except FloodWaitError as e:
                async with _lock:
                    stats["flood"] += 1
                print(f"  акаунт #{acc.id}: FloodWait {e.seconds}s — зупиняю акаунт",
                      flush=True)
                return
            except Exception as e:  # noqa: BLE001
                rec["ok"] = False
                rec["error"] = f"{type(e).__name__}: {str(e)[:110]}"

            async with _lock:
                done[u.lower()] = rec
                stats["ok" if rec.get("ok") else "err"] += 1
                if rec.get("linked_entity"):
                    stats["linked"] += 1
                n = stats["ok"] + stats["err"]
                if n % 25 == 0:
                    DST.write_text(json.dumps(done, ensure_ascii=False, indent=1))
                    print(f"  {n}/{len(todo)}  ok={stats['ok']} linked={stats['linked']} "
                          f"err={stats['err']}", flush=True)
            await asyncio.sleep(PAUSE)
    except Exception as e:  # noqa: BLE001
        print(f"  акаунт #{acc.id}: впав — {type(e).__name__}: {str(e)[:90]}", flush=True)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def main():
    sem = asyncio.Semaphore(CONCURRENCY)

    async def guarded(acc):
        async with sem:
            await run_account(acc, buckets[acc.id])

    await asyncio.gather(*(guarded(a) for a in accounts))
    DST.write_text(json.dumps(done, ensure_ascii=False, indent=1))


asyncio.run(main())
print(f"\n✓ у файлі: {len(done)} записів | ok={stats['ok']} "
      f"з linked-групою={stats['linked']} помилок={stats['err']} flood={stats['flood']}")
print(f"-> {DST}")
