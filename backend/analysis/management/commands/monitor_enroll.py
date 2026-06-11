"""
Enroll Telegram chats into an opinion-monitoring AnalysisTask.

For each --username provided:
  1. Try to find a Channel in DB by username (case-insensitive).
  2. If missing → fetch metadata via TeleZip `/Channels?username=...`,
     create a Channel record.
  3. Create or update the MonitorChat row tying Channel to Task.

Examples:
  python manage.py monitor_enroll \
    --task dagestan-criticism-monitor \
    --usernames gaziinetinebydet,chatkhadulaev,official_atypical_chat,ekhodagestan

  python manage.py monitor_enroll \
    --task dagestan-criticism-monitor \
    --tg-ids 1721536340,1797530467 \
    --critical-source
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from analysis.models import AnalysisTask, Channel, MonitorChat
from analysis.services.telezip import TelezipClient


async def _fetch_channel_by_id(cid: int) -> dict | None:
    async with TelezipClient(settings.TELEZIP_API_KEY,
                             settings.TELEZIP_BASE_URL) as tz:
        return await tz.get_channel(cid)


def _resolve_channel_by_username(username: str) -> Channel | None:
    """Find a Channel in DB by username, case-insensitive."""
    if not username:
        return None
    return Channel.objects.filter(username__iexact=username.lstrip("@")).first()


class Command(BaseCommand):
    help = "Enroll one or more chats into an opinion-monitor AnalysisTask."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True,
                            help="Slug AnalysisTask, у яку записуємо чати.")
        parser.add_argument("--usernames", default="",
                            help="Comma-separated Telegram usernames (без @).")
        parser.add_argument("--tg-ids", default="",
                            help="Comma-separated Telegram channel/chat IDs.")
        parser.add_argument("--critical-source", action="store_true",
                            help="Позначити чати як critical_source=True.")
        parser.add_argument("--priority", type=int, default=100)
        parser.add_argument("--note", default="",
                            help="Нотатка (одна на всю партію).")
        parser.add_argument("--added-by", default="manual",
                            help="Хто додає (для аудиту).")
        parser.add_argument("--deactivate", action="store_true",
                            help="Деактивувати вказані чати (is_active=False) "
                                 "замість додавання.")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=opts["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"AnalysisTask slug={opts['task']!r} not found. "
                               f"Run seed_opinion_tags first.")

        usernames = [u.strip().lstrip("@") for u in opts["usernames"].split(",")
                     if u.strip()]
        tg_ids = [int(x) for x in opts["tg_ids"].split(",") if x.strip()]
        if not usernames and not tg_ids:
            raise CommandError("Pass --usernames and/or --tg-ids.")

        if opts["deactivate"]:
            return self._deactivate(task, usernames, tg_ids)

        n_added = 0
        n_updated = 0
        n_fetched = 0
        n_skipped = 0

        # ---- by username ------------------------------------------------
        for u in usernames:
            ch = _resolve_channel_by_username(u)
            if not ch:
                # Without a TeleZip "get channel by username" endpoint, we
                # can't autocreate. Tell the operator how to add manually.
                self.stdout.write(self.style.WARNING(
                    f"  @{u}: no Channel row, and TeleZip /Channels supports "
                    f"lookup only by numeric id. Add the tg_id via --tg-ids "
                    f"or create a Channel row manually first."
                ))
                n_skipped += 1
                continue
            created = self._enroll(task, ch, opts)
            if created: n_added += 1
            else:       n_updated += 1
            self.stdout.write(f"  @{u} (id={ch.tg_id}) {'+' if created else '·'}")

        # ---- by tg_id (with autofetch) ---------------------------------
        for cid in tg_ids:
            ch = Channel.objects.filter(tg_id=cid).first()
            if not ch:
                # Fetch metadata via TeleZip.
                self.stdout.write(f"  id={cid}: fetching meta from TeleZip ...")
                meta = asyncio.run(_fetch_channel_by_id(cid))
                if not meta:
                    self.stdout.write(self.style.WARNING(
                        f"  id={cid}: TeleZip returned nothing — skip."
                    ))
                    n_skipped += 1
                    continue
                ch = Channel.objects.create(
                    tg_id=meta["tg_id"] or cid,
                    username=meta["username"] or "",
                    title=meta["title"] or "",
                    description=meta["about"] or "",
                    subscribers=meta["subscribers"] or 0,
                    language=meta["language"] or "",
                    is_channel=meta["is_channel"],
                    enriched=True,
                    fetched_at=datetime.now(timezone.utc),
                    raw_meta={"created_by": "monitor_enroll"},
                )
                n_fetched += 1
            created = self._enroll(task, ch, opts)
            if created: n_added += 1
            else:       n_updated += 1
            self.stdout.write(
                f"  id={cid} (@{ch.username or '-'}) {'+' if created else '·'}"
            )

        self.stdout.write(self.style.SUCCESS(
            f"\ntask {task.slug}: +{n_added} added, ·{n_updated} updated, "
            f"⇣{n_fetched} fetched from TeleZip, ?{n_skipped} skipped"
        ))

    @transaction.atomic
    def _enroll(self, task: AnalysisTask, channel: Channel, opts: dict) -> bool:
        obj, created = MonitorChat.objects.update_or_create(
            task=task, channel=channel,
            defaults={
                "is_active": True,
                "is_critical_source": opts["critical_source"],
                "priority": opts["priority"],
                "notes": opts["note"],
                "added_by": opts["added_by"],
            },
        )
        return created

    @transaction.atomic
    def _deactivate(self, task, usernames, tg_ids):
        ids = set(tg_ids)
        for u in usernames:
            ch = _resolve_channel_by_username(u)
            if ch and ch.tg_id:
                ids.add(ch.tg_id)
        n = MonitorChat.objects.filter(
            task=task, channel__tg_id__in=ids
        ).update(is_active=False)
        self.stdout.write(self.style.SUCCESS(f"deactivated {n} enrollments"))
