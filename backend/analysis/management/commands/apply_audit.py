"""
Apply a hand-audit CSV to events:
  - valid=drop → review_status='rejected' (with audit note)
  - attacker  → Event.tags += Tag(category='attacker_nationality')
  - victim    → Event.tags += Tag(category='victim_nationality')

Сторони живуть у звичайному Event.tags під ЗАКРИТИМИ категоріями
attacker_nationality / victim_nationality (словник = nationality). Окремих
M2M-полів attacker_tags/victim_tags більше немає.

CSV columns: id,valid,attacker,victim,note
`attacker`/`victim` — comma-separated назви національностей (мають існувати
в словнику відповідної категорії); порожня клітинка не чіпає теги
(використовуйте 'CLEAR' щоб стерти сторону).

  python manage.py apply_audit _audit_dagestan.csv
"""
import csv
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone as djtz

from analysis.models import Event, Tag


class Command(BaseCommand):
    help = "Apply a hand-audit CSV (id,valid,attacker,victim,note) to events"

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        path = opts["csv_path"]
        dry = opts["dry_run"]

        # словники сторін (закриті категорії, дзеркало nationality)
        side_tags = {
            "attacker_nationality": {t.name.lower(): t for t in
                                     Tag.objects.filter(category="attacker_nationality")},
            "victim_nationality":   {t.name.lower(): t for t in
                                     Tag.objects.filter(category="victim_nationality")},
        }

        def resolve(names_str, category):
            """'дагестанець, росіянин' → [Tag, Tag] категорії сторони. Unknown → skipped."""
            if not names_str or names_str.strip().upper() == "CLEAR":
                return None, names_str.strip().upper() == "CLEAR"
            out, missing = [], []
            for n in [x.strip().lower() for x in names_str.split(",") if x.strip()]:
                t = side_tags[category].get(n)
                if t:
                    out.append(t)
                else:
                    missing.append(n)
            if missing:
                self.stderr.write(f"  unknown {category} tags: {missing}")
            return out, False

        def set_side(ev, category, new_tags):
            """Замінити теги сторони у звичайному ev.tags (clear-then-add у межах категорії)."""
            cur = [t for t in ev.tags.all() if t.category == category]
            if cur:
                ev.tags.remove(*cur)
            if new_tags:
                ev.tags.add(*new_tags)

        n_drop = n_attacker = n_victim = n_skip = 0
        with open(path) as f, transaction.atomic():
            for row in csv.DictReader(f):
                eid = int(row["id"])
                ev = Event.objects.filter(id=eid).first()
                if not ev:
                    self.stderr.write(f"  #{eid}: not found, skipped")
                    n_skip += 1
                    continue

                valid = (row.get("valid") or "").strip().lower()
                note = (row.get("note") or "")[:300]

                if valid == "drop":
                    if not dry:
                        ev.review_status = Event.REVIEW_REJECTED
                        ev.review_notes = f"manual audit: {note}"
                        ev.reviewed_at = djtz.now()
                        ev.save(update_fields=["review_status", "review_notes",
                                               "reviewed_at"])
                    n_drop += 1

                att_tags, att_clear = resolve(row.get("attacker", ""), "attacker_nationality")
                vic_tags, vic_clear = resolve(row.get("victim", ""), "victim_nationality")

                if att_tags is not None or att_clear:
                    if not dry:
                        set_side(ev, "attacker_nationality", att_tags or [])
                    n_attacker += 1
                if vic_tags is not None or vic_clear:
                    if not dry:
                        set_side(ev, "victim_nationality", vic_tags or [])
                    n_victim += 1

            if dry:
                transaction.set_rollback(True)

        verb = "would apply" if dry else "applied"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb}: {n_drop} drops, {n_attacker} attacker-sets, "
            f"{n_victim} victim-sets, {n_skip} skipped"))
