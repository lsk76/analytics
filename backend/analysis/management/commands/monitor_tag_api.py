"""
Тегування monitor-коментарів через OpenRouter API (заміна агентних пачок).

Агентний шлях (monitor_prepare_batches -> Claude-агенти -> monitor_ingest_tags)
лишається каноном для суцільних зборів. Але при ВИБІРКОВОМУ зборі обсяг — десятки
тисяч коментарів на місяць, тобто сотні пачок, які треба оркеструвати хвилями
вручну; саме на цьому зупинилася кампанія лютого 2026 (946 пачок). Тут те саме
робить API-виклик.

Схема вердикту — та сама, що читає monitor_ingest_tags:
    {"fed_crit": "критика_фед_влади"|"інше",
     "criticism_target": [...], "topic": [...], "confidence": 0.0-1.0}
fed_crit — головна вісь, від неї is_relevant; решта — деталізація того ж проходу.

Промпт береться з task.tagger_prompt (адмінка), модель — з --model.
ВИКОНАВЦЯ МІЖ ПЕРІОДАМИ НЕ МІНЯТИ: інакше ламається порівнюваність динаміки.

Приклади:
  python manage.py monitor_tag_api --task fedcrit-sib-dv --limit 300 --dry-run
  python manage.py monitor_tag_api --task fedcrit-sib-dv \
      --model anthropic/claude-haiku-4.5 --date-from 2026-07-01 --date-to 2026-07-31
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from typing import Any, Dict, List

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from analysis.models import AnalysisTask, Post, Tag
from analysis.services import llm
from analysis.services.monitor_stages import sync_comment_event

DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
FED_CRIT_YES = "критика_фед_влади"
DETAIL_CATS = ("criticism_target", "topic")


def _build_user(batch: List[Post]) -> str:
    return "\n\n".join(
        f'[{i}] {(p.text or "").strip()[:900]}' for i, p in enumerate(batch))


def _parse(text: str, n: int) -> Dict[int, Dict[str, Any]]:
    """Витягти {i: verdict} з відповіді (терпить ``` та зайвий текст навколо)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        t = t[4:] if t.startswith("json") else t
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    out = {}
    for it in data.get("items", []):
        i = it.get("i")
        if isinstance(i, int) and 0 <= i < n:
            out[i] = it
    return out


async def _run(batches, model, system, concurrency, max_tokens, log):
    sem = asyncio.Semaphore(concurrency)
    client = llm.make_client()
    results: Dict[int, Dict[str, Any]] = {}
    done = [0]

    async def one(batch):
        async with sem:
            msgs = [{"role": "system", "content": system},
                    {"role": "user", "content": _build_user(batch)}]
            try:
                raw = await llm.query(msgs, model=model, client=client,
                                      max_tokens=max_tokens)
            except Exception as e:  # noqa: BLE001 — один батч не валить прохід
                log(f"  батч впав: {type(e).__name__}: {str(e)[:80]}")
                return
            for i, v in _parse(raw, len(batch)).items():
                results[batch[i].id] = v
            done[0] += 1
            if done[0] % 5 == 0 or done[0] == len(batches):
                log(f"  {done[0]}/{len(batches)} батчів, вердиктів {len(results)}")

    try:
        await asyncio.gather(*(one(b) for b in batches))
    finally:
        await client.close()
    return results


class Command(BaseCommand):
    help = "Тегування monitor-коментарів через OpenRouter (бінарна вісь fed_crit + деталі)."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True)
        parser.add_argument("--model", default=DEFAULT_MODEL)
        parser.add_argument("--batch-size", type=int, default=40)
        parser.add_argument("--concurrency", type=int, default=8)
        parser.add_argument("--max-tokens", type=int, default=4000)
        parser.add_argument("--limit", type=int, default=0, help="0 = усі")
        parser.add_argument("--date-from", default="")
        parser.add_argument("--date-to", default="")
        parser.add_argument("--regions", default="", help="Лише ці регіони (кома)")
        parser.add_argument("--retag", action="store_true",
                            help="Перетегувати вже теговані (інакше пропускаються).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Викликати LLM, показати розкладку, у БД не писати.")

    def handle(self, *args, **o):
        try:
            task = AnalysisTask.objects.get(slug=o["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Задачі {o['task']!r} немає.")
        if not settings.OPENROUTER_API_KEY:
            raise CommandError("OPENROUTER_API_KEY не заданий.")
        system = (task.tagger_prompt or "").strip()
        if not system:
            raise CommandError("У задачі порожній tagger_prompt.")

        qs = Post.objects.filter(task=task).exclude(text="")
        qs = qs.filter(stage__in=[Post.STAGE_MON_PRESCREENED, Post.STAGE_MON_FILTERED])
        # автопересилки постів каналу в групу обговорень — не думки людей:
        # ні в чисельник, ні в знаменник метрики, і платити за них теж не треба
        qs = qs.exclude(is_channel_repost=True)
        if not o["retag"]:
            qs = qs.filter(is_classified=False)
        if o["date_from"]:
            qs = qs.filter(posted_at__date__gte=date.fromisoformat(o["date_from"]))
        if o["date_to"]:
            qs = qs.filter(posted_at__date__lte=date.fromisoformat(o["date_to"]))
        if o["regions"]:
            qs = qs.filter(region_subject__name__in=
                           [r.strip() for r in o["regions"].split(",") if r.strip()])
        posts = list(qs.order_by("posted_at", "id")[:o["limit"]] if o["limit"]
                     else qs.order_by("posted_at", "id"))
        if not posts:
            self.stdout.write("нема чого тегувати")
            return

        bs = o["batch_size"]
        batches = [posts[i:i + bs] for i in range(0, len(posts), bs)]
        self.stdout.write(f"{len(posts)} коментарів, {len(batches)} батчів по {bs}, "
                          f"модель {o['model']}, конкурентність {o['concurrency']}")

        results = asyncio.run(_run(batches, o["model"], system, o["concurrency"],
                                   o["max_tokens"], self.stdout.write))

        n_fed = sum(1 for v in results.values()
                    if str(v.get("fed_crit", "")).strip() == FED_CRIT_YES)
        self.stdout.write(f"\nвердиктів {len(results)}/{len(posts)} | "
                          f"критика фед. влади: {n_fed} "
                          f"({n_fed / max(1, len(results)) * 100:.1f}%)")
        if o["dry_run"]:
            self.stdout.write("dry-run: у БД не пишемо")
            for p in posts[:5]:
                v = results.get(p.id)
                if v:
                    self.stdout.write(f"  [{v.get('fed_crit')}] {v.get('criticism_target')} "
                                      f"{v.get('topic')} | {p.text[:70]}")
            return

        by_post = {p.id: p for p in posts}
        n_saved = n_rel = 0
        for pid, v in results.items():
            post = by_post[pid]
            fed = str(v.get("fed_crit", "")).strip()
            tags = []
            if fed in (FED_CRIT_YES, "інше"):
                t = Tag.objects.filter(name=fed, category="fed_crit").first()
                if t:
                    tags.append(t)
            for cat in DETAIL_CATS:
                for name in (v.get(cat) or []):
                    t = Tag.objects.filter(name=str(name).strip(), category=cat).first()
                    if t:                       # закриті списки: чужого не створюємо
                        tags.append(t)
            with transaction.atomic():
                if tags:
                    post.tags.add(*tags)
                cl = dict(post.classification or {})
                cl["opinion"] = {**v, "_model": o["model"], "_source": "monitor_tag_api"}
                post.classification = cl
                post.is_classified = True
                post.is_relevant = (fed == FED_CRIT_YES)
                post.stage = Post.STAGE_DONE
                post.save(update_fields=["classification", "is_classified",
                                         "is_relevant", "stage"])
                sync_comment_event(post)        # relevant коментар <=> Event 1:1
            n_saved += 1
            n_rel += int(post.is_relevant)
        self.stdout.write(f"збережено {n_saved}, релевантних {n_rel}")
