"""
Cheap Telemetr.io smoke test — verify the key and read the quota WITHOUT
spending the scarce axes.

Why a separate command: on the free/test plan the expensive resources are
*unique search terms* (5/month) and *unique channels* (5/month), and neither can
be refunded. Everything this command does by default is free or costs only a
plain request, so it is safe to run repeatedly while wiring things up.

  # free: subscription status + all four quota counters
  python manage.py telemetrio_probe

  # + channel lookup by @username / t.me link (1 request, 0 terms, 0 channels)
  python manage.py telemetrio_probe --channel @rian_ru --channel ekhodagestan

  # + resolve our Telegram numeric ids into Telemetr.io internal ids (1 req each)
  python manage.py telemetrio_probe --tg-ids 1721536340,1797530467

  # DANGEROUS: actually calls /v1/search/messages. Only spends a term if `--term`
  # is not already in the local ledger; it tells you which before asking.
  python manage.py telemetrio_probe --search-term "мобилизация" --yes
"""
from __future__ import annotations

import asyncio
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from analysis.services.telemetrio import (
    TelemetrioAccessError, TelemetrioClient, TelemetrioError, TermLedger,
    default_ledger_path,
)


def _fmt(counter: dict | None) -> str:
    if not counter:
        return "n/a"
    spent, limit = counter.get("spent"), counter.get("limit")
    left = (limit - spent) if isinstance(spent, int) and isinstance(limit, int) else "?"
    return f"{spent}/{limit} (залишок {left})"


class Command(BaseCommand):
    help = "Перевірити ключ Telemetr.io і показати квоти (без витрат)."

    def add_arguments(self, parser):
        parser.add_argument("--channel", action="append", default=[],
                            help="@username / t.me-лінк / назва — пошук каналу. Можна кілька.")
        parser.add_argument("--tg-ids", default="",
                            help="Comma-separated Telegram numeric ids -> internal_id.")
        parser.add_argument("--search-term", default="",
                            help="ВИТРАТНО: пробний /v1/search/messages. Спалює унікальний "
                                 "термін, якщо його ще немає в реєстрі.")
        parser.add_argument("--yes", action="store_true",
                            help="Не питати підтвердження на витрату нового терміна.")
        parser.add_argument("--json", action="store_true", help="Сирий JSON замість таблиці.")

    def handle(self, *a, **o):
        key = settings.TELEMETRIO_API_KEY
        if not key:
            raise CommandError(
                "TELEMETRIO_API_KEY порожній. Додай його в .env (див. .env.example) "
                "і перезапусти контейнер: docker compose restart web worker-collect")
        asyncio.run(self._run(key, o))

    async def _run(self, key: str, o: dict):
        ledger = TermLedger(default_ledger_path())
        async with TelemetrioClient(key, settings.TELEMETRIO_BASE_URL) as tm:
            # 1. usage — безкоштовно, не рахується у квоту
            try:
                info = await tm.usage()
            except TelemetrioAccessError as e:
                raise CommandError(f"Ключ не приймається: {e}")
            if o["json"]:
                self.stdout.write(json.dumps(info, ensure_ascii=False, indent=2))
            else:
                self.stdout.write(self.style.MIGRATE_HEADING("Квоти Telemetr.io"))
                self.stdout.write(f"  статус              : {info.get('status')}")
                self.stdout.write(f"  requests            : {_fmt(info.get('requests'))}")
                self.stdout.write(f"  unique channels     : {_fmt(info.get('channels'))}")
                self.stdout.write(f"  search requests     : {_fmt(info.get('search_messages_requests'))}")
                self.stdout.write(f"  UNIQUE SEARCH TERMS : {_fmt(info.get('search_terms'))}  <-- дефіцитний ресурс")
                self.stdout.write(f"  період білінгу      : {info.get('billing_start_date')} .. {info.get('billing_end_date')}")
            self.stdout.write(f"\nЛокальний реєстр термінів ({ledger.path}): "
                              f"{ledger.terms or '— порожній —'}")

            # 2. канали (плоскі запити, унікальні канали НЕ витрачаються)
            for term in o["channel"]:
                try:
                    found = await tm.search_channels(term, limit=5)
                except TelemetrioError as e:
                    self.stderr.write(self.style.ERROR(f"channels/search {term!r}: {e}"))
                    continue
                self.stdout.write(self.style.MIGRATE_HEADING(f"\nchannels/search {term!r} → {len(found)}"))
                for c in found[:5]:
                    self.stdout.write(
                        f"  {c.get('internal_id'):>10}  {c.get('peer','?'):7} "
                        f"verified={str(c.get('verified')):5} "
                        f"{c.get('members_count'):>9} підп.  "
                        f"{(c.get('country') or '-'):10} {c.get('title','')[:50]}")

            # 3. tg id -> internal id
            tg_ids = [x.strip() for x in (o["tg_ids"] or "").split(",") if x.strip()]
            if tg_ids:
                self.stdout.write(self.style.MIGRATE_HEADING("\nresolve_telegram_id"))
            for raw in tg_ids:
                r = await tm.resolve_telegram_id(int(raw))
                self.stdout.write(f"  {raw} → {r}")

            # 4. опційний і витратний пробний пошук
            term = (o["search_term"] or "").strip()
            if term:
                new = not ledger.is_spent(term)
                if new and not o["yes"]:
                    raise CommandError(
                        f"Термін {term!r} НОВИЙ — запит спалить 1 з "
                        f"{_fmt(info.get('search_terms'))} унікальних термінів НАЗАВЖДИ. "
                        f"Повтори з --yes, якщо це справді той термін, який треба.")
                try:
                    msgs, chats, count, cursor = await tm.search_messages(
                        term, period="7d", return_short_info=True)
                except TelemetrioAccessError as e:
                    raise CommandError(
                        "/v1/search/messages недоступний для цього ключа (Alpha preview "
                        f"вимкнено за замовчуванням — треба писати @telemetrio_support).\n{e}")
                if new:
                    ledger.record(term)
                self.stdout.write(self.style.SUCCESS(
                    f"\nsearch_messages({term!r}, 7d): всього {count}, на сторінці "
                    f"{len(msgs)}, каналів {len(chats)}, cursor={'є' if cursor else 'нема'}"))
                for m in msgs[:3]:
                    ch = next((c for c in chats if c.get("internal_id") == m.get("peer_id")), {})
                    self.stdout.write(
                        f"  [{m.get('date')}] {ch.get('title','?')[:30]:30} "
                        f"verified={ch.get('verified')} views={m.get('views')} :: "
                        f"{(m.get('text') or '')[:120]!r}")

            # 5. ЩО САМЕ це коштувало. Документація каже, яка квота витрачається,
            # лише для частини ендпоінтів (info-batch), тож міряємо фактом:
            # usage/info безкоштовний, отже дельта «до/після» — точна ціна.
            after = await tm.usage()
            self.stdout.write(self.style.MIGRATE_HEADING("\nФактична ціна прогону (дельта квот)"))
            for k, label in (("requests", "requests"), ("channels", "unique channels"),
                             ("search_messages_requests", "search requests"),
                             ("search_terms", "unique search terms")):
                b = (info.get(k) or {}).get("spent")
                a = (after.get(k) or {}).get("spent")
                if isinstance(a, int) and isinstance(b, int):
                    mark = "  <-- витрачено" if a > b else ""
                    self.stdout.write(f"  {label:22}: {b} -> {a}  (+{a - b}){mark}")
            self.stdout.write(f"  HTTP-запитів клієнтом : {tm.requests_made}")
