"""
Split Events whose member posts span more than `max_gap` days between consecutive
posts. Each sub-cluster becomes a NEW Event inheriting the parent's summary/region/
tags/review_status (the audit verdict was on the same content, just spread). The
original Event is deleted only if it ends up empty.

This is the offline backfill counterpart to the `dedup_once` MAX_CLUSTER_GAP_DAYS
guard added in `services/stages.py`. Use it once to clean up Events that were
formed BEFORE the guard existed.

  python manage.py split_overspread_events                       # all over-spread events
  python manage.py split_overspread_events --max-gap 14          # custom gap (default 14)
  python manage.py split_overspread_events --dry-run             # report what would change
  python manage.py split_overspread_events --task ethnic-clashes # one task only
  python manage.py split_overspread_events --event 923           # split just one Event
"""
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from analysis.models import AnalysisTask, Event, Post


class Command(BaseCommand):
    help = "Split Events with posts spread across long time gaps into sub-Events"

    def add_arguments(self, parser):
        parser.add_argument("--max-gap", type=int, default=14,
                            help="Days; posts more than this apart start a new sub-Event (default 14)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Don't write — just print what would change")
        parser.add_argument("--task", default=None, help="Restrict to one task by slug")
        parser.add_argument("--event", type=int, default=None,
                            help="Process just one Event by id (for testing)")

    def handle(self, *args, **opts):
        max_gap = timedelta(days=opts["max_gap"])
        dry_run = opts["dry_run"]

        qs = Event.objects.all().prefetch_related("posts", "tags")
        if opts["event"]:
            qs = qs.filter(id=opts["event"])
        elif opts["task"]:
            task = AnalysisTask.objects.filter(slug=opts["task"]).first()
            if not task:
                self.stderr.write(f"Task '{opts['task']}' not found")
                return
            qs = qs.filter(task=task)

        n_seen = n_split = n_new_events = 0

        for ev in qs.iterator(chunk_size=100):
            posts = sorted(ev.posts.all(), key=lambda p: (p.posted_at, p.id))
            if len(posts) < 2:
                continue
            n_seen += 1

            # collect gap-bounded sub-clusters
            sub_clusters = []
            cur = [posts[0]]
            for p in posts[1:]:
                if p.posted_at - cur[-1].posted_at > max_gap:
                    sub_clusters.append(cur)
                    cur = []
                cur.append(p)
            sub_clusters.append(cur)

            if len(sub_clusters) == 1:
                continue            # event is contiguous — keep as is

            n_split += 1
            n_new_events += len(sub_clusters) - 1   # original becomes one of the subs

            if dry_run:
                spans = [(c[0].posted_at.date(), c[-1].posted_at.date(), len(c)) for c in sub_clusters]
                self.stdout.write(
                    f"  #{ev.id} ({ev.event_date}, {len(posts)} posts) → "
                    f"{len(sub_clusters)} sub-clusters: " +
                    ", ".join(f"[{a}..{b}:{n}]" for a, b, n in spans))
                continue

            self._apply_split(ev, sub_clusters)

        verb = "would split" if dry_run else "split"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb} {n_split} events ({n_seen} multi-post events seen), "
            f"created +{n_new_events} new events"))

    def _apply_split(self, original, sub_clusters):
        """Rewrite `original` to hold the FIRST sub-cluster, clone N-1 sibling Events
        for the rest. Each clone copies summary/region/tags/review_status from the
        original (audit verdict applies to the same content)."""
        with transaction.atomic():
            tag_ids = list(original.tags.values_list("id", flat=True))
            base = dict(
                task=original.task,
                region=original.region,
                region_subject=original.region_subject,
                settlement=original.settlement,
                summary=original.summary,
                review_status=original.review_status,
                review_notes=original.review_notes,
                reviewed_at=original.reviewed_at,
            )

            # first sub-cluster: rewrite the original Event to match it
            first = sub_clusters[0]
            original.event_date = first[0].posted_at.date()
            original.save(update_fields=["event_date"])

            # move posts not in the first sub-cluster off the original Event
            for sub in sub_clusters[1:]:
                new_ev = Event.objects.create(
                    event_date=sub[0].posted_at.date(),
                    **base,
                )
                if tag_ids:
                    new_ev.tags.set(tag_ids)
                Post.objects.filter(id__in=[p.id for p in sub]).update(event=new_ev)
                # recompute reach/post_count on the new event
                members = list(Post.objects.filter(event=new_ev).select_related("channel"))
                chans = {p.channel_id: (p.channel.subscribers or 0) for p in members if p.channel_id}
                new_ev.post_count = len(members)
                new_ev.channel_count = len(chans)
                new_ev.reach = sum(chans.values())
                new_ev.save(update_fields=["post_count", "channel_count", "reach"])

            # recompute reach/post_count on the original (it lost members)
            members = list(Post.objects.filter(event=original).select_related("channel"))
            chans = {p.channel_id: (p.channel.subscribers or 0) for p in members if p.channel_id}
            original.post_count = len(members)
            original.channel_count = len(chans)
            original.reach = sum(chans.values())
            original.save(update_fields=["post_count", "channel_count", "reach"])
