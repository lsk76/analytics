"""
Чи бачить Telemetr.io НАШІ канали — і чи віддає їхні пости повністю.

Два питання, які вирішуються БЕЗ жодного пошукового терміна (найдефіцитніша
квота), тому це має бути перший реальний тест, а не head-to-head пошук.

  --coverage  : скільки з наших Channel'ів Telemetr.io взагалі знає і ТРЕКАЄ,
                у розрізі chat_type (канал / чат / linked-обговорення).
                Міряється через `resolve_telegram_id`: він приймає наш
                Telegram numeric id і повертає {internal_id, tracked}.
                ВИМІРЯНО 2026-09-01: `channels/search` для цього НЕ годиться —
                він шукає за НАЗВОЮ, а не за username, і на `@toporlive`
                впевнено віддає інший канал зі схожим титулом. Тому він тут
                лише запасний варіант для каналів без tg_id.
                Ціна: 1 запит на канал, 0 термінів, 0 унікальних каналів
                (виміряно дельтою квоти, не припущено).

  --messages  : повнота стрічки одного каналу — /v1/messages/channel за вікно
                проти того, що вже лежить у нашій БД за той самий канал і вікно.
                Зіставлення за нормалізованим текстом (id-простори різні).
                Ціна: 1-2 запити + 1 УНІКАЛЬНИЙ КАНАЛ (з 5 на місяць).

  python manage.py telemetrio_channels --coverage --per-type 50 --with-region
  python manage.py telemetrio_channels --coverage --usernames rian_ru,ekhodagestan
  python manage.py telemetrio_channels --messages --username ekhodagestan --days 7
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from analysis.models import Channel, Post
from analysis.services.telemetrio import (
    TelemetrioAccessError, TelemetrioClient, TelemetrioError, TelemetrioQuotaError,
)
from .telemetrio_vs_telezip import text_key


class Command(BaseCommand):
    help = "Покриття наших каналів у Telemetr.io і повнота стрічки каналу."

    def add_arguments(self, parser):
        parser.add_argument("--coverage", action="store_true")
        parser.add_argument("--messages", action="store_true")
        parser.add_argument("--usernames", default="",
                            help="Comma-separated; інакше беремо з таблиці Channel.")
        parser.add_argument("--username", default="", help="Один канал для --messages.")
        parser.add_argument("--limit", type=int, default=30,
                            help="Скільки каналів із БД перевіряти в --coverage (без --per-type).")
        parser.add_argument("--per-type", type=int, default=0,
                            help="Вибірка по N каналів НА КОЖЕН chat_type (channel/chat/"
                                 "discussion). Головна цифра тесту: Telemetr.io — сервіс "
                                 "аналітики каналів, і покриття чатів апріорі інше.")
        parser.add_argument("--with-region", action="store_true",
                            help="Лише канали з проставленим region_subject (наш робочий кістяк).")
        parser.add_argument("--random", action="store_true",
                            help="Випадкова вибірка замість топ-N за підписниками. "
                                 "Топ-вибірка систематично завищує покриття — великі "
                                 "канали індексують усі; чесна цифра саме тут.")
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--max-pages", type=int, default=20,
                            help="Стеля сторінок стрічки (кожна ~22 повідомлення = 1 запит).")
        parser.add_argument("--vs-telezip", action="store_true",
                            help="Зіставити стрічку ще й з TeleZip по тому самому каналу "
                                 "й вікну (searchTerm='*' + channelNames). Дає чесне "
                                 "порівняння ПОВНОТИ без витрати пошукових термінів "
                                 "Telemetr.io — єдиний доступний шлях, поки Alpha-пошук "
                                 "вимкнено на ключі.")

    def handle(self, *a, **o):
        if not settings.TELEMETRIO_API_KEY:
            raise CommandError("TELEMETRIO_API_KEY порожній — додай у .env.")
        if not (o["coverage"] or o["messages"]):
            raise CommandError("Вкажи --coverage і/або --messages.")
        asyncio.run(self._run(o))

    async def _run(self, o):
        async with TelemetrioClient(settings.TELEMETRIO_API_KEY,
                                    settings.TELEMETRIO_BASE_URL) as tm:
            before = await tm.usage()
            if o["coverage"]:
                await self._coverage(tm, o)
            if o["messages"]:
                await self._messages(tm, o)
            after = await tm.usage()
            self.stdout.write(self.style.MIGRATE_HEADING("\nФактична ціна прогону"))
            for k in ("requests", "channels", "search_messages_requests", "search_terms"):
                b, a_ = (before.get(k) or {}).get("spent"), (after.get(k) or {}).get("spent")
                if isinstance(a_, int) and isinstance(b, int) and a_ != b:
                    self.stdout.write(f"  {k:26}: {b} -> {a_}  (+{a_ - b})")
            self.stdout.write(f"  HTTP-запитів: {tm.requests_made}")

    # ------------------------------------------------------------------
    async def _coverage(self, tm, o):
        names = [x.strip().lstrip("@") for x in (o["usernames"] or "").split(",") if x.strip()]
        groups: dict[str, list] = {}
        if names:
            groups["(явний список)"] = await sync_to_async(list)(
                Channel.objects.filter(username__in=names)
                .values("username", "title", "tg_id", "chat_type", "subscribers"))
        elif o["per_type"]:
            for ct in ("channel", "chat", "discussion"):
                qs = Channel.objects.filter(tg_id__isnull=False, chat_type=ct)
                if o["with_region"]:
                    qs = qs.filter(region_subject__isnull=False)
                groups[ct] = await sync_to_async(list)(
                    qs.order_by("?" if o["random"] else "-subscribers")
                    .values("username", "title", "tg_id", "chat_type", "subscribers")[:o["per_type"]])
        else:
            qs = Channel.objects.filter(tg_id__isnull=False)
            if o["with_region"]:
                qs = qs.filter(region_subject__isnull=False)
            groups["усі"] = await sync_to_async(list)(
                qs.order_by("?" if o["random"] else "-subscribers")
                .values("username", "title", "tg_id", "chat_type", "subscribers")[:o["limit"]])
        if not any(groups.values()):
            raise CommandError("Немає каналів для перевірки.")

        totals = {}
        for label, rows in groups.items():
            if not rows:
                continue
            known = tracked = 0
            misses = []
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n[{label}] {len(rows)} наших каналів"))
            for r in rows:
                iid = is_tracked = None
                if r["tg_id"]:
                    res = await tm.resolve_telegram_id(r["tg_id"])
                    if res and res.get("internal_id"):
                        iid, is_tracked = res["internal_id"], bool(res.get("tracked"))
                else:
                    # fallback лише для каналів без tg_id; матч за назвою — ненадійний,
                    # тому позначаємо його окремо (~) і не рахуємо як tracked.
                    found = await tm.search_channels(r["username"], limit=1)
                    if found:
                        iid = "~" + str(found[0].get("internal_id"))
                if not iid:
                    misses.append(r["username"] or str(r["tg_id"]))
                    continue
                known += 1
                tracked += 1 if is_tracked else 0
                if o["verbosity"] >= 2:
                    self.stdout.write(
                        f"  {(r['username'] or '-'):28} → {iid:>9} tracked={is_tracked} "
                        f"{r['subscribers']:>8} підп.  {(r['title'] or '')[:34]}")
            totals[label] = (known, tracked, len(rows))
            self.stdout.write(
                f"  знає {known}/{len(rows)} ({100*known/len(rows):.0f}%), "
                f"з них ТРЕКАЄ {tracked} ({100*tracked/len(rows):.0f}% від наших)")
            if misses:
                self.stdout.write(f"  не знає: {', '.join(misses[:15])}"
                                  + (" …" if len(misses) > 15 else ""))

        self.stdout.write(self.style.MIGRATE_HEADING("\nПідсумок покриття"))
        for label, (known, tracked, total) in totals.items():
            self.stdout.write(f"  {label:14}: знає {known:>4}/{total:<4} "
                              f"трекає {tracked:>4}/{total:<4}")
        self.stdout.write(
            "\n  ЧИТАТИ ТАК: `tracked=false` = Telemetr.io знає такий чат, але статистику\n"
            "  по ньому не збирає, отже його постів у пошуку не буде. Це стеля повноти\n"
            "  для нашого корпусу — і вона від тарифу не залежить.")

    # ------------------------------------------------------------------
    async def _messages(self, tm, o):
        uname = (o["username"] or "").strip().lstrip("@")
        if not uname:
            raise CommandError("--messages потребує --username.")
        ch = await sync_to_async(
            lambda: Channel.objects.filter(username__iexact=uname).first())()
        internal_id = None
        if ch and ch.tg_id:
            res = await tm.resolve_telegram_id(ch.tg_id)
            internal_id = (res or {}).get("internal_id")
        if not internal_id:
            found = await tm.search_channels(uname, limit=1)
            internal_id = found[0].get("internal_id") if found else None
        if not internal_id:
            raise CommandError(f"Telemetr.io не знає канал {uname!r}.")

        date_to = datetime.now(timezone.utc)
        date_from = date_to - timedelta(days=o["days"])
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nПовнота стрічки {uname} ({internal_id}) за {o['days']} дн."))
        self.stdout.write(self.style.WARNING(
            "  УВАГА: цей виклик витрачає 1 УНІКАЛЬНИЙ КАНАЛ (на free-плані їх 5/міс)."))

        # Канал і чат — РІЗНІ ендпоінти; /messages/channel на групі поверне
        # порожньо, і це виглядало б як «нема покриття» замість «не той виклик».
        is_group = (ch.chat_type in ("chat", "discussion")) if ch else False
        try:
            if is_group:
                msgs, cursor, pages = [], None, 0
                while pages < o["max_pages"]:
                    batch, _cs, cursor = await tm.group_messages(
                        internal_id, from_date=date_from, to_date=date_to, cursor=cursor)
                    pages += 1
                    msgs.extend(batch)
                    if not cursor or not batch:
                        break
            else:
                msgs = await tm.channel_messages_all(
                    internal_id, from_date=date_from, to_date=date_to, max_pages=o["max_pages"])
        except TelemetrioAccessError as e:
            # Найчастіший випадок на free-ключі: чат не verified. Це відповідь про
            # ТАРИФ, а не про наявність даних, тож і звучати має саме так.
            self.stderr.write(self.style.ERROR(
                f"  {uname}: Telemetr.io відмовив у доступі до цього чату.\n"
                f"  {e}\n"
                f"  Тобто на free-ключі стрічка НЕ verified-чатів недоступна взагалі — "
                f"перевірити повноту по нашому корпусу можна лише на платному тарифі."))
            return
        self.stdout.write(f"  ендпоінт: /v1/messages/{'group' if is_group else 'channel'} "
                          f"(chat_type={ch.chat_type if ch else '?'})")
        tm_keys = {k for k in (text_key(m.get("text") or "") for m in msgs) if k}

        db_posts = []
        if ch:
            db_posts = await sync_to_async(list)(
                Post.objects.filter(channel=ch, posted_at__gte=date_from,
                                    posted_at__lte=date_to).values("text", "url", "posted_at"))
        db_keys = {k for k in (text_key(p["text"]) for p in db_posts) if k}

        both = tm_keys & db_keys
        self.stdout.write(f"  Telemetr.io : {len(msgs)} повідомлень ({len(tm_keys)} зіставних)")
        self.stdout.write(f"  наша БД     : {len(db_posts)} постів ({len(db_keys)} зіставних)")
        self.stdout.write(f"  перетин     : {len(both)}")
        if db_keys:
            self.stdout.write(self.style.SUCCESS(
                f"  Telemetr.io покриває {100 * len(both) / len(db_keys):.1f}% того, "
                f"що ми вже маємо по цьому каналу"))
        else:
            self.stdout.write("  (у нашій БД за це вікно порожньо — порівнювати нема з чим; "
                              "візьми канал з активного моніторингу)")
        if o["vs_telezip"]:
            await self._vs_telezip(uname, tm_keys, len(msgs), date_from, date_to)

        if is_group:
            # GroupMessage не має ні views/forwards, ні is_ad/deleted_at, ні reply_to —
            # тобто для чатів Telemetr.io не дає ані наших полів, ані своїх бонусних.
            with_sender = sum(1 for m in msgs if m.get("from_peer"))
            self.stdout.write(f"  у групових повідомленнях: відправник є у {with_sender}/"
                              f"{len(msgs)}; reply_to / ім'я автора / views — відсутні як поля")
        else:
            deleted = sum(1 for m in msgs if m.get("deleted_at"))
            ads = sum(1 for m in msgs if m.get("is_ad"))
            self.stdout.write(f"  бонус Telemetr.io: {deleted} видалених постів (ми їх не бачимо "
                              f"взагалі), {ads} позначених як реклама, є views/forwards/reactions")

    async def _vs_telezip(self, uname, tm_keys, tm_total, date_from, date_to):
        """Та сама стрічка з TeleZip: searchTerm='*' звужений до одного каналу.

        Це не пошук за темою, а вивантаження каналу — тому порівнюється саме
        ПОВНОТА індексу, а не якість ранжування, і Telemetr.io-квота термінів
        не витрачається взагалі."""
        from django.conf import settings as dj
        from analysis.services.telezip import TelezipClient
        self.stdout.write(self.style.MIGRATE_HEADING("\n  TeleZip на тому ж каналі й вікні"))
        try:
            async with TelezipClient(dj.TELEZIP_API_KEY, dj.TELEZIP_BASE_URL) as tz:
                tz_msgs = await tz.find_posts_range(
                    "*", date_from, date_to, channel_names=[uname], unique=True)
        except Exception as e:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f"  TeleZip не відповів: {e}"))
            return
        tz_keys = {k for k in (text_key(m.get("content") or "") for m in tz_msgs) if k}
        both = tm_keys & tz_keys
        self.stdout.write(f"  TeleZip     : {len(tz_msgs)} повідомлень ({len(tz_keys)} зіставних)")
        self.stdout.write(f"  перетин     : {len(both)}")
        if tz_keys:
            self.stdout.write(f"  Telemetr.io покриває {100*len(both)/len(tz_keys):.1f}% TeleZip")
        if tm_keys:
            self.stdout.write(f"  TeleZip покриває {100*len(both)/len(tm_keys):.1f}% Telemetr.io")
        self.stdout.write(f"  лише TeleZip: {len(tz_keys - tm_keys)}   "
                          f"лише Telemetr.io: {len(tm_keys - tz_keys)}")
