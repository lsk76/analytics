"""
Validation step — другий, строгіший прохід над уже-релевантними постами через
Claude Haiku-агентів (розумніша модель, ніж API-теґер на Gemini Flash).

Закриває хибнопозитиви, знайдені на ручному аудиті (~27%): pro-влада-сарказм,
фрагменти, захист авторитета, нейтральні новини.

Pipeline (як prescreen/tagger — file-based для Claude Code агентів):
  1. --prepare: вибирає Post(is_relevant=True), ще не валідовані → batch JSON
     ({id, text, current_target}) + VALIDATOR_PROMPT.md поряд.
  2. Я (Claude Code) запускаю N Haiku-агентів, кожен зчитує батч, перечитує
     кожен пост і пише batch_NNN_done.json з {id, valid, criticism_target, reason}.
  3. --ingest: зчитує done-файли:
        valid=false → is_relevant=False, прибирає criticism_target-теги,
                      classification._validation={valid:false, reason}
        valid=true  → лишає relevant, синхронізує criticism_target теги з
                      виправленими, classification._validation={valid:true}

Examples:
  python manage.py monitor_validate --task dagestan-criticism-monitor \\
      --out-dir /app/backend/_pilot_fed_criticism/dagestan_validate \\
      --batch-size 110
  # ... я ганяю агентів ...
  python manage.py monitor_validate --task dagestan-criticism-monitor \\
      --done-dir /app/backend/_pilot_fed_criticism/dagestan_validate --ingest
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from analysis.models import AnalysisTask, Post, Tag
from analysis.pilot.logging import open_log
from analysis.pilot.prompts import VALIDATOR_SYSTEM_PROMPT


class Command(BaseCommand):
    help = "Validate is_relevant posts via Haiku agents (2nd strict pass)."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True)
        parser.add_argument("--out-dir", help="Куди писати batch_NNN.json (prepare).")
        parser.add_argument("--done-dir", help="Звідки читати batch_NNN_done.json (ingest).")
        parser.add_argument("--batch-size", type=int, default=110)
        parser.add_argument("--ingest", action="store_true")
        parser.add_argument("--revalidate", action="store_true",
                            help="Перевалідувати навіть уже валідовані пости.")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=opts["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Task {opts['task']!r} not found.")
        if opts["ingest"]:
            return self._ingest(task, opts)
        return self._prepare(task, opts)

    # ---- prepare ---------------------------------------------------------
    def _prepare(self, task, opts):
        if not opts["out_dir"]:
            raise CommandError("--out-dir required without --ingest")
        out = Path(opts["out_dir"]); out.mkdir(parents=True, exist_ok=True)
        log, log_path = open_log("monitor_validate_prep", task.slug)
        log(f"log: {log_path}")

        qs = Post.objects.filter(task=task, is_relevant=True)
        if not opts["revalidate"]:
            qs = qs.exclude(classification__has_key="_validation")
        qs = qs.prefetch_related("tags").order_by("id")
        total = qs.count()
        log(f"relevant to validate: {total} | batch={opts['batch_size']}")
        if total == 0:
            log("nothing to validate"); return

        (out / "VALIDATOR_PROMPT.md").write_text(
            f"# Validator system prompt\n\nTask: `{task.slug}`\n\n---\n\n"
            f"```\n{VALIDATOR_SYSTEM_PROMPT}\n```\n")

        bsz = max(1, opts["batch_size"])
        buf, idx, written = [], 0, 0
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for p in qs.iterator(chunk_size=200):
            tgt = sorted({t.name for t in p.tags.all()
                          if t.category == "criticism_target"})
            buf.append({"id": p.id, "current_target": tgt,
                        "text": (p.text or "").strip()[:1500]})
            if len(buf) >= bsz:
                idx += 1; self._write(out, idx, buf, task, ts); written += len(buf); buf = []
        if buf:
            idx += 1; self._write(out, idx, buf, task, ts); written += len(buf)
        log(f"wrote {idx} batches × ~{bsz} = {written} → {out}")

    @staticmethod
    def _write(out, idx, items, task, ts):
        (out / f"batch_{idx:03d}.json").write_text(json.dumps({
            "meta": {"task_slug": task.slug, "batch_id": idx, "stage": "validate",
                     "n_items": len(items), "created_at": ts},
            "system_prompt_ref": "VALIDATOR_PROMPT.md",
            "items": items,
        }, ensure_ascii=False, indent=2))

    # ---- ingest ----------------------------------------------------------
    def _ingest(self, task, opts):
        if not opts["done_dir"]:
            raise CommandError("--done-dir required for --ingest")
        files = sorted(Path(opts["done_dir"]).glob("*_done.json"))
        if not files:
            self.stdout.write(self.style.WARNING("No *_done.json")); return
        log, log_path = open_log("monitor_validate_ingest", task.slug)
        log(f"log: {log_path} | {len(files)} done-batches")

        ct = {c.name: c for c in Tag.objects.filter(category="criticism_target")}
        n=valid=invalid=skip=0
        for f in files:
            try:
                data = json.loads(f.read_text())
            except Exception as e:
                log(f"  {f.name}: parse fail {e}"); continue
            with transaction.atomic():
                for it in (data.get("items") or []):
                    n += 1
                    try:
                        p = Post.objects.get(id=it.get("id"), task=task)
                    except Post.DoesNotExist:
                        skip += 1; continue
                    is_valid = bool(it.get("valid"))
                    cl = dict(p.classification or {})
                    cl["_validation"] = {"valid": is_valid,
                                         "reason": (it.get("reason") or "")[:200]}
                    if is_valid:
                        # синхронізуємо criticism_target теги з виправленими
                        want = [ct[name] for name in (it.get("criticism_target") or [])
                                if name in ct]
                        cur = [t for t in p.tags.all() if t.category == "criticism_target"]
                        p.tags.remove(*cur)
                        if want:
                            p.tags.add(*want)
                        valid += 1
                    else:
                        cur = [t for t in p.tags.all() if t.category == "criticism_target"]
                        if cur:
                            p.tags.remove(*cur)
                        p.is_relevant = False
                        invalid += 1
                    p.classification = cl
                    p.save(update_fields=["classification", "is_relevant"])
            log(f"  {f.name}: {len(data.get('items') or [])} items")
        log(f"DONE: {n} judged | valid={valid} | invalid(downgraded)={invalid} "
            f"| skipped={skip}")
