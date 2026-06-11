"""
Pre-screen monitor Posts via direct OpenRouter API (Variant B).

Same job as `monitor_prescreen`, but skips the file-based agent dance:
  read posts → batched async LLM calls → write _prescreen.could_be_criticism
into Post.classification.

Why: agent-based prescreen burns 5h-window tokens on Max-plan and parallelises
to ~10 agents. Direct API has no plan limit (just per-call cost), no file IO,
and scales by concurrency parameter (default 8).

Usage:
  python manage.py monitor_prescreen_api --task dagestan-criticism-monitor
  python manage.py monitor_prescreen_api --task dagestan-criticism-monitor \\
      --batch-size 200 --concurrency 8 --model anthropic/claude-haiku-4.5
  python manage.py monitor_prescreen_api --task dagestan-criticism-monitor \\
      --reset    # wipe previous _prescreen before running
  python manage.py monitor_prescreen_api --task dagestan-criticism-monitor \\
      --date-from 2026-01-01 --date-to 2026-01-31    # one-month chunk
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from analysis.models import AnalysisTask, Post
from analysis.pilot.logging import open_log
from analysis.pilot.prompts import (
    PRESCREEN_SYSTEM_PROMPT, PRESCREEN_SYSTEM_PROMPT_COMPACT,
)
from analysis.services import llm


def _build_user(batch: List[Dict]) -> str:
    lines = []
    for i, p in enumerate(batch):
        txt = (p.get("text") or "").strip().replace("\n", " ")[:1000]
        lines.append(f"[{i}] {txt}")
    return "\n\n".join(lines)


def _parse_object(text: str) -> Any:
    """Parse a JSON OBJECT from a model reply (tolerates ``` fences).

    Unlike llm.extract_json (which prefers the first `[...]`), this grabs the
    outermost `{...}` so payloads like {"positive": []} survive."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b != -1 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except Exception:  # noqa: BLE001
            return None
    return None


async def _classify_batch(client, model: str, batch: List[Dict],
                          max_tokens: int, compact: bool) -> List[Dict[str, Any]]:
    system = PRESCREEN_SYSTEM_PROMPT_COMPACT if compact else PRESCREEN_SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _build_user(batch)},
    ]
    raw = await llm.query(messages, model=model, client=client,
                          max_tokens=max_tokens, json_mode=True)

    if compact:
        # {"positive": [{"i": 12, "c": 0.8}, ...]} → expand to per-item verdicts.
        # NB: don't use llm.extract_json here — it greedily grabs the FIRST
        # `[...]`, so {"positive": []} yields [] not the dict. json_mode=True
        # guarantees a JSON object, so parse it directly (strip ``` fences).
        data = _parse_object(raw)
        if not isinstance(data, dict) or "positive" not in data:
            return []
        pos: Dict[int, float] = {}
        for e in (data.get("positive") or []):
            if isinstance(e, dict) and "i" in e:
                try:
                    pos[int(e["i"])] = float(e.get("c") or 0.0)
                except (TypeError, ValueError):
                    continue
        return [{"could_be_criticism": i in pos, "confidence": pos.get(i, 0.0)}
                for i in range(len(batch))]

    data = llm.extract_json(raw)
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        return []
    return data


async def _run(posts: List[Dict], model: str, batch_size: int,
               concurrency: int, max_tokens: int, compact: bool,
               log) -> Dict[int, Dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)
    client = llm.make_client()
    results: Dict[int, Dict[str, Any]] = {}
    done_batches = [0]
    n_batches = (len(posts) + batch_size - 1) // batch_size

    async def one(start: int):
        async with sem:
            batch = posts[start:start + batch_size]
            verdicts = await _classify_batch(client, model, batch, max_tokens,
                                             compact)
            for j, p in enumerate(batch):
                if j < len(verdicts) and isinstance(verdicts[j], dict):
                    results[p["id"]] = verdicts[j]
            done_batches[0] += 1
            if done_batches[0] % 5 == 0 or done_batches[0] == n_batches:
                log(f"  batch {done_batches[0]}/{n_batches} done "
                    f"(got verdicts for {len(results)} posts)")

    try:
        tasks = [one(i) for i in range(0, len(posts), batch_size)]
        await asyncio.gather(*tasks)
    finally:
        await client.close()
    return results


class Command(BaseCommand):
    help = ("Pre-screen monitor posts via direct OpenRouter API "
            "(replaces agent-based monitor_prescreen for scale).")

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True)
        parser.add_argument("--model",
                            default=settings.LLM_MODEL,
                            help=f"OpenRouter model id (default {settings.LLM_MODEL}).")
        parser.add_argument("--batch-size", type=int, default=50,
                            help="Comments per LLM call (default 50; 200 truncates on Gemini Flash).")
        parser.add_argument("--concurrency", type=int, default=12,
                            help="Parallel in-flight LLM calls (default 12).")
        parser.add_argument("--max-tokens", type=int, default=2500,
                            help="LLM output cap. 50 items × ~15 tok each + JSON overhead ≈ 1.5k.")
        parser.add_argument("--limit", type=int, default=0,
                            help="0 = all; else first N (chronological).")
        parser.add_argument("--reset", action="store_true",
                            help="Wipe previous _prescreen before running.")
        parser.add_argument("--date-from", default="",
                            help="YYYY-MM-DD; restrict by posted_at.")
        parser.add_argument("--date-to", default="",
                            help="YYYY-MM-DD; restrict by posted_at.")
        parser.add_argument("--compact", action="store_true", default=True,
                            help="Компактний output (ДЕФОЛТ): модель повертає лише "
                                 "індекси позитивів (~40× менше output-токенів, "
                                 "та й recall вищий — 85%% vs 46%% на тесті 01-01).")
        parser.add_argument("--full", dest="compact", action="store_false",
                            help="Повний output: вердикт на кожен пост (дорожче, "
                                 "гірший recall). Лишено для діагностики.")
        parser.add_argument("--store-key", default="_prescreen",
                            help="Ключ у Post.classification (default _prescreen). "
                                 "Окремий ключ = A/B-порівняння без затирання.")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=opts["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Task {opts['task']!r} not found.")

        if not settings.OPENROUTER_API_KEY:
            raise CommandError("OPENROUTER_API_KEY is not set.")

        store_key = opts["store_key"]
        log, log_path = open_log("monitor_prescreen_api", task.slug)
        log(f"log: {log_path}")
        log(f"model={opts['model']} | batch_size={opts['batch_size']} | "
            f"concurrency={opts['concurrency']} | compact={opts['compact']} | "
            f"store_key={store_key}")

        qs = (Post.objects.filter(task=task)
              .exclude(text="")
              .exclude(classification__is_filtered=True))
        if opts["date_from"]:
            qs = qs.filter(posted_at__date__gte=date.fromisoformat(opts["date_from"]))
        if opts["date_to"]:
            qs = qs.filter(posted_at__date__lte=date.fromisoformat(opts["date_to"]))

        if opts["reset"]:
            # Clear store_key on the filtered window.
            n_cleared = 0
            for p in qs.only("id", "classification").iterator(chunk_size=500):
                cl = dict(p.classification or {})
                if cl.pop(store_key, None) is not None:
                    p.classification = cl
                    p.save(update_fields=["classification"])
                    n_cleared += 1
            log(f"reset: cleared {store_key} on {n_cleared} posts")
        else:
            qs = qs.exclude(classification__has_key=store_key)

        qs = qs.order_by("posted_at", "id").only("id", "text")
        if opts["limit"]:
            qs = qs[:opts["limit"]]

        posts = list(qs.values("id", "text"))
        total = len(posts)
        log(f"posts to prescreen: {total}")
        if total == 0:
            log("nothing to do")
            return

        t0 = datetime.now(timezone.utc)
        verdicts = asyncio.run(_run(
            posts, model=opts["model"],
            batch_size=max(1, opts["batch_size"]),
            concurrency=max(1, opts["concurrency"]),
            max_tokens=opts["max_tokens"],
            compact=opts["compact"],
            log=log,
        ))
        wall = (datetime.now(timezone.utc) - t0).total_seconds()
        log(f"got verdicts for {len(verdicts)}/{total} | wall={wall:.0f}s")

        # Write into Post.classification._prescreen
        n_written = n_pos = 0
        for p in posts:
            v = verdicts.get(p["id"])
            if not v:
                continue
            post = Post.objects.get(id=p["id"])
            cl = dict(post.classification or {})
            verdict = bool(v.get("could_be_criticism"))
            cl[store_key] = {
                "could_be_criticism": verdict,
                "confidence": float(v.get("confidence") or 0.0),
                "_model": opts["model"],
                "_mode": "compact" if opts["compact"] else "full",
            }
            post.classification = cl
            post.save(update_fields=["classification"])
            n_written += 1
            if verdict:
                n_pos += 1

        log(f"DONE: written={n_written} | could_be_criticism={n_pos} "
            f"({n_pos/max(n_written,1)*100:.1f}%) | "
            f"missing={total-n_written}")
