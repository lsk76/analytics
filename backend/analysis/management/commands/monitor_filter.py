"""
Filter out noise from collected monitor Posts using exclusion regexes
(see analysis/pilot/filters.py).

Outcomes:
  * Post.classification.is_filtered     = True/False
  * Post.classification.exclusion_label = "transport" / "realestate" / ...
  * Post.is_relevant = False if filtered  (so admin filters can pick noise out)

The filter is idempotent: re-run after editing pilot/filters.py to refresh.

  python manage.py monitor_filter --task dagestan-criticism-monitor \
      --region dagestan \
      --min-length 30 --max-length 800
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from analysis.models import AnalysisTask, Post
from analysis.pilot import filters as F


class Command(BaseCommand):
    help = "Apply exclusion regex to monitor Posts, mark filtered/kept."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True)
        parser.add_argument("--region", default="",
                            help="Region key for regional-specific rules "
                                 "(e.g. 'dagestan'). See pilot/filters.REGIONAL_PATTERNS.")
        parser.add_argument("--min-length", type=int, default=15,
                            help="Too-short messages drop. Default 15 chars — "
                                 "ловить «Махачкала?», «С Дербента» але лишає "
                                 "коротку критику типу «Путин — пидар лживый».")
        parser.add_argument("--max-length", type=int, default=800,
                            help="Too-long messages dropped as likely-posts, not comments.")
        parser.add_argument("--reset", action="store_true",
                            help="Wipe previous filter flags before running.")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=opts["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Task {opts['task']!r} not found.")

        region = opts["region"] or None
        min_len = opts["min_length"]
        max_len = opts["max_length"]

        qs = Post.objects.filter(task=task)
        total = qs.count()
        self.stdout.write(self.style.HTTP_INFO(
            f"== Filter {task.slug} | region={region or '-'} | "
            f"{min_len}≤len<{max_len} | posts={total} =="
        ))

        if opts["reset"]:
            with transaction.atomic():
                for p in qs.only("id", "classification"):
                    cl = dict(p.classification or {})
                    cl.pop("is_filtered", None)
                    cl.pop("exclusion_label", None)
                    cl.pop("exclusion_description", None)
                    cl.pop("filter_reason", None)
                    p.classification = cl
                    p.save(update_fields=["classification"])
            self.stdout.write("  reset previous filter flags")

        # Apply filters
        tally = Counter()
        kept = 0
        with transaction.atomic():
            for p in qs.iterator(chunk_size=500):
                text = p.text or ""
                cl = dict(p.classification or {})
                reason = self._reason(text, region, min_len, max_len)
                if reason:
                    cl["is_filtered"] = True
                    cl["exclusion_label"] = reason[0]
                    cl["exclusion_description"] = reason[1]
                    p.is_relevant = False
                    tally[reason[0]] += 1
                else:
                    cl["is_filtered"] = False
                    cl.pop("exclusion_label", None)
                    cl.pop("exclusion_description", None)
                    kept += 1
                p.classification = cl
                p.save(update_fields=["classification", "is_relevant"])

        self.stdout.write(self.style.SUCCESS(
            f"\nfiltered out: {sum(tally.values())} / {total} "
            f"({sum(tally.values())/max(total,1)*100:.0f}%)\n"
            f"kept for tagging: {kept}"
        ))
        for label, n in tally.most_common():
            self.stdout.write(f"  {label:24s} {n:>5d}")

    @staticmethod
    def _reason(text: str, region, min_len: int, max_len: int):
        if len(text) < min_len:
            return ("too_short", f"len<{min_len}")
        if len(text) >= max_len:
            return ("too_long", f"len≥{max_len} — likely a post, not a comment")
        return F.classify(text, region)
