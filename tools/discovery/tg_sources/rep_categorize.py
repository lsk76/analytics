"""Категоризація джерел довідника: що це за чат/канал за призначенням.

Категорія потрібна для фільтра у звіті: 3887 джерел без розрізнення — це купа,
а «покажи міські чати Дагестану» — робочий інструмент.

Класифікуємо по назві й опису (більшого в нас і немає). Пишемо в Channel.topics:
перший елемент — головна категорія, далі можуть іти додаткові.

Env: CAT_APPLY=1 — писати; CAT_LIMIT; MODEL; BATCH; CONCURRENCY
Запуск: docker compose exec -T web python manage.py shell < _dir/rep_categorize.py
"""
import asyncio
import json
import os
import re

from django.conf import settings

from analysis.models import Channel, Region
from analysis.services import llm

APPLY = os.environ.get("CAT_APPLY") == "1"
LIMIT = int(os.environ.get("CAT_LIMIT", 0))
MODEL = os.environ.get("MODEL", "google/gemini-2.5-flash")
BATCH = int(os.environ.get("BATCH", 40))
CONCURRENCY = int(os.environ.get("CONCURRENCY", 8))

REGIONS = ["Дагестан", "Башкортостан", "Саха (Якутія)", "Бурятія", "Алтай",
           "Алтайський край", "Іркутська область", "Кемеровська область",
           "Красноярський край", "Новосибірська область", "Омська область",
           "Томська область", "Тива", "Хакасія", "Амурська область",
           "Забайкальський край", "Камчатський край", "Магаданська область",
           "Приморський край", "Сахалінська область", "Хабаровський край",
           "Єврейська АО", "Чукотський АО"]

CATS = {
    "новини": "региональные/городские новости и СМИ, ЧП-каналы, сводки происшествий",
    "міський чат": "общий чат города/района, «болталка», «Подслушано», обсуждения",
    "влада": "官方 каналы администраций, госорганов, депутатов, ведомств",
    "барахолка": "куплю/продам, объявления, барахолка, доска объявлений",
    "робота": "вакансии, работа, подработка, поиск сотрудников",
    "транспорт": "такси, попутчики, перевозки, автолюбители, ДПС, дороги",
    "нерухомість": "аренда и продажа жилья, новостройки, риелторы",
    "бізнес": "реклама, магазины, услуги, доставка еды, салоны",
    "етнічне/релігійне": "национальные и религиозные сообщества, язык, традиции",
    "дозвілля": "афиша, куда пойти, спорт, юмор, музыка, знакомства",
    "інше": "всё, что не подходит ни к одной категории",
}

SYSTEM = """Ты классифицируешь Telegram-каналы и чаты российских регионов по
НАЗНАЧЕНИЮ. Видишь только название и описание.

Категории (выбери ОДНУ главную):
""" + "\n".join(f"  {k} — {v}" for k, v in CATS.items()) + """

Правила:
* «ЧП», «Инцидент», «Типичный <город>» без явной барахолки — это новини;
* группа обсуждения новостного канала — тоже новини (это комментарии к новостям);
* если в названии и барахолка, и работа — выбирай то, что в названии первое;
* сомневаешься между міський чат и новини — смотри, кто пишет: если редакция,
  то новини, если сами жители, то міський чат.

Ответ — строгий JSON, ровно столько элементов, сколько на входе:
{"items":[{"i":0,"cat":"новини"}]}"""


def rows():
    ids = list(Region.objects.filter(name__in=REGIONS).values_list("id", flat=True))
    qs = (Channel.objects.filter(region_subject_id__in=ids, comments_open=True)
          .exclude(topics__contains=["_cat"])      # уже категоризовані
          .only("id", "title", "description", "topics")
          .order_by("-subscribers"))
    out = [{"id": c.id, "title": c.title or "",
            "desc": (c.description or "")[:250], "topics": c.topics or []}
           for c in qs]
    return out[:LIMIT] if LIMIT else out


def parse(raw, n):
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}
    return {it["i"]: it.get("cat", "інше") for it in data.get("items", [])
            if isinstance(it.get("i"), int) and 0 <= it["i"] < n}


async def main(items):
    sem, client = asyncio.Semaphore(CONCURRENCY), llm.make_client()
    batches = [items[i:i + BATCH] for i in range(0, len(items), BATCH)]
    out = {}

    async def one(b):
        async with sem:
            user = "\n".join(
                f'{i}. {c["title"][:80]} | {c["desc"].replace(chr(10), " ")[:180]}'
                for i, c in enumerate(b))
            try:
                raw = await llm.query(
                    [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
                    model=MODEL, client=client, max_tokens=3000, json_mode=True)
            except Exception as e:  # noqa: BLE001
                print(f"  батч впав: {type(e).__name__}")
                return
            for i, cat in parse(raw, len(b)).items():
                out[b[i]["id"]] = cat if cat in CATS else "інше"

    try:
        await asyncio.gather(*(one(b) for b in batches))
    finally:
        await client.close()
    return out


items = rows()
print(f"до категоризації: {len(items)}", flush=True)
verdicts = asyncio.run(main(items)) if items else {}
print(f"отримано вердиктів: {len(verdicts)}")

import collections
print("розкладка:", dict(collections.Counter(verdicts.values()).most_common()))

if APPLY and verdicts:
    objs = {c.id: c for c in Channel.objects.filter(id__in=verdicts)}
    upd = []
    for cid, cat in verdicts.items():
        c = objs.get(cid)
        if not c:
            continue
        # «_cat» — маркер, що рядок уже проходив категоризацію (щоб не платити двічі)
        c.topics = [cat, "_cat"]
        upd.append(c)
    Channel.objects.bulk_update(upd, ["topics"], batch_size=500)
    print(f"ЗАПИСАНО в довідник: {len(upd)}")
else:
    print("нічого не записано (CAT_APPLY=1 щоб застосувати)")
