"""Скільки РЕАЛЬНО пишуть у знайдених прикордонних чатах.

Питання замовника — «чи є чати, де АКТИВНО обговорюють перетин». Для тематичного
чату («Граница РФ РК», «Верхний Ларс») пошук за словами безглуздий: там усі
повідомлення про це, люди просто не вживають слово «граница». Тому міряємо
загальний обсяг.

Механіка дешева: останній id і id на межі -7 діб (offset_date) -> різниця/7.
Два запити на чат замість вичитування тисяч повідомлень.

ВАЖЛИВО: різниця id рахує і службові повідомлення (вхід/вихід учасників), тож
для груп це верхня оцінка. Для порядку величини цього досить.

Env: ACT_ACCOUNTS (6), ACT_PAUSE (1.5), ACT_MIN_MEMBERS (200)
Вихід: _dir/border_activity.json
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

from accounts.models import TelegramAccount

SRC = Path("_dir/border_topic_discovery.json")
DST = Path("_dir/border_activity.json")
N_ACC = int(os.environ.get("ACT_ACCOUNTS", 6))
PAUSE = float(os.environ.get("ACT_PAUSE", 1.5))
MIN_MEMBERS = int(os.environ.get("ACT_MIN_MEMBERS", 200))
DAYS = 3
CAP = 1200      # стеля вичитування на чат

# Лишаємо тільки те, що справді про перетин; відсіюємо крамниці, перукарні,
# вакансії та асоціацію психотерапевтів (МАПП = ще й «Московская ассоциация...»)
GOOD = re.compile(r"границ|мапп|кпп|апп |очеред|погран|переход|таможн|ларс|"
                  r"маньчжур|суйфэньхэ|хуньчунь|алтанбулаг|цагааннуур|боршоо|"
                  r"бугристое|маштаково|бидаик|валим", re.I)
BAD = re.compile(r"торт|волос|школ|сош|дом культур|крылышк|ножниц|beauty|"
                 r"вакансии|работа в|психиатр|психотерап|магазин|аптек|"
                 r"медицин|тур в|туры в|mapporn|доска коротких", re.I)


def targets():
    d = json.loads(SRC.read_text())
    out = []
    for r in d.values():
        t = r.get("title") or ""
        if not GOOD.search(t) or BAD.search(t):
            continue
        if not r.get("megagroup"):          # канал міряємо окремо, тут лише чати
            continue
        if (r.get("members") or 0) < MIN_MEMBERS:
            continue
        out.append(r)
    out.sort(key=lambda r: -(r.get("members") or 0))
    return out


ACCS = list(TelegramAccount.objects.filter(is_authenticated=True, is_active=True)
            .exclude(session_string="").select_related("proxy").order_by("id")[:N_ACC])
TARGETS = targets()
DONE = json.loads(DST.read_text()) if DST.exists() else {}
TODO = [t for t in TARGETS if str(t["tg_id"]) not in DONE]
print(f"прикордонних чатів: {len(TARGETS)}, до заміру {len(TODO)}, "
      f"акаунтів {len(ACCS)}", flush=True)


async def measure(client, r):
    """РЕАЛЬНИЙ підрахунок за вікно. Різницю id використовувати не можна:
    вона рахує службові події й видалені повідомлення — на @ekaterinburg_granitsa
    дала 17870/добу проти реальних 4, помилка в чотири тисячі разів."""
    since = datetime.now(timezone.utc) - timedelta(days=DAYS)
    handle = r["username"] or None
    if not handle:
        return {"error": "без юзернейма"}
    total = human = 0
    try:
        async for m in client.iter_messages(handle, limit=CAP):
            if not m.date or m.date < since:
                break
            total += 1
            if (m.message or "").strip():
                human += 1
    except FloodWaitError:
        raise
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:70]}"}
    if not total:
        return {"error": "за вікно жодного повідомлення"}
    capped = total >= CAP
    return {"per_day": round(total / DAYS, 1),
            "text_per_day": round(human / DAYS, 1),
            "capped": capped}


async def run(acc, chunk):
    client = TelegramClient(
        StringSession(acc.session_string), int(acc.api_id), acc.api_hash,
        proxy=acc.proxy.to_telethon_proxy() if acc.proxy else None,
        connection_retries=2, retry_delay=2, timeout=20, **acc.client_kwargs())
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return
        for r in chunk:
            try:
                res = await measure(client, r)
            except FloodWaitError as e:
                print(f"  акаунт #{acc.id}: FloodWait {e.seconds}s — стоп", flush=True)
                return
            DONE[str(r["tg_id"])] = {**r, **res}
            await asyncio.sleep(PAUSE)
    except Exception as e:  # noqa: BLE001
        print(f"  акаунт #{acc.id}: {type(e).__name__}", flush=True)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def main():
    if not ACCS or not TODO:
        return
    chunks = [TODO[i::len(ACCS)] for i in range(len(ACCS))]
    await asyncio.gather(*(run(a, c) for a, c in zip(ACCS, chunks)))
    DST.write_text(json.dumps(DONE, ensure_ascii=False, indent=1))


asyncio.run(main())
DST.write_text(json.dumps(DONE, ensure_ascii=False, indent=1))

rows = [v for v in DONE.values() if not v.get("error")]
rows.sort(key=lambda r: -(r.get("per_day") or 0))
print(f"\nпроміряно {len(rows)} чатів\n")
print(f"{'повідом./добу':>14}  {'учасн.':>7}  назва / юзернейм")
for r in rows:
    print(f"{r['per_day']:>14}  {(r.get('members') or 0):>7}  "
          f"{r['title'][:40]:<42}@{r['username']}")
bad = [v for v in DONE.values() if v.get("error")]
if bad:
    print(f"\nне виміряно: {len(bad)}")
