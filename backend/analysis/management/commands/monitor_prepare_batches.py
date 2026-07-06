"""
Prepare JSON batches for Variant-A tagging (Claude Code Agent runs).

What it does:
  1. Selects unfiltered, untagged Posts of the given task.
  2. Splits them into batches of --batch-size (default 50).
  3. Writes each batch to <out_dir>/batch_<NNN>.json with:
       - system: TAGGER_SYSTEM_PROMPT (from analysis/pilot/prompts.py)
       - items: [{id, chat, region, text}, ...]
       - meta:  {task_slug, region, created_at, batch_id, ...}

Then the orchestrator (Claude Code, me) reads each batch file, runs the
Agent(model='haiku', subagent_type='general-purpose') with the system prompt
+ user_prompt(items), parses the JSON response and writes:
       <out_dir>/batch_<NNN>_done.json

Finally `monitor_ingest_tags` reads the done files and applies tags to Post.

Examples:
  python manage.py monitor_prepare_batches \
      --task dagestan-criticism-monitor --region dagestan \
      --out-dir /app/backend/_pilot_fed_criticism/dagestan_batches \
      --batch-size 50
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from analysis.models import AnalysisTask, Post
from analysis.pilot.prompts import TAGGER_SYSTEM_PROMPT


class Command(BaseCommand):
    help = "Prepare JSON batches of comments for Claude Code Agent tagging."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True)
        parser.add_argument("--region", default="",
                            help="Region label for context inside each batch.")
        parser.add_argument("--out-dir", required=True,
                            help="Where to write batch_<NNN>.json files.")
        parser.add_argument("--batch-size", type=int, default=50)
        parser.add_argument("--max-batches", type=int, default=0,
                            help="0 = no cap.")
        parser.add_argument("--only-untagged", action="store_true", default=True,
                            help="Skip Posts that already have tags (default ON).")
        parser.add_argument("--include-filtered", action="store_true",
                            help="Include is_filtered=True posts (debug).")
        parser.add_argument("--require-prescreen", action="store_true",
                            help="Беремо лише пости, які пройшли prescreen "
                                 "(_prescreen.could_be_criticism=True). "
                                 "Сильно зменшує обсяг для повноцінного tagger.")
        parser.add_argument("--prefix", default="batch",
                            help="Filename prefix (default 'batch').")
        parser.add_argument("--date-from", default="",
                            help="YYYY-MM-DD; restrict by posted_at (optional).")
        parser.add_argument("--date-to", default="",
                            help="YYYY-MM-DD; restrict by posted_at (optional).")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=opts["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Task {opts['task']!r} not found.")

        out = Path(opts["out_dir"])
        out.mkdir(parents=True, exist_ok=True)

        qs = (Post.objects.filter(task=task)
              .select_related("channel")
              .exclude(text=""))

        if opts["date_from"]:
            qs = qs.filter(posted_at__date__gte=date.fromisoformat(opts["date_from"]))
        if opts["date_to"]:
            qs = qs.filter(posted_at__date__lte=date.fromisoformat(opts["date_to"]))

        if not opts["include_filtered"]:
            qs = qs.exclude(classification__is_filtered=True)

        if opts["only_untagged"]:
            qs = qs.filter(tags__isnull=True)

        if opts["require_prescreen"]:
            qs = qs.filter(
                classification__has_key="_prescreen",
                classification___prescreen__could_be_criticism=True,
            )

        qs = qs.distinct().order_by("posted_at", "id")

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("No posts to batch."))
            return

        bsz = max(1, opts["batch_size"])
        max_b = opts["max_batches"] or 10_000

        region = opts["region"]
        prefix = opts["prefix"]
        ts_now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        batch_idx = 0
        written = 0
        buf = []
        for p in qs.iterator(chunk_size=200):
            buf.append({
                "id": p.id,
                "chat": (p.channel.username if p.channel else "") or p.channel_name,
                "region": region or (p.channel.inferred_region if p.channel else ""),
                "text": (p.text or "").strip(),
            })
            if len(buf) >= bsz:
                batch_idx += 1
                self._write_batch(out, prefix, batch_idx, buf, task, region,
                                  ts_now)
                written += len(buf)
                buf = []
                if batch_idx >= max_b: break

        if buf and batch_idx < max_b:
            batch_idx += 1
            self._write_batch(out, prefix, batch_idx, buf, task, region, ts_now)
            written += len(buf)

        # Also write the system prompt + how-to-use as a sidecar so the
        # human (or the orchestrator) doesn't have to dig for it.
        (out / "SYSTEM_PROMPT.md").write_text(
            f"# Tagger system prompt\n\n"
            f"Task: `{task.slug}` | Region: `{region or '-'}`\n"
            f"Generated: {ts_now}\n\n"
            f"---\n\n```\n{task.tagger_prompt or TAGGER_SYSTEM_PROMPT}\n```\n"
        )

        self.stdout.write(self.style.SUCCESS(
            f"wrote {batch_idx} batches × ~{bsz} items = {written} comments → {out}"
        ))

    def _write_batch(self, out: Path, prefix: str, idx: int,
                     items: list, task, region: str, ts: str):
        path = out / f"{prefix}_{idx:03d}.json"
        path.write_text(json.dumps({
            "meta": {
                "task_slug": task.slug,
                "region": region,
                "batch_id": idx,
                "created_at": ts,
                "n_items": len(items),
            },
            "system_prompt_ref": "SYSTEM_PROMPT.md",
            "items": items,
        }, ensure_ascii=False, indent=2))
