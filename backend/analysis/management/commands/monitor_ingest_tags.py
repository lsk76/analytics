"""
Ingest Agent results back into Post.tags.

Reads batch_<NNN>_done.json files from --done-dir. Format expected:

    {
      "meta": {"task_slug": "...", "batch_id": NN, ...},
      "items": [
        {
          "id": <post_id>,
          "verdict": {
            "criticism_target": ["крит_путін", "крит_меликова"],
            "topic": ["тема_СВО"],
            "opinion": ["сарказм"],
            "proposed_tags": [
              {"category": "criticism_target", "name": "крит_меликова",
               "reason": "коментар про колишнього главу регіону Меликова"}
            ],
            "confidence": 0.9
          }
        }, ...
      ]
    }

For each item:
  * resolve each tag (criticism_target / topic / opinion) — get_or_create
    Tag rows (auto-create allowed since user chose option (c) — bulk review later).
  * attach to Post.tags
  * persist verdict raw into Post.classification.opinion = {...}.

Examples:
  python manage.py monitor_ingest_tags \
      --task dagestan-criticism-monitor \
      --done-dir /app/backend/_pilot_fed_criticism/dagestan_batches
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from analysis.models import AnalysisTask, Post, Tag
from analysis.services.monitor_stages import sync_comment_event


VALID_CATEGORIES = {"criticism_target", "topic", "opinion", "fed_crit"}
FED_CRIT_YES = "критика_фед_влади"      # головна вісь задач із категорією fed_crit


class Command(BaseCommand):
    help = "Ingest LLM tagger results into Post.tags."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True)
        parser.add_argument("--done-dir", required=True,
                            help="Directory with batch_<NNN>_done.json files.")
        parser.add_argument("--glob", default="*_done.json",
                            help="Glob for done files (default *_done.json).")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=opts["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Task {opts['task']!r} not found.")

        ddir = Path(opts["done_dir"])
        if not ddir.is_dir():
            raise CommandError(f"--done-dir not a directory: {ddir}")
        files = sorted(ddir.glob(opts["glob"]))
        if not files:
            self.stdout.write(self.style.WARNING(
                f"No files matching {opts['glob']} in {ddir}"
            ))
            return

        self.stdout.write(self.style.HTTP_INFO(
            f"== Ingest {len(files)} done-batches for {task.slug} =="
        ))

        n_items = n_applied = n_skipped = 0
        n_new_tags = 0
        tag_cache: dict[tuple[str, str], Tag] = {}

        def _get_tag(category: str, name: str) -> Tag | None:
            if not name: return None
            if category not in VALID_CATEGORIES:
                return None
            key = (category, name.strip())
            if key in tag_cache: return tag_cache[key]
            tag, created = Tag.objects.get_or_create(
                name=key[1], category=category,
            )
            tag_cache[key] = tag
            nonlocal n_new_tags
            if created: n_new_tags += 1
            return tag

        for f in files:
            try:
                data = json.loads(f.read_text())
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  {f.name}: parse fail: {e}"))
                continue
            items = data.get("items") or []
            for it in items:
                n_items += 1
                pid = it.get("id")
                verdict = it.get("verdict") or {}
                if not pid or not verdict:
                    n_skipped += 1; continue
                try:
                    post = Post.objects.get(id=pid, task=task)
                except Post.DoesNotExist:
                    n_skipped += 1; continue

                tags_to_attach = []
                for cat in VALID_CATEGORIES:
                    for name in (verdict.get(cat) or []):
                        t = _get_tag(cat, name)
                        if t: tags_to_attach.append(t)

                # proposed_tags — теж створюємо одразу (опція (c))
                for pt in (verdict.get("proposed_tags") or []):
                    cat = pt.get("category")
                    name = pt.get("name")
                    if cat in VALID_CATEGORIES:
                        t = _get_tag(cat, name)
                        if t: tags_to_attach.append(t)

                if opts["dry_run"]:
                    n_applied += 1
                    continue

                with transaction.atomic():
                    if tags_to_attach:
                        post.tags.add(*tags_to_attach)
                    cl = dict(post.classification or {})
                    cl["opinion"] = {
                        **verdict,
                        "_ingest_batch": data.get("meta", {}).get("batch_id"),
                    }
                    post.is_classified = True
                    # is_relevant. Дві схеми, розрізняються вердиктом:
                    #   fed_crit  — бінарна вісь «критика фед. влади / інше»
                    #               (решта тегів лишається деталізацією);
                    #   інакше    — стара схема: будь-який criticism_target.
                    fed = verdict.get("fed_crit")
                    if fed is not None:
                        vals = fed if isinstance(fed, list) else [fed]
                        post.is_relevant = FED_CRIT_YES in [str(v).strip() for v in vals]
                    else:
                        post.is_relevant = bool(verdict.get("criticism_target") or [])
                    post.classification = cl
                    post.save(update_fields=["classification", "is_classified",
                                             "is_relevant"])
                    # єдина структура моделей: relevant коментар <=> Event 1:1
                    # (створює/оновлює/знімає подію за фактичним is_relevant)
                    sync_comment_event(post)
                n_applied += 1
            self.stdout.write(f"  {f.name}: {len(items)} items")

        self.stdout.write(self.style.SUCCESS(
            f"\nitems read: {n_items} | applied: {n_applied} | "
            f"skipped: {n_skipped} | new tags created: {n_new_tags}"
        ))
