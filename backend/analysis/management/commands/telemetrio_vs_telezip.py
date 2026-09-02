"""
Head-to-head: Telemetr.io /v1/search/messages vs TeleZip /Find, same term, same
window — does Telemetr.io find what TeleZip finds on OUR topics?

METHOD (why it is built this way)
  * ONE BARE TERM PER RUN, sent verbatim to both APIs. TeleZip's `+`/`-`/`~N`
    operators have no Telemetr.io equivalent, so any query using them would
    compare our query language against nothing. A bare term is the only fair
    unit, and it is also what the free plan can afford.
  * JOIN ON NORMALISED TEXT, not on ids. The two services live in different id
    spaces (TeleZip = Telegram numeric id; Telemetr.io = opaque internal_id) and
    translating between them costs the 5-per-month unique-channels quota. Message
    text is present on both sides and is free, so overlap is computed over
    sha1(normalised text prefix). Reposts collapse into one key — which is what
    we want, since TeleZip is queried with unique=true.
  * WINDOW <= 7 DAYS. The free Telemetr.io plan cannot see further back, so a
    longer window would compare Telemetr.io's blindness against TeleZip's index
    and prove nothing.
  * EXCLUSIVE HITS ARE DUMPED, NOT COUNTED. Recall numbers alone cannot say
    whether the misses matter; the JSON/`--out` dump exists so a human can read
    what each side missed and judge relevance.

QUOTA SAFETY
  A new `term` permanently consumes one of five monthly slots. The command
  refuses to spend more than `--allow-new-terms` (default 1) and prints the
  before/after quota. Terms already in the local ledger are free to repeat.

  # 1. plan only, spends nothing
  python manage.py telemetrio_vs_telezip --term "мобилизация" --dry-run
  # 2. real run
  python manage.py telemetrio_vs_telezip --term "мобилизация" --days 7 \
      --country russia --out /app/backend/_dev/tm_vs_tz
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from analysis.services.telemetrio import (
    TelemetrioAccessError, TelemetrioClient, TelemetrioError, TermLedger,
    default_ledger_path,
)
from analysis.services.telezip import TelezipClient

_URL_RE = re.compile(r"https?://\S+|t\.me/\S+")
_NONWORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def norm_text(t: str) -> str:
    """Fold the cosmetic differences between the two services' renderings of the
    same post: links (one side keeps markup entities separate), punctuation,
    case, whitespace. Deliberately lossy — this is a join key, not content."""
    t = _URL_RE.sub(" ", t or "")
    t = _NONWORD_RE.sub(" ", t.lower())
    return _WS_RE.sub(" ", t).strip()


def text_key(t: str) -> str | None:
    """sha1 of the first 300 normalised chars. Short/empty posts (media-only,
    stickers) get None — they cannot be joined reliably and are reported apart
    rather than silently counted as misses."""
    n = norm_text(t)
    if len(n) < 25:
        return None
    return hashlib.sha1(n[:300].encode("utf-8")).hexdigest()


class Command(BaseCommand):
    help = "Порівняти пошук Telemetr.io і TeleZip на одному терміні та вікні."

    def add_arguments(self, parser):
        parser.add_argument("--term", action="append", required=True,
                            help="Пошуковий термін (як є, без +/-). Можна кілька разів — "
                                 "але кожен НОВИЙ термін незворотно з'їдає слот місячної "
                                 "квоти search_terms (фактичний ліміт друкує --dry-run).")
        parser.add_argument("--days", type=int, default=7,
                            help="Глибина вікна в днях (free-план Telemetr.io бачить 7).")
        parser.add_argument("--country", default="",
                            help="Фільтр країни каналу для Telemetr.io (напр. russia). "
                                 "У TeleZip аналога немає — див. --languages.")
        parser.add_argument("--languages", default="ru",
                            help="Мовний фільтр TeleZip, comma-separated. Порожньо = без фільтра.")
        parser.add_argument("--max-pages", type=int, default=5,
                            help="Стеля сторінок Telemetr.io на термін (кожна = 1 запит).")
        parser.add_argument("--allow-new-terms", type=int, default=1,
                            help="Скільки НОВИХ (ще не оплачених) термінів дозволено спалити.")
        parser.add_argument("--skip-telezip", action="store_true")
        parser.add_argument("--out", default="",
                            help="Каталог для JSON-дампа результатів і ексклюзивних знахідок.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Показати план і ціну, нічого не запитувати.")

    def handle(self, *a, **o):
        if not settings.TELEMETRIO_API_KEY:
            raise CommandError("TELEMETRIO_API_KEY порожній — додай у .env.")
        if o["days"] > 7:
            self.stderr.write(self.style.WARNING(
                f"--days={o['days']}: free-ключ Telemetr.io бачить лише 7 днів — "
                f"порівняння за межами тижня буде нечесним до Telemetr.io."))
        asyncio.run(self._run(o))

    # ------------------------------------------------------------------
    async def _run(self, o: dict):
        terms = [t.strip() for t in o["term"] if t.strip()]
        ledger = TermLedger(default_ledger_path())
        new_terms = [t for t in terms if not ledger.is_spent(t)]

        date_to = datetime.now(timezone.utc)
        date_from = date_to - timedelta(days=o["days"])

        self.stdout.write(self.style.MIGRATE_HEADING("План"))
        self.stdout.write(f"  вікно      : {date_from:%Y-%m-%d %H:%M} .. {date_to:%Y-%m-%d %H:%M} UTC")
        self.stdout.write(f"  терміни    : {terms}")
        self.stdout.write(f"  з них НОВІ : {new_terms or '—'}  (кожен незворотно з'їдає слот квоти)")
        self.stdout.write(f"  ціна       : до {len(terms) * o['max_pages']} запитів Telemetr.io "
                          f"+ {len(new_terms)} унікальних термінів")

        if len(new_terms) > o["allow_new_terms"]:
            raise CommandError(
                f"Нових термінів {len(new_terms)} > --allow-new-terms={o['allow_new_terms']}. "
                f"Це незворотна витрата. Або зменш список, або підніми ліміт свідомо.")
        if o["dry_run"]:
            async with TelemetrioClient(settings.TELEMETRIO_API_KEY,
                                        settings.TELEMETRIO_BASE_URL) as tm:
                self._quota(await tm.usage(), "зараз")   # usage/info безкоштовний
            self.stdout.write(self.style.SUCCESS("dry-run — нічого не витрачено."))
            return

        async with TelemetrioClient(settings.TELEMETRIO_API_KEY,
                                    settings.TELEMETRIO_BASE_URL) as tm:
            before = await tm.usage()
            self._quota(before, "ДО прогону")

            report = []
            for term in terms:
                report.append(await self._one_term(tm, ledger, term, date_from, date_to, o))

            after = await tm.usage()
            self._quota(after, "ПІСЛЯ прогону")
            self.stdout.write(f"  запитів Telemetr.io цим прогоном: {tm.requests_made}")

        if o["out"]:
            out = Path(o["out"])
            out.mkdir(parents=True, exist_ok=True)
            stamp = date_to.strftime("%Y%m%d-%H%M")
            path = out / f"tm-vs-tz-{stamp}.json"
            path.write_text(json.dumps(
                {"window": [date_from.isoformat(), date_to.isoformat()],
                 "quota_before": before, "quota_after": after, "terms": report},
                ensure_ascii=False, indent=1), "utf-8")
            self.stdout.write(self.style.SUCCESS(f"\nДамп: {path}"))

    # ------------------------------------------------------------------
    async def _one_term(self, tm, ledger, term, date_from, date_to, o) -> dict:
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {term!r} ==="))

        # --- Telemetr.io -------------------------------------------------
        t0 = time.monotonic()
        try:
            tm_msgs, tm_chats, tm_total = await tm.search_messages_all(
                term, max_pages=o["max_pages"], date_from=date_from, date_to=date_to,
                country=o["country"] or None, return_short_info=True)
        except TelemetrioAccessError as e:
            raise CommandError(
                "/v1/search/messages закритий для цього ключа — це Alpha preview, "
                f"вмикається лише через @telemetrio_support.\n{e}")
        tm_secs = time.monotonic() - t0
        if not ledger.is_spent(term):
            ledger.record(term)
        tm_parsed = [TelemetrioClient.parse_message(m, tm_chats) for m in tm_msgs]

        # --- TeleZip ------------------------------------------------------
        tz_parsed, tz_secs = [], 0.0
        if not o["skip_telezip"]:
            langs = [x.strip() for x in (o["languages"] or "").split(",") if x.strip()]
            t0 = time.monotonic()
            async with TelezipClient(settings.TELEZIP_API_KEY, settings.TELEZIP_BASE_URL) as tz:
                tz_parsed = await tz.find_posts_range(
                    term, date_from, date_to, languages=langs or None, unique=True)
            tz_secs = time.monotonic() - t0

        # --- join on normalised text -------------------------------------
        tm_by_key, tm_nokey = {}, 0
        for m in tm_parsed:
            k = text_key(m["content"])
            if k is None:
                tm_nokey += 1
            else:
                tm_by_key.setdefault(k, m)
        tz_by_key, tz_nokey = {}, 0
        for m in tz_parsed:
            k = text_key(m.get("content") or "")
            if k is None:
                tz_nokey += 1
            else:
                tz_by_key.setdefault(k, m)

        both = set(tm_by_key) & set(tz_by_key)
        only_tm = set(tm_by_key) - set(tz_by_key)
        only_tz = set(tz_by_key) - set(tm_by_key)

        tm_channels = {m["channel_id"] for m in tm_parsed if m["channel_id"]}
        tz_channels = {m.get("channel_id") for m in tz_parsed if m.get("channel_id")}
        verified = sum(1 for c in tm_chats.values() if c.get("verified"))

        self.stdout.write(
            f"  Telemetr.io : {len(tm_parsed)} завантажено з {tm_total} заявлених, "
            f"{len(tm_channels)} каналів ({verified} verified), {tm_secs:.1f}s")
        self.stdout.write(
            f"  TeleZip     : {len(tz_parsed)} знайдено, "
            f"{len(tz_channels)} каналів, {tz_secs:.1f}s")
        self.stdout.write(
            f"  перетин     : {len(both)}   лише Telemetr.io: {len(only_tm)}   "
            f"лише TeleZip: {len(only_tz)}")
        if tz_by_key:
            self.stdout.write(
                f"  ПОКРИТТЯ TeleZip-знахідок з боку Telemetr.io: "
                f"{100 * len(both) / len(tz_by_key):.1f}%")
        if tm_nokey or tz_nokey:
            self.stdout.write(f"  (без тексту/закороткі, не зіставлялись: "
                              f"Telemetr.io {tm_nokey}, TeleZip {tz_nokey})")

        for label, keys, src in (("лише TeleZip", only_tz, tz_by_key),
                                 ("лише Telemetr.io", only_tm, tm_by_key)):
            sample = [src[k] for k in list(keys)[:3]]
            if sample:
                self.stdout.write(f"  приклади «{label}»:")
                for m in sample:
                    self.stdout.write(f"    [{m.get('date')}] {str(m.get('channel_name'))[:28]:28} "
                                      f":: {(m.get('content') or '')[:110]!r}")

        return {
            "term": term,
            "telemetrio": {"fetched": len(tm_parsed), "reported_total": tm_total,
                           "channels": len(tm_channels), "verified_channels": verified,
                           "seconds": round(tm_secs, 2), "unjoinable": tm_nokey,
                           "messages": tm_parsed},
            "telezip": {"fetched": len(tz_parsed), "channels": len(tz_channels),
                        "seconds": round(tz_secs, 2), "unjoinable": tz_nokey,
                        "messages": tz_parsed},
            "overlap": {"both": len(both), "only_telemetrio": len(only_tm),
                        "only_telezip": len(only_tz),
                        "telezip_recall_by_telemetrio":
                            round(len(both) / len(tz_by_key), 4) if tz_by_key else None},
            "only_telezip_sample": [tz_by_key[k] for k in list(only_tz)[:40]],
            "only_telemetrio_sample": [tm_by_key[k] for k in list(only_tm)[:40]],
        }

    def _quota(self, info: dict, when: str) -> None:
        def f(c):
            return f"{c.get('spent')}/{c.get('limit')}" if c else "n/a"
        self.stdout.write(
            f"  квота {when}: requests {f(info.get('requests'))}, "
            f"search {f(info.get('search_messages_requests'))}, "
            f"TERMS {f(info.get('search_terms'))}, "
            f"channels {f(info.get('channels'))}")
