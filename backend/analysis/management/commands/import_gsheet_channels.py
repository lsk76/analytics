"""
Import / merge the regional channel list from the public Google Sheet
(https://docs.google.com/spreadsheets/d/1qnhnIa_1ziZjRICiP4RHBi0OJlF28uyf_3LDPnIMbOA/, first sheet)
into our `analysis.Channel` table.

Match strategy: by Telegram `username` (case-insensitive). For existing rows we
fill *empty* fields only — we never overwrite a non-empty value.

The Channel model has no FK to Region; we mirror the canonical Region name into
`Channel.inferred_region` (text). Resolution path:
  1) direct case-insensitive match against `Region.name`,
  2) `RegionAlias.raw` (NFKC-lowered),
  3) a small manual map for cells the sheet uses that have no alias yet
     (city-named columns like "Тамбов", "Брянськ", non-RF rows like "Абхазія").

Usage:
    python manage.py import_gsheet_channels                # dry-run (default)
    python manage.py import_gsheet_channels --apply        # commit
    python manage.py import_gsheet_channels --csv-path ... # custom CSV

Unmatched CSV region labels are appended to /app/_gsheet_unmatched_regions.txt
(or `--unmatched-log` path) so we can decide later whether to extend aliases.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from django.core.management.base import BaseCommand
from django.db import transaction

from analysis.models import Channel, Region, RegionAlias
from analysis.services.normalize import _key as norm_key


# Manual fallback for CSV labels that are not in RegionAlias / Region.name.
# Empty string => intentionally drop (non-RF / aggregate buckets we don't map).
MANUAL_REGION_MAP = {
    # Cities/short forms we don't yet alias
    "Брянськ": "Брянська область",
    "Владимирська обл.": "Володимирська область",
    "Курська обл.": "Курська область",
    "Смоленська обл.": "Смоленська область",
    "Мурманськ": "Мурманська область",
    "Омськ": "Омська область",
    "Тамбов": "Тамбовська область",
    "Красноярськ": "Красноярський край",
    "Саха": "Саха (Якутія)",
    "Сахалін": "Сахалінська область",
    "Камчатка": "Камчатський край",
    "Москва/МО": "Москва",
    "Балтія/Калінінград": "Калінінградська область",
    "Кубань": "Краснодарський край",
    "Новоросійськ": "Краснодарський край",
    "КБР": "Кабардино-Балкарія",
    # Aggregate / non-RF buckets — skip channel-to-region binding entirely
    "Росія (федеральний)": "",
    "Кавказ": "",
    "Кавказ (СКФО)": "",
    "Черкесія/Кавказ": "",
    "Чечня/Кавказ": "",
    "Сибір": "",
    "Північ Росії": "",
    "Середня Азія": "",
    "Монгольські народи": "",
    "Фінно-угорські народи": "",
    "Інгрія": "",
    "Абхазія": "",
    "Вірменія": "",
    "Маріуполь": "",
}

USERNAME_RE = re.compile(r"t\.me/(?:s/)?(?:joinchat/)?([A-Za-z0-9_]+)", re.IGNORECASE)
LANGUAGE_RE = re.compile(r"^\s*([A-Za-zЀ-ӿЁёІіЇїЄєҐґ]+)", re.UNICODE)


def extract_username(link: str, name: str) -> Optional[str]:
    """Pull the @handle out of a t.me/... URL; fall back to the Name cell if it is @handle."""
    if link:
        m = USERNAME_RE.search(link.strip())
        if m:
            return m.group(1).lower()
    if name and name.startswith("@"):
        return name[1:].strip().split()[0].lower()
    return None


def parse_subscribers(raw: str) -> Optional[int]:
    """The 'Підписники' column is mostly an int, but also holds 'приватний/видалений', '—', etc."""
    if not raw:
        return None
    s = raw.strip().replace(" ", "").replace(" ", "").replace(",", "")
    if not s:
        return None
    try:
        n = int(s)
        if n < 0:
            return None
        return n
    except ValueError:
        return None


class Command(BaseCommand):
    help = "Merge the Google-Sheet regional channel list into analysis.Channel."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv-path",
            default="/app/_gsheet_channels_raw.csv",
            help="Path to the CSV downloaded from the Google Sheet "
                 "(default: /app/_gsheet_channels_raw.csv inside the container).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist changes. Without this flag the command is a dry run.",
        )
        parser.add_argument(
            "--unmatched-log",
            default="/app/_gsheet_unmatched_regions.txt",
            help="Where to write the list of CSV region labels we could not map.",
        )

    # ------------------------------------------------------------------ helpers

    def _resolve_region(self, raw_region: str, region_cache: dict) -> tuple[Optional[Region], Optional[str]]:
        """
        Map a CSV region label to (Region, status) where status is one of:
          'matched'   — Region resolved
          'skipped'   — explicitly mapped to '' (non-RF / aggregate); do not bind
          'unmatched' — label is not known; log and leave Channel.inferred_region empty
        """
        if not raw_region:
            return None, "skipped"
        if raw_region in region_cache:
            return region_cache[raw_region]

        # 1) direct
        r = Region.objects.filter(name__iexact=raw_region).first()
        if r:
            region_cache[raw_region] = (r, "matched")
            return region_cache[raw_region]

        # 2) RegionAlias
        a = RegionAlias.objects.filter(raw=norm_key(raw_region)).select_related("region").first()
        if a and a.region:
            region_cache[raw_region] = (a.region, "matched")
            return region_cache[raw_region]

        # 3) manual fallback
        if raw_region in MANUAL_REGION_MAP:
            target = MANUAL_REGION_MAP[raw_region]
            if not target:
                region_cache[raw_region] = (None, "skipped")
                return region_cache[raw_region]
            r = Region.objects.filter(name__iexact=target).first()
            if r:
                region_cache[raw_region] = (r, "matched")
                return region_cache[raw_region]

        region_cache[raw_region] = (None, "unmatched")
        return region_cache[raw_region]

    # ------------------------------------------------------------------ main

    def handle(self, *args, **opts):
        csv_path = Path(opts["csv_path"])
        if not csv_path.exists():
            self.stderr.write(f"CSV not found: {csv_path}")
            sys.exit(2)

        apply = bool(opts["apply"])
        self.stdout.write(
            self.style.WARNING(f"Mode: {'APPLY' if apply else 'DRY-RUN'}  CSV: {csv_path}")
        )

        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        # Pre-aggregate per username (the same channel can appear in 2-3 regions —
        # we take the first non-empty value per field, keep the first matched region).
        by_user: dict[str, dict] = {}
        skipped_no_username = 0
        for r in rows:
            link = (r.get("Посилання") or "").strip()
            name = (r.get("Назва") or "").strip()
            username = extract_username(link, name)
            if not username:
                skipped_no_username += 1
                continue
            agg = by_user.setdefault(username, {
                "username": username,
                "title_candidates": [],
                "subs_candidates": [],
                "lang_candidates": [],
                "desc_candidates": [],
                "regions": [],
                "tgstat_candidates": [],
            })
            if name and not name.startswith("@"):
                agg["title_candidates"].append(name)
            subs = parse_subscribers(r.get("Підписники", ""))
            if subs is not None:
                agg["subs_candidates"].append(subs)
            lang = (r.get("Мова") or "").strip()
            if lang:
                m = LANGUAGE_RE.match(lang)
                if m:
                    agg["lang_candidates"].append(m.group(1).strip()[:16])
            d_tg = (r.get("Опис (Telegram)") or "").strip()
            d_ts = (r.get("Опис (TGStat)") or "").strip()
            if d_tg:
                agg["desc_candidates"].append(d_tg)
            elif d_ts:
                agg["desc_candidates"].append(d_ts)
            reg = (r.get("Регіон") or "").strip()
            if reg and reg not in agg["regions"]:
                agg["regions"].append(reg)
            tgstat = (r.get("TGStat") or "").strip()
            if tgstat:
                agg["tgstat_candidates"].append(tgstat)

        # Reporting counters
        stat = Counter()
        unmatched_regions: Counter = Counter()
        updates_per_field: Counter = Counter()
        region_cache: dict = {}
        sample_creates: list[str] = []
        sample_updates: list[str] = []

        # We do everything in one transaction so that --apply is atomic AND
        # so that --dry-run rolls back cleanly. atomic() opens a real transaction
        # (Django by default runs in autocommit mode; bare savepoint() would be a no-op).
        with transaction.atomic():
            sid = transaction.savepoint()
            for username, agg in by_user.items():
                stat["sheet_total"] += 1

                # Pick best Region across all rows for this channel.
                region_name: str = ""
                for reg_label in agg["regions"]:
                    r_obj, status = self._resolve_region(reg_label, region_cache)
                    if status == "matched":
                        region_name = r_obj.name
                        break
                    if status == "unmatched":
                        unmatched_regions[reg_label] += 1

                title = agg["title_candidates"][0] if agg["title_candidates"] else ""
                subs = max(agg["subs_candidates"]) if agg["subs_candidates"] else None
                lang = agg["lang_candidates"][0] if agg["lang_candidates"] else ""
                desc = agg["desc_candidates"][0] if agg["desc_candidates"] else ""

                ch = Channel.objects.filter(username__iexact=username).first()
                if ch is None:
                    stat["created"] += 1
                    raw_meta = {"source": "gsheet_2026_06"}
                    if agg["tgstat_candidates"]:
                        raw_meta["tgstat_url"] = agg["tgstat_candidates"][0]
                    new = Channel(
                        username=username,
                        title=title,
                        description=desc,
                        subscribers=subs or 0,
                        language=lang,
                        inferred_region=region_name,
                        raw_meta=raw_meta,
                    )
                    new.save()
                    if len(sample_creates) < 8:
                        sample_creates.append(
                            f"  + @{username}  title={title[:40]!r}  subs={subs}  region={region_name!r}"
                        )
                else:
                    stat["matched"] += 1
                    changes = []
                    if not ch.title and title:
                        ch.title = title
                        changes.append("title")
                    if (not ch.subscribers or ch.subscribers == 0) and subs:
                        ch.subscribers = subs
                        changes.append("subscribers")
                    if not ch.language and lang:
                        ch.language = lang
                        changes.append("language")
                    if not ch.description and desc:
                        ch.description = desc
                        changes.append("description")
                    if not ch.inferred_region and region_name:
                        ch.inferred_region = region_name
                        changes.append("inferred_region")
                    # raw_meta: tag the source if not yet present
                    if agg["tgstat_candidates"] and not ch.raw_meta.get("tgstat_url"):
                        ch.raw_meta = {**(ch.raw_meta or {}), "tgstat_url": agg["tgstat_candidates"][0],
                                       "gsheet_2026_06": True}
                        changes.append("raw_meta")
                    if changes:
                        ch.save(update_fields=list(dict.fromkeys(changes)))
                        stat["updated"] += 1
                        for c in changes:
                            updates_per_field[c] += 1
                        if len(sample_updates) < 8:
                            sample_updates.append(
                                f"  ~ @{username} (id={ch.id}) fields={changes} region<-{region_name!r}"
                            )

            if not apply:
                transaction.savepoint_rollback(sid)
            else:
                transaction.savepoint_commit(sid)

        # ---------------------------------------------------------------- report
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("=== Summary ==="))
        self.stdout.write(f"  CSV rows                   : {len(rows)}")
        self.stdout.write(f"  Unique usernames in sheet  : {len(by_user)}")
        self.stdout.write(f"  Rows skipped (no username) : {skipped_no_username}")
        self.stdout.write(f"  Matched existing channels  : {stat['matched']}")
        self.stdout.write(f"    of which updated fields  : {stat['updated']}")
        self.stdout.write(f"  Created new channels       : {stat['created']}")
        self.stdout.write(f"  Region misses (unique)     : {len(unmatched_regions)}")

        if updates_per_field:
            self.stdout.write("\n  Field updates breakdown:")
            for fld, n in updates_per_field.most_common():
                self.stdout.write(f"    {fld:18}: {n}")

        if sample_creates:
            self.stdout.write("\n  Sample creates:")
            for s in sample_creates:
                self.stdout.write(s)
        if sample_updates:
            self.stdout.write("\n  Sample updates:")
            for s in sample_updates:
                self.stdout.write(s)

        if unmatched_regions:
            unmatched_path = Path(opts["unmatched_log"])
            unmatched_path.write_text(
                "\n".join(f"{cnt}\t{label}" for label, cnt in unmatched_regions.most_common()),
                encoding="utf-8",
            )
            self.stdout.write(f"\n  Unmatched region labels written to: {unmatched_path}")
            for label, cnt in unmatched_regions.most_common(15):
                self.stdout.write(f"    {cnt:>4}  {label!r}")

        self.stdout.write("")
        if apply:
            self.stdout.write(self.style.SUCCESS("APPLIED. Changes committed."))
        else:
            self.stdout.write(self.style.WARNING("DRY-RUN. No changes were persisted."))
