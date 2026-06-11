"""
Pre-screen monitor Posts: швидкий yes/no «could_be_criticism» через LLM-агентів.

Мета: знизити обсяг для повноцінного tagger у 5-10 разів. Cluster-only collect
дає ~5k повідомлень/день/регіон, але після prescreen лишається ~500-1000.

Pipeline:
  1. Selects Posts that passed monitor_filter (is_filtered != True) but ще не
     pre-screened (classification._prescreen not set).
  2. Розбиває на батчі по --batch-size (default 200, бо кожне рішення легке).
  3. Пише JSON-файл на батч: items=[{id, text}, ...] + PRESCREEN_SYSTEM_PROMPT.md
     поряд.
  4. Я (Claude Code) запускаю Agents паралельно — кожен зчитує батч,
     повертає JSON {items:[{could_be_criticism, confidence},...]}, пише
     `batch_NNN_done.json`.
  5. monitor_ingest_prescreen зчитує done-файли, виставляє
     `Post.classification.could_be_criticism = True/False` і
     `Post.is_relevant = True` для positive (тимчасово, до повного tagger).

Examples:
  python manage.py monitor_prescreen --task dagestan-criticism-monitor \\
      --out-dir /app/backend/_pilot_fed_criticism/dagestan_prescreen \\
      --batch-size 200

Потім після того як я заповню done-файли:
  python manage.py monitor_prescreen --task dagestan-criticism-monitor \\
      --done-dir /app/backend/_pilot_fed_criticism/dagestan_prescreen --ingest
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from analysis.models import AnalysisTask, Post
from analysis.pilot.logging import open_log
from analysis.pilot.prompts import PRESCREEN_SYSTEM_PROMPT


class Command(BaseCommand):
    help = ("Pre-screen monitor posts: lightweight LLM yes/no «could be "
            "criticism?» on big batches before full tagging.")

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True)
        parser.add_argument("--out-dir",
                            help="Куди писати batch_NNN.json (для prepare). "
                                 "Обов'язковий без --ingest.")
        parser.add_argument("--done-dir",
                            help="Звідки зчитувати batch_NNN_done.json (для ingest).")
        parser.add_argument("--batch-size", type=int, default=200)
        parser.add_argument("--max-batches", type=int, default=0,
                            help="0 = no cap.")
        parser.add_argument("--ingest", action="store_true",
                            help="Режим інгестії: зчитати done-файли і записати "
                                 "у Post.classification.could_be_criticism.")
        parser.add_argument("--reset", action="store_true",
                            help="Скинути попередні prescreen-флаги перед prep.")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=opts["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Task {opts['task']!r} not found.")

        if opts["ingest"]:
            return self._do_ingest(task, opts)
        return self._do_prepare(task, opts)

    # ---- Prepare batches -------------------------------------------------

    def _do_prepare(self, task, opts):
        if not opts["out_dir"]:
            raise CommandError("--out-dir required without --ingest")
        out = Path(opts["out_dir"])
        out.mkdir(parents=True, exist_ok=True)

        log, log_path = open_log("monitor_prescreen_prep", task.slug)
        log(f"log: {log_path}")

        qs = (Post.objects.filter(task=task)
              .exclude(text="")
              .exclude(classification__is_filtered=True))
        if not opts["reset"]:
            qs = qs.exclude(classification__has_key="_prescreen")

        qs = qs.order_by("posted_at", "id").only("id", "text")
        total = qs.count()
        log(f"posts to prescreen: {total} | batch_size={opts['batch_size']}")
        if total == 0:
            log("nothing to do")
            return

        bsz = max(1, opts["batch_size"])
        max_b = opts["max_batches"] or 10_000
        ts_now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Write sidecar prompt for orchestrator
        (out / "PRESCREEN_PROMPT.md").write_text(
            f"# Pre-screen system prompt\n\n"
            f"Task: `{task.slug}` | Generated: {ts_now}\n\n"
            f"---\n\n```\n{PRESCREEN_SYSTEM_PROMPT}\n```\n"
        )

        batch_idx = 0
        written = 0
        buf = []
        for p in qs.iterator(chunk_size=500):
            buf.append({"id": p.id, "text": (p.text or "").strip()[:1000]})
            if len(buf) >= bsz:
                batch_idx += 1
                self._write_batch(out, batch_idx, buf, task, ts_now)
                written += len(buf)
                buf = []
                if batch_idx >= max_b: break
        if buf and batch_idx < max_b:
            batch_idx += 1
            self._write_batch(out, batch_idx, buf, task, ts_now)
            written += len(buf)

        log(f"wrote {batch_idx} batches × ~{bsz} = {written} comments → {out}")

    @staticmethod
    def _write_batch(out: Path, idx: int, items: list,
                     task, ts: str):
        (out / f"batch_{idx:03d}.json").write_text(json.dumps({
            "meta": {
                "task_slug": task.slug, "batch_id": idx,
                "stage": "prescreen", "n_items": len(items),
                "created_at": ts,
            },
            "system_prompt_ref": "PRESCREEN_PROMPT.md",
            "items": items,
        }, ensure_ascii=False, indent=2))

    # ---- Ingest done -----------------------------------------------------

    def _do_ingest(self, task, opts):
        if not opts["done_dir"]:
            raise CommandError("--done-dir required for --ingest")
        ddir = Path(opts["done_dir"])
        files = sorted(ddir.glob("*_done.json"))
        if not files:
            self.stdout.write(self.style.WARNING(
                f"No *_done.json in {ddir}"
            ))
            return

        log, log_path = open_log("monitor_prescreen_ingest", task.slug)
        log(f"log: {log_path}")
        log(f"ingesting {len(files)} done-batches")

        n_total = 0
        n_pos = 0
        n_skip = 0
        for f in files:
            try:
                data = json.loads(f.read_text())
            except Exception as e:
                log(f"  {f.name}: parse fail: {e}"); continue
            items = data.get("items") or []
            with transaction.atomic():
                for it in items:
                    n_total += 1
                    pid = it.get("id")
                    verdict = bool(it.get("could_be_criticism"))
                    conf = float(it.get("confidence") or 0.0)
                    try:
                        post = Post.objects.get(id=pid, task=task)
                    except Post.DoesNotExist:
                        n_skip += 1; continue
                    cl = dict(post.classification or {})
                    cl["_prescreen"] = {
                        "could_be_criticism": verdict,
                        "confidence": conf,
                    }
                    post.classification = cl
                    post.save(update_fields=["classification"])
                    if verdict: n_pos += 1
            log(f"  {f.name}: {len(items)} items processed")

        log(f"DONE: total={n_total} | could_be_criticism={n_pos} "
            f"({n_pos/max(n_total,1)*100:.0f}%) | skipped={n_skip}")
