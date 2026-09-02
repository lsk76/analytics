"""Крок 6: заливка результатів розвідки в довідник Channel.

Джерело — сирий дамп Telegram (_dir/border_resolve_raw.json) плюс лонг-ліст
(гео і профіль). Пишемо і сам канал/чат, і його linked-групу як ОКРЕМИЙ Channel,
звʼязані через Channel.linked_chat.

Зіставлення з наявними рядками: спершу за tg_id (там unique-констрейнт), далі за
юзернеймом без регістру. Непорожні поля довідника НЕ затираємо порожнім — оновлюємо
лише те, що реально дізналися.

Окремо рахуємо «дублі-привиди»: у довіднику є рядки-заглушки з юзернеймом
`linked:<канал>`, заведені колись під групи обговорення. Тепер у тієї ж групи є
справжній юзернейм і tg_id — якщо заглушка не має tg_id, зіставити її нічим, і
вона лишиться другим рядком про той самий чат. Такі показуємо списком, не чіпаючи.

Запуск: docker compose exec -T web python manage.py shell < _dir/border_import_directory.py
Env: IMP_APPLY=1 — писати в БД (без нього тільки рахує).
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from django.db import transaction

from accounts.models import TelegramAccount
from analysis.models import Channel

APPLY = os.environ.get("IMP_APPLY") == "1"
RAW = json.loads(Path("_dir/border_resolve_raw.json").read_text())
LL = {c["username"].lower(): c
      for c in json.loads(Path("_dir/border_longlist.json").read_text())["items"]}

stats = {"створено": 0, "оновлено": 0, "linked створено": 0, "linked оновлено": 0,
         "звʼязків": 0, "мертвих": 0, "привиди": 0}
ghosts = []


def find(tg_id, username):
    ch = Channel.objects.filter(tg_id=tg_id).first() if tg_id else None
    if ch:
        return ch
    if username:
        return Channel.objects.filter(username__iexact=username).first()
    return None


def upsert(ent, full, *, chat_type, region_id, settlement, account_id,
           comments_open=None, linked=None):
    """Створити або оновити Channel із сирих даних. Порожнім не затираємо."""
    tg_id = ent.get("id")
    username = ent.get("username") or ""
    ch = find(tg_id, username)
    created = ch is None
    if created:
        ch = Channel(tg_id=tg_id, username=username)

    ch.title = ent.get("title") or ch.title
    about = (full.get("about") or "").strip()
    if about:
        ch.description = about
    if full.get("participants_count"):
        ch.subscribers = full["participants_count"]
    if tg_id and not ch.tg_id:
        ch.tg_id = tg_id
    if username and not ch.username:
        ch.username = username

    ch.chat_type = chat_type
    ch.is_channel = chat_type == "channel"
    if region_id:
        ch.region_subject_id = region_id
    if settlement:
        ch.settlement = settlement
    if comments_open is not None:
        ch.comments_open = comments_open
    ch.participants_visible = full.get("can_view_participants")
    ch.access = "public" if username else "closed"
    ch.enriched = True
    ch.fetched_at = datetime.now(timezone.utc)
    if account_id:
        ch.joined_by_id = account_id
    ch.check_error = ""
    if linked is not None:
        ch.linked_chat = linked

    # усе, що не заслуговує на власну колонку, але шкода викидати
    meta = dict(ch.directory_meta or {})
    meta["tg_flags"] = {
        "megagroup": ent.get("megagroup"), "gigagroup": ent.get("gigagroup"),
        "broadcast": ent.get("broadcast"), "forum": ent.get("forum"),
        "join_to_send": ent.get("join_to_send"), "join_request": ent.get("join_request"),
        "restricted": ent.get("restricted"), "has_geo": ent.get("has_geo"),
        "slowmode_seconds": full.get("slowmode_seconds"),
        "linked_chat_id": full.get("linked_chat_id"),
    }
    meta["border_import"] = "2026-08-21"
    ch.directory_meta = meta

    if APPLY:
        ch.save()
    return ch, created


acc_ids = set(TelegramAccount.objects.values_list("id", flat=True))

with transaction.atomic():
    for key, rec in RAW.items():
        ll = LL.get(key, {})
        region_id = rec.get("region_id") or ll.get("region_id")
        point = rec.get("point") or ll.get("point") or ""
        account_id = rec.get("account_id") if rec.get("account_id") in acc_ids else None

        if not rec.get("ok"):
            ch = find(None, rec["username"])
            if ch and APPLY:
                ch.access = "dead"
                ch.check_error = (rec.get("error") or "")[:200]
                ch.comments_open = None
                ch.save(update_fields=["access", "check_error", "comments_open"])
            stats["мертвих"] += 1
            continue

        ent = rec.get("entity") or {}
        full = (rec.get("full") or {}).get("full_chat") or {}
        l_ent = rec.get("linked_entity")
        l_full = (rec.get("linked_full") or {}).get("full_chat") or {}

        linked_obj = None
        if l_ent:
            linked_obj, l_created = upsert(
                l_ent, l_full, chat_type="discussion", region_id=region_id,
                settlement=point, account_id=account_id, comments_open=True)
            stats["linked створено" if l_created else "linked оновлено"] += 1
            # заглушка linked:<канал> без tg_id лишиться другим рядком — фіксуємо
            stub = Channel.objects.filter(
                username__iexact=f"linked:{ent.get('username') or ''}").first()
            if stub and stub.pk != linked_obj.pk:
                ghosts.append((stub.pk, stub.username, linked_obj.pk,
                               linked_obj.username or linked_obj.title))
                stats["привиди"] += 1

        is_group = ent.get("megagroup") or ent.get("gigagroup")
        _, created = upsert(
            ent, full,
            chat_type="chat" if is_group else "channel",
            region_id=region_id, settlement=point, account_id=account_id,
            comments_open=(True if (is_group or l_ent) else False),
            linked=linked_obj)
        stats["створено" if created else "оновлено"] += 1
        if linked_obj:
            stats["звʼязків"] += 1

    if not APPLY:
        transaction.set_rollback(True)

print(("ЗАПИСАНО" if APPLY else "ПРОБНИЙ ПРОГІН (IMP_APPLY=1 щоб записати)") + ":")
for k, v in stats.items():
    print(f"  {k:<18}{v}")
if ghosts:
    print(f"\nдублі-привиди (заглушки linked:* без tg_id), перші 10 із {len(ghosts)}:")
    for pk, u, npk, nu in ghosts[:10]:
        print(f"  #{pk} {u:<30} -> тепер є #{npk} {nu}")
    print("  їх НЕ чіпав: злиття потребує окремого рішення (на них можуть висіти пости)")
