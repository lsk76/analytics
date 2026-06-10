"""
Apply a hand-audit CSV to events:
  - valid=drop → review_status='rejected' (with audit note)
  - attacker  → Event.attacker_tags = matching Tag rows (nationality category)
  - victim    → Event.victim_tags  = matching Tag rows

CSV columns: id,valid,attacker,victim,note
`attacker` and `victim` are comma-separated nationality names (must already
exist as Tag rows with category='nationality'); empty cells leave the M2M
untouched (use 'CLEAR' to wipe).

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

        # Cache nationality tags by lowercase name
        nat_tags = {t.name.lower(): t for t in
                    Tag.objects.filter(category="nationality")}

        def resolve(names_str):
            """'дагестанець, росіянин' → [Tag, Tag]. Unknown names → skipped + warned."""
            if not names_str or names_str.strip().upper() == "CLEAR":
                return None, names_str.strip().upper() == "CLEAR"
            out, missing = [], []
            for n in [x.strip().lower() for x in names_str.split(",") if x.strip()]:
                t = nat_tags.get(n)
                if t:
                    out.append(t)
                else:
                    missing.append(n)
            if missing:
                self.stderr.write(f"  unknown nationality tags: {missing}")
            return out, False

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

                att_tags, att_clear = resolve(row.get("attacker", ""))
                vic_tags, vic_clear = resolve(row.get("victim", ""))

                if att_tags is not None or att_clear:
                    if not dry:
                        ev.attacker_tags.set(att_tags or [])
                    n_attacker += 1
                if vic_tags is not None or vic_clear:
                    if not dry:
                        ev.victim_tags.set(vic_tags or [])
                    n_victim += 1

            if dry:
                transaction.set_rollback(True)

        verb = "would apply" if dry else "applied"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb}: {n_drop} drops, {n_attacker} attacker-sets, "
            f"{n_victim} victim-sets, {n_skip} skipped"))
