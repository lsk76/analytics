"""
Pilot: one-day comment-only TeleZip search to iterate on a federal-criticism query.

Each invocation = one cycle. The command:
  1. Calls TeleZip /Find for the given query over a single calendar day.
  2. Fetches channel meta for every unique channel id (so we know is_channel).
  3. Persists everything under a dedicated AnalysisTask `pilot-fed-criticism-comments`,
     tagging each Post with `classification._pilot_cycle = <cycle>` and the query
     used (`classification._pilot_query`) for later filtering / replay.
  4. Filters Posts to those whose Channel.is_channel == False (= chat / comments).
  5. Writes two artefacts to --report-dir:
       cycle<NN>_summary.json   — counters, top-chats, query string, env.
       cycle<NN>_sample.txt     — N random comment texts for manual relevance review.
  6. Updates a running per-chat hit table (cycle<NN>_top_chats.csv) — first step
     toward a whitelist of chats we will later target directly.

We deliberately avoid the classification/dedup pipeline here: the pilot is about
recall + precision of the QUERY, not about distilling events.
"""
from __future__ import annotations

import asyncio
import csv
import json
import random
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count

from analysis.models import AnalysisTask, Channel, Post
from analysis.services.telezip import TelezipClient

PILOT_SLUG = "pilot-fed-criticism-comments"


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


class Command(BaseCommand):
    help = (
        "Pilot one-day TeleZip search filtered to comments (chat messages). "
        "Persists posts + channel meta, emits a sample for manual relevance review."
    )

    def add_arguments(self, parser):
        parser.add_argument("--query", required=True,
                            help="TeleZip search query (see existing tasks for syntax).")
        parser.add_argument("--date", required=True,
                            help="Single day, YYYY-MM-DD (UTC).")
        parser.add_argument("--cycle", type=int, required=True,
                            help="Cycle number for logging / artefacts.")
        parser.add_argument("--sample", type=int, default=60,
                            help="How many random comments to write into the sample.txt.")
        parser.add_argument("--report-dir",
                            default="/app/backend/_pilot_fed_criticism",
                            help="Where to put JSON/CSV/TXT artefacts (in-container path).")
        parser.add_argument("--languages", default="ru",
                            help="Comma-separated language codes (or empty).")
        parser.add_argument("--chunk-hours", type=int, default=1,
                            help="TeleZip has an internal search timeout. Broad queries "
                                 "must be split into N-hour chunks (default 1h).")
        parser.add_argument("--channel-ids", default="",
                            help="Comma-separated TG channel ids — narrows search to these.")
        parser.add_argument("--channel-names", default="",
                            help="Comma-separated TG usernames — narrows search to these.")

    # ---- TeleZip helpers ---------------------------------------------------

    async def _find(self, query: str, dfrom: datetime, dto: datetime,
                    languages: Optional[List[str]],
                    chunk_hours: int = 1,
                    channel_ids: Optional[List[int]] = None,
                    channel_names: Optional[List[str]] = None,
                    ) -> List[Dict[str, Any]]:
        """Chunk the [dfrom, dto] window into hourly pieces; bail on chunk failure."""
        from datetime import timedelta
        step = timedelta(hours=max(1, chunk_hours))
        out: List[Dict[str, Any]] = []
        seen_urls: set = set()
        async with TelezipClient(settings.TELEZIP_API_KEY,
                                 settings.TELEZIP_BASE_URL) as tz:
            cur = dfrom
            chunk_i = 0
            while cur < dto:
                nxt = min(cur + step, dto)
                chunk_i += 1
                try:
                    batch = await tz.find_posts(
                        query, cur, nxt, languages, unique=False,
                        channel_ids=channel_ids, channel_names=channel_names,
                    )
                except Exception as e:
                    self.stdout.write(self.style.WARNING(
                        f"  chunk {chunk_i} {cur.time()}-{nxt.time()} FAILED: {e}"))
                    cur = nxt
                    continue
                # dedup by message_url across chunks (TeleZip occasionally
                # returns boundary overlaps)
                new = 0
                for r in batch:
                    u = r.get("message_url")
                    if u and u in seen_urls:
                        continue
                    if u:
                        seen_urls.add(u)
                    out.append(r)
                    new += 1
                self.stdout.write(
                    f"  chunk {chunk_i:>2} {cur.strftime('%H:%M')}–"
                    f"{nxt.strftime('%H:%M')}: +{new} (raw {len(batch)})"
                )
                cur = nxt
        return out

    async def _fetch_channels(self, cids: List[int]) -> Dict[int, Dict[str, Any]]:
        out: Dict[int, Dict[str, Any]] = {}
        if not cids:
            return out
        sem = asyncio.Semaphore(2)  # TeleZip allows 2 parallel
        async with TelezipClient(settings.TELEZIP_API_KEY,
                                 settings.TELEZIP_BASE_URL) as tz:
            async def one(cid: int):
                async with sem:
                    out[cid] = await tz.get_channel(cid)
            await asyncio.gather(*[one(c) for c in cids])
        return out

    # ---- main --------------------------------------------------------------

    def handle(self, *args, **opts):
        query = opts["query"]
        cycle = opts["cycle"]
        try:
            d = date.fromisoformat(opts["date"])
        except ValueError as e:
            raise CommandError(f"--date must be YYYY-MM-DD: {e}")

        sample_n = opts["sample"]
        report_dir = Path(opts["report_dir"])
        report_dir.mkdir(parents=True, exist_ok=True)
        languages = [s.strip() for s in (opts["languages"] or "").split(",") if s.strip()]

        # 1. pilot task -----------------------------------------------------
        task, created = AnalysisTask.objects.get_or_create(
            slug=PILOT_SLUG,
            defaults=dict(
                name="Pilot: federal-criticism (comments)",
                description="Iterative pilot — comment-only TeleZip search for "
                            "criticism of federal authorities.",
                telezip_query=query,
                date_from=d, date_to=d,
                languages=languages,
                search_posts=False, search_comments=True,
                telezip_unique=False, collect_chunk_days=1,
                classify_system_prompt="(pilot — classification deferred)",
                relevance_field="is_relevant",
            ),
        )
        # keep the LATEST query / range on the task for visibility
        if not created:
            task.telezip_query = query
            task.date_from = d
            task.date_to = d
            task.languages = languages
            task.save(update_fields=["telezip_query", "date_from", "date_to", "languages"])

        dfrom = datetime.combine(d, time.min, tzinfo=timezone.utc)
        dto = datetime.combine(d, time.max, tzinfo=timezone.utc)

        self.stdout.write(self.style.HTTP_INFO(
            f"== Cycle {cycle} | day {d} | query_len {len(query)} =="))
        channel_ids = [int(x) for x in opts["channel_ids"].split(",") if x.strip()]
        channel_names = [x.strip() for x in opts["channel_names"].split(",") if x.strip()]
        scope = ""
        if channel_ids:   scope += f" | ids={len(channel_ids)}"
        if channel_names: scope += f" | names={len(channel_names)}"
        self.stdout.write(
            f"step 1: TeleZip /Find — {(dto-dfrom).total_seconds()/3600:.0f}h "
            f"window, {opts['chunk_hours']}h chunks{scope} ..."
        )
        raw = asyncio.run(self._find(
            query, dfrom, dto, languages or None,
            chunk_hours=opts['chunk_hours'],
            channel_ids=channel_ids or None,
            channel_names=channel_names or None,
        ))
        self.stdout.write(f"  raw hits: {len(raw)}")

        if not raw:
            self.stdout.write(self.style.WARNING("no hits — bail out"))
            return

        # 2. channel meta ---------------------------------------------------
        unique_cids = sorted({r["channel_id"] for r in raw if r.get("channel_id")})
        known = {c.tg_id: c for c in Channel.objects.filter(tg_id__in=unique_cids)}
        missing = [c for c in unique_cids if c not in known]
        self.stdout.write(f"step 2: channels — {len(unique_cids)} unique, "
                          f"{len(known)} cached, {len(missing)} to fetch")

        meta = asyncio.run(self._fetch_channels(missing))
        now = datetime.now(timezone.utc)
        for cid, m in meta.items():
            if not m:
                continue
            ch, _ = Channel.objects.update_or_create(
                tg_id=m["tg_id"] or cid,
                defaults=dict(
                    username=m["username"] or "",
                    title=m["title"] or "",
                    description=m["about"] or "",
                    subscribers=m["subscribers"] or 0,
                    language=m["language"] or "",
                    is_channel=m["is_channel"],
                    enriched=True,
                    fetched_at=now,
                ),
            )
            # tag the raw_meta with this pilot run
            ch.raw_meta = {**(ch.raw_meta or {}),
                           "pilot_fed_criticism": {
                               "last_cycle": cycle,
                               "last_seen": d.isoformat(),
                           }}
            ch.save(update_fields=["raw_meta"])
            known[cid] = ch

        # 3. persist posts --------------------------------------------------
        n_saved = 0
        n_skipped_no_url = 0
        n_skipped_no_chan = 0
        with transaction.atomic():
            for r in raw:
                url = r.get("message_url")
                if not url:
                    n_skipped_no_url += 1
                    continue
                cid = r.get("channel_id")
                ch = known.get(cid)
                if not ch:
                    n_skipped_no_chan += 1
                    continue
                post, _ = Post.objects.update_or_create(
                    task=task, url=url,
                    defaults=dict(
                        channel=ch,
                        channel_name=r.get("channel_name") or "",
                        telezip_date=_parse_dt(r.get("date")),
                        posted_at=_parse_dt(r.get("date")),
                        text=r.get("content") or "",
                        content_hash=r.get("content_hash") or "",
                        telezip_mid=r.get("mid"),
                    ),
                )
                post.classification = {
                    **(post.classification or {}),
                    "_pilot_cycle": cycle,
                    "_pilot_query": query[:300],
                    "_tz_channel_id": cid,
                }
                post.save(update_fields=["classification"])
                n_saved += 1
        self.stdout.write(f"step 3: saved {n_saved} posts "
                          f"(skipped {n_skipped_no_url} no-url, "
                          f"{n_skipped_no_chan} no-channel)")

        # 4. split posts / comments ----------------------------------------
        cycle_posts_qs = Post.objects.filter(
            task=task,
            classification___pilot_cycle=cycle,
        )
        n_total = cycle_posts_qs.count()
        n_unknown = cycle_posts_qs.filter(channel__is_channel__isnull=True).count()
        n_channels = cycle_posts_qs.filter(channel__is_channel=True).count()
        n_comments = cycle_posts_qs.filter(channel__is_channel=False).count()

        comments_qs = cycle_posts_qs.filter(channel__is_channel=False)\
                                    .exclude(text="")

        # top chats by # comments in this cycle
        top_chats_rows = (
            comments_qs.values("channel_id", "channel__tg_id",
                               "channel__username", "channel__title",
                               "channel__subscribers", "channel__inferred_region")
            .annotate(hits=Count("id"))
            .order_by("-hits")[:50]
        )
        top_chats = list(top_chats_rows)

        # 5. write artefacts ------------------------------------------------
        summary = {
            "cycle": cycle,
            "date": d.isoformat(),
            "query": query,
            "languages": languages,
            "raw_hits": len(raw),
            "unique_channels": len(unique_cids),
            "channels_fetched": len(meta),
            "posts_saved": n_saved,
            "by_channel_kind": {
                "channels": n_channels,
                "comments": n_comments,
                "unknown": n_unknown,
                "total": n_total,
            },
            "comments_ratio": round(n_comments / n_total, 3) if n_total else 0,
            "top_chats": top_chats,
        }
        sumf = report_dir / f"cycle{cycle:02d}_summary.json"
        sumf.write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                   default=str))
        self.stdout.write(f"wrote {sumf}")

        # top chats csv (for later aggregation across cycles)
        csvf = report_dir / f"cycle{cycle:02d}_top_chats.csv"
        with csvf.open("w", newline="") as fp:
            w = csv.DictWriter(fp, fieldnames=[
                "cycle", "channel_id", "tg_id", "username", "title",
                "subscribers", "inferred_region", "hits",
            ])
            w.writeheader()
            for row in top_chats:
                w.writerow({
                    "cycle": cycle,
                    "channel_id": row["channel_id"],
                    "tg_id": row["channel__tg_id"],
                    "username": row["channel__username"],
                    "title": row["channel__title"],
                    "subscribers": row["channel__subscribers"],
                    "inferred_region": row["channel__inferred_region"],
                    "hits": row["hits"],
                })
        self.stdout.write(f"wrote {csvf}")

        # sample for manual review
        all_ids = list(comments_qs.values_list("id", flat=True))
        random.shuffle(all_ids)
        sample_ids = all_ids[:sample_n]
        sample_posts = list(
            Post.objects.filter(id__in=sample_ids)
            .select_related("channel")
            .values("id", "url", "text", "channel__username", "channel__title",
                    "channel__inferred_region", "channel__subscribers")
        )
        sampf = report_dir / f"cycle{cycle:02d}_sample.txt"
        with sampf.open("w") as fp:
            fp.write(f"# cycle {cycle} | {d} | query:\n# {query}\n\n")
            fp.write(f"# total comments this cycle: {n_comments}\n")
            fp.write(f"# sample size: {len(sample_posts)} (random)\n\n")
            fp.write("=" * 80 + "\n")
            for i, p in enumerate(sample_posts, 1):
                fp.write(f"\n[{i}/{len(sample_posts)}] post_id={p['id']}\n")
                fp.write(f"  chat: {p['channel__username']} | "
                         f"{p['channel__title']} | "
                         f"region={p['channel__inferred_region'] or '?'} | "
                         f"subs={p['channel__subscribers']}\n")
                fp.write(f"  url: {p['url']}\n")
                fp.write(f"  text: {(p['text'] or '').strip()[:500]}\n")
                fp.write("-" * 80 + "\n")
        self.stdout.write(f"wrote {sampf}")

        # 6. console summary -----------------------------------------------
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"raw={len(raw)} unique_channels={len(unique_cids)} "
            f"saved={n_saved} | channels={n_channels} comments={n_comments} "
            f"unknown={n_unknown} | comments_ratio={summary['comments_ratio']}"
        ))
        self.stdout.write("top-5 chats by hits:")
        for r in top_chats[:5]:
            self.stdout.write(
                f"  {r['hits']:>4}  @{r['channel__username'] or '-'}  "
                f"({r['channel__title'][:50]}) "
                f"reg={r['channel__inferred_region'] or '-'}"
            )
