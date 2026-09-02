"""Цільовий пошук чатів ПРО КОРДОН — глобальним пошуком Telegram.

Навіщо окремо від основного конвеєра: він шукав джерела за ГЕОПРИНЦИПОМ
(місто -> його паблік), а чати про перетин організовані за ПЕРЕХОДОМ, і назва в
них тематична, а не географічна. Тому геопошук їх систематично відрізав: вони або
канали без linked-групи, або відсіялися junk-фільтром і city_ok.

Що робить: `contacts.Search` по темі й назвах переходів -> для кожної знахідки
GetFullChannel (учасники, linked-група = чи є коментарі) -> файл із результатом.

Env: DISC_ACCOUNTS (скільки акаунтів задіяти, дефолт 4), DISC_PAUSE (2.0).
Вихід: _dir/border_topic_discovery.json
"""
import asyncio
import json
import os
from pathlib import Path

from telethon import TelegramClient, functions
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest

from accounts.models import TelegramAccount

DST = Path("_dir/border_topic_discovery.json")
N_ACC = int(os.environ.get("DISC_ACCOUNTS", 4))
PAUSE = float(os.environ.get("DISC_PAUSE", 2.0))

# Пошукові фрази: назви переходів + тематичні слова. Глобальний пошук Telegram
# шукає по НАЗВАХ і юзернеймах публічних чатів, тож фрази мають бути такі, як
# люди називають групи: «МАПП Забайкальск», «очередь на границе».
TERMS = [t.strip() for t in os.environ.get("DISC_TERMS", "").split("|") if t.strip()] or [
    "Забайкальск граница", "МАПП Забайкальск", "Маньчжурия", "очередь на границе",
    "погранпереход", "пункт пропуска", "МАПП",
]


def _jsonable(o):
    import base64
    import datetime as dt
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    if isinstance(o, bytes):
        return base64.b64encode(o).decode()
    return o


async def search_terms(client, terms, found):
    for t in terms:
        try:
            res = await client(functions.contacts.SearchRequest(q=t, limit=50))
        except FloodWaitError as e:
            print(f"  FloodWait {e.seconds}s на «{t}» — стоп акаунта", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            print(f"  «{t}»: {type(e).__name__}", flush=True)
            await asyncio.sleep(PAUSE)
            continue
        n = 0
        for ch in res.chats:
            if getattr(ch, "id", None) is None:
                continue
            key = str(ch.id)
            if key in found:
                found[key]["terms"].append(t)
                continue
            found[key] = {
                "tg_id": ch.id, "access_hash": getattr(ch, "access_hash", None),
                "title": getattr(ch, "title", ""),
                "username": getattr(ch, "username", "") or "",
                "megagroup": bool(getattr(ch, "megagroup", False)),
                "broadcast": bool(getattr(ch, "broadcast", False)),
                "terms": [t],
            }
            n += 1
        print(f"  «{t[:32]}»: +{n} нових (усього {len(found)})", flush=True)
        await asyncio.sleep(PAUSE)


async def enrich(client, items):
    """Для кожної знахідки: учасники, опис, linked-група (= чи є коментарі)."""
    for it in items:
        if it.get("members") is not None or it.get("error"):
            continue
        try:
            handle = it["username"] or None
            if not handle:
                it["error"] = "без юзернейма"
                continue
            ent = await client.get_entity(handle)
            full = await client(GetFullChannelRequest(ent))
            fc = _jsonable(full.to_dict()).get("full_chat", {})
            it["members"] = fc.get("participants_count")
            it["about"] = (fc.get("about") or "").replace("\n", " ")[:300]
            it["linked_chat_id"] = fc.get("linked_chat_id")
            it["participants_visible"] = fc.get("can_view_participants")
        except FloodWaitError as e:
            print(f"  FloodWait {e.seconds}s на збагаченні — стоп", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            it["error"] = f"{type(e).__name__}: {str(e)[:70]}"
        await asyncio.sleep(PAUSE)


# ORM не можна чіпати з async-контексту — тягнемо акаунти ДО asyncio.run
ACCS = list(TelegramAccount.objects.filter(is_authenticated=True, is_active=True)
            .exclude(session_string="").select_related("proxy").order_by("-id")[:N_ACC])


async def main():
    accs = ACCS
    if not accs:
        print("немає акаунтів")
        return
    found = json.loads(DST.read_text()) if DST.exists() else {}
    chunks = [TERMS[i::len(accs)] for i in range(len(accs))]
    print(f"акаунтів {len(accs)}, фраз {len(TERMS)}, уже знайдено {len(found)}", flush=True)

    async def run(acc, terms):
        client = TelegramClient(
            StringSession(acc.session_string), int(acc.api_id), acc.api_hash,
            proxy=acc.proxy.to_telethon_proxy() if acc.proxy else None,
            connection_retries=2, retry_delay=2, timeout=20, **acc.client_kwargs())
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return
            await search_terms(client, terms, found)
            mine = [v for i, v in enumerate(found.values()) if i % len(accs) == accs.index(acc)]
            await enrich(client, mine)
        except Exception as e:  # noqa: BLE001
            print(f"  акаунт #{acc.id}: {type(e).__name__}", flush=True)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    await asyncio.gather(*(run(a, c) for a, c in zip(accs, chunks)))
    DST.write_text(json.dumps(found, ensure_ascii=False, indent=1))


asyncio.run(main())

rows = [v for v in json.loads(DST.read_text()).values()]
chats = [r for r in rows if r.get("megagroup")]
chans = [r for r in rows if r.get("broadcast")]
withc = [r for r in chans if r.get("linked_chat_id")]
print(f"\nзнайдено {len(rows)}: чатів {len(chats)}, каналів {len(chans)} "
      f"(з коментарями {len(withc)})\n")
rows.sort(key=lambda r: -(r.get("members") or 0))
print(f"{'учасн.':>8}  {'тип':<10}{'коментарі':<11}назва / юзернейм")
for r in rows[:40]:
    kind = "чат" if r.get("megagroup") else "канал"
    com = ("є" if r.get("linked_chat_id") else "—") if r.get("broadcast") else "сам чат"
    print(f"{(r.get('members') or 0):>8}  {kind:<10}{com:<11}"
          f"{r['title'][:36]} @{r['username'] or '—'}")
print(f"\n-> {DST}")
