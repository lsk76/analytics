"""
Backfill region_subject + settlement for existing Events from their raw region text.

    python manage.py backfill_regions [--run <id>]
"""
from django.core.management.base import BaseCommand

from analysis.models import Event, ResearchRun
from analysis.services.normalize import resolve_region


class Command(BaseCommand):
    help = "Resolve raw Event.region -> RF subject + settlement"

    def add_arguments(self, parser):
        parser.add_argument("--run", type=int, default=None)

    def handle(self, *args, **opts):
        qs = Event.objects.all()
        if opts["run"]:
            qs = qs.filter(run_id=opts["run"])

        cache = {}  # raw -> (region, settlement) to avoid repeat LLM calls
        n = 0
        for ev in qs:
            raw = (ev.region or "").strip()
            if not raw:
                continue
            if raw not in cache:
                cache[raw] = resolve_region(raw)
            region, settlement = cache[raw]
            # the raw region is usually just the subject (no city) -> dig the
            # settlement out of the summary, then the source post text (the village
            # name often lives only in the original text, e.g. "п. Винзили")
            if not settlement:
                head = ev.posts.order_by("posted_at").first()
                for src in (ev.summary, getattr(head, "text", None)):
                    if not src or not src.strip():
                        continue
                    s_region, s_settlement = resolve_region(src.strip()[:600])
                    if s_settlement:
                        settlement = s_settlement
                    if region is None and s_region is not None:
                        region = s_region
                    if settlement:
                        break
            ev.region_subject = region
            ev.settlement = settlement
            ev.save(update_fields=["region_subject", "settlement"])
            n += 1
        self.stdout.write(self.style.SUCCESS(f"Оброблено подій: {n}, унікальних регіонів: {len(cache)}"))

        # refresh run stats if a single run was targeted
        if opts["run"]:
            from analysis.services import pipeline
            run = ResearchRun.objects.get(id=opts["run"])
            pipeline.aggregate(run)
            run.refresh_from_db()
            self.stdout.write("Топ суб'єктів: " + str(list(run.stats.get("by_region", {}).items())[:12]))
