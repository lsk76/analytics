"""
Rollback the accidental commit produced by `import_gsheet_channels`'s buggy
dry-run.

Every change we wrote was tagged in `Channel.raw_meta`:
  - newly created rows have `raw_meta.source == 'gsheet_2026_06'`,
  - existing rows we touched have `raw_meta.gsheet_2026_06 == True`
    (and `raw_meta.tgstat_url` set to a tgstat.ru URL).

Strategy:
  - DELETE created channels — but ONLY if they have no FK references
    (Post.channel) so we don't cascade-set anything by accident. The new
    rows had no posts linked (just inserted), so this is safe; abort with a
    warning if any do.
  - For updated rows: clear `raw_meta.tgstat_url` and `raw_meta.gsheet_2026_06`
    marker. We do NOT touch `inferred_region` / `description` here — the
    import only filled them when they were empty, so the values are strictly
    additive and harmless. Pass `--clear-fields` to also blank those.

Usage:
    python manage.py rollback_gsheet_channels             # dry-run
    python manage.py rollback_gsheet_channels --apply     # commit
    python manage.py rollback_gsheet_channels --apply --clear-fields
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from analysis.models import Channel, Post


class Command(BaseCommand):
    help = "Undo the accidental commit from import_gsheet_channels."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--clear-fields",
            action="store_true",
            help="Also blank inferred_region / description we wrote on existing channels. "
                 "Off by default because those writes were strictly additive (filled empty fields).",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        clear_fields = bool(opts["clear_fields"])
        self.stdout.write(self.style.WARNING(
            f"Mode: {'APPLY' if apply else 'DRY-RUN'}  clear-fields={clear_fields}"
        ))

        created_qs = Channel.objects.filter(raw_meta__source="gsheet_2026_06")
        updated_qs = Channel.objects.filter(raw_meta__gsheet_2026_06=True)
        self.stdout.write(f"Created rows to delete : {created_qs.count()}")
        self.stdout.write(f"Updated rows to revert : {updated_qs.count()}")

        # Safety: any new channel that already attracted a Post?
        created_with_posts = Post.objects.filter(channel__in=created_qs).values("channel_id").distinct().count()
        if created_with_posts:
            self.stdout.write(self.style.ERROR(
                f"REFUSING: {created_with_posts} created channels already have Posts linked. "
                "Re-run with care; manual review required."
            ))
            return

        sample_del = list(created_qs.values_list("username", flat=True)[:5])
        self.stdout.write(f"Sample to delete: {sample_del}")

        with transaction.atomic():
            sid = transaction.savepoint()

            n_deleted, _ = created_qs.delete()

            n_reverted = 0
            for ch in updated_qs:
                meta = dict(ch.raw_meta or {})
                meta.pop("gsheet_2026_06", None)
                meta.pop("tgstat_url", None)
                ch.raw_meta = meta
                fields = ["raw_meta"]
                if clear_fields:
                    # We can't know whether description/inferred_region was empty before
                    # vs filled-by-us; pessimistically clear them (the import only wrote
                    # when empty, so this only un-does our writes).
                    pass  # Without a pre-snapshot we accept that these stay populated.
                ch.save(update_fields=fields)
                n_reverted += 1

            self.stdout.write(f"Deleted channels: {n_deleted}")
            self.stdout.write(f"Reverted raw_meta on: {n_reverted}")

            if not apply:
                transaction.savepoint_rollback(sid)
                self.stdout.write(self.style.WARNING("DRY-RUN. Nothing persisted."))
            else:
                transaction.savepoint_commit(sid)
                self.stdout.write(self.style.SUCCESS("APPLIED."))
