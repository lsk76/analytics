"""
LLM-classify comments from `pilot-fed-criticism-comments` task.

Writes the verdict into `Post.classification.opinion = {...}` without touching
any other field. Default model is a FREE OpenRouter model — pass --model to
override (e.g. `--model anthropic/claude-haiku-4.5` for the paid run).

Usage:
  python manage.py pilot_classify_opinion --cycle 4
  python manage.py pilot_classify_opinion --cycle 4 --limit 60   # subsample
  python manage.py pilot_classify_opinion --cycle 4 --model deepseek/deepseek-chat:free

Outputs (in --report-dir):
  cycleNN_llm_<modelslug>.json   — per-post verdicts + tally
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from analysis.models import AnalysisTask, Post
from analysis.services import llm

PILOT_SLUG = "pilot-fed-criticism-comments"

SYSTEM_PROMPT = """Ты — аналитик политического контента из Telegram-комментариев.
Для каждого комментария верни СТРОГИЙ JSON с полями:
  - criticizes_federal: bool — критикует ли он федеральную власть РФ
    (Путин, Кремль, Дума, правительство, министерства, силовые ведомства,
    «Москва» как символ центра). Поддержка/защита власти = false.
  - stance: "negative" | "neutral" | "positive_to_authority"
  - policy_topic: одно из {"war","mobilization","economy","corruption",
                           "repression","social","regional","other",null}
    null если коммент не про конкретную тему политики.
  - relates_to_regional_decision: bool — критикует ли решение центра,
    касающееся конкретного региона (бюджет, мобилизация в регионе,
    языковая политика, назначение главы, и т.д.)
  - is_sarcasm: bool — это сарказм/ирония
  - is_propaganda: bool — это явно про-кремлёвская пропаганда
  - confidence: 0.0-1.0

Ответ — ОДИН JSON-объект {"items": [<verdict>, ...]} с массивом длиной N
по числу комментариев на входе, в том же порядке. Никакого текста вне JSON."""


def _build_user(batch: List[Dict]) -> str:
    lines = []
    for i, p in enumerate(batch):
        # Truncate to keep token use bounded; comments rarely add signal past 800 chars.
        txt = (p.get("text") or "").strip().replace("\n", " ")[:800]
        lines.append(f"[{i}] {txt}")
    return "\n".join(lines)


async def _classify_batch(client, model: str, batch: List[Dict]) -> List[Dict[str, Any]]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user(batch)},
    ]
    raw = await llm.query(messages, model=model, client=client,
                          max_tokens=2000, json_mode=True)
    data = llm.extract_json(raw)
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        return []
    return data


async def _run(posts: List[Dict], model: str, batch_size: int, concurrency: int):
    sem = asyncio.Semaphore(concurrency)
    client = llm.make_client()
    results: Dict[int, Dict[str, Any]] = {}

    async def one(start: int):
        async with sem:
            batch = posts[start:start + batch_size]
            verdicts = await _classify_batch(client, model, batch)
            for j, p in enumerate(batch):
                if j < len(verdicts) and isinstance(verdicts[j], dict):
                    results[p["id"]] = verdicts[j]

    try:
        tasks = [one(i) for i in range(0, len(posts), batch_size)]
        await asyncio.gather(*tasks)
    finally:
        await client.close()
    return results


def _slugify_model(m: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", m.lower()).strip("_")


class Command(BaseCommand):
    help = "LLM-classify pilot comments and write verdict into Post.classification.opinion."

    def add_arguments(self, parser):
        parser.add_argument("--cycle", type=int, required=True)
        parser.add_argument("--model",
                            default="meta-llama/llama-3.3-70b-instruct:free",
                            help="OpenRouter model id. Default = free.")
        parser.add_argument("--limit", type=int, default=0,
                            help="0 = all comments of the cycle, else first N (random order)")
        parser.add_argument("--batch-size", type=int, default=5)
        parser.add_argument("--concurrency", type=int, default=3,
                            help="Free OpenRouter tier is rate-limited; keep low.")
        parser.add_argument("--report-dir",
                            default="/app/backend/_pilot_fed_criticism")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=PILOT_SLUG)
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"task {PILOT_SLUG!r} not found — run pilot_collect_comments first")
        cycle = opts["cycle"]
        model = opts["model"]
        report_dir = Path(opts["report_dir"])
        report_dir.mkdir(parents=True, exist_ok=True)

        qs = (Post.objects
              .filter(task=task,
                      channel__is_channel=False,
                      classification__contains={"_pilot_cycle": cycle})
              .exclude(text="")
              .order_by("?"))   # randomize
        if opts["limit"]:
            qs = qs[:opts["limit"]]

        posts = list(qs.values("id", "text", "channel__username",
                               "channel__title", "channel__inferred_region"))
        if not posts:
            self.stdout.write(self.style.WARNING(
                f"no comments for cycle {cycle} — nothing to classify"))
            return

        self.stdout.write(self.style.HTTP_INFO(
            f"== Classify cycle {cycle}: {len(posts)} comments | model {model} =="))

        verdicts = asyncio.run(_run(
            posts, model,
            batch_size=opts["batch_size"],
            concurrency=opts["concurrency"],
        ))
        self.stdout.write(f"  got verdicts for {len(verdicts)} / {len(posts)}")

        # write to DB
        n_written = 0
        for p in posts:
            v = verdicts.get(p["id"])
            if not v:
                continue
            post = Post.objects.get(id=p["id"])
            post.classification = {
                **(post.classification or {}),
                "opinion": {**v, "_model": model},
            }
            post.save(update_fields=["classification"])
            n_written += 1
        self.stdout.write(f"  wrote {n_written} verdicts to DB")

        # tally
        tally = {"total": len(verdicts), "criticizes_federal": 0, "stance_negative": 0,
                 "stance_neutral": 0, "stance_positive": 0,
                 "regional_decision": 0, "sarcasm": 0, "propaganda": 0,
                 "by_policy_topic": {}, "high_confidence": 0}
        for v in verdicts.values():
            if v.get("criticizes_federal"): tally["criticizes_federal"] += 1
            s = v.get("stance")
            if s == "negative":           tally["stance_negative"] += 1
            elif s == "neutral":          tally["stance_neutral"] += 1
            elif s == "positive_to_authority": tally["stance_positive"] += 1
            if v.get("relates_to_regional_decision"): tally["regional_decision"] += 1
            if v.get("is_sarcasm"):       tally["sarcasm"] += 1
            if v.get("is_propaganda"):    tally["propaganda"] += 1
            topic = v.get("policy_topic") or "null"
            tally["by_policy_topic"][topic] = tally["by_policy_topic"].get(topic, 0) + 1
            if (v.get("confidence") or 0) >= 0.7:  tally["high_confidence"] += 1

        slug = _slugify_model(model)
        out = {
            "cycle": cycle, "model": model,
            "input_posts": len(posts),
            "got_verdicts": len(verdicts),
            "tally": tally,
            "verdicts": [
                {"post_id": p["id"],
                 "chat": p["channel__username"], "title": p["channel__title"],
                 "region": p["channel__inferred_region"],
                 "text_head": (p["text"] or "").replace("\n"," ")[:160],
                 "verdict": verdicts.get(p["id"])}
                for p in posts
            ],
        }
        outf = report_dir / f"cycle{cycle:02d}_llm_{slug}.json"
        outf.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        self.stdout.write(f"wrote {outf}")
        self.stdout.write(self.style.SUCCESS(
            f"criticizes_federal {tally['criticizes_federal']}/{tally['total']} "
            f"({tally['criticizes_federal']/max(tally['total'],1)*100:.1f}%) | "
            f"hi-conf {tally['high_confidence']} | "
            f"sarcasm {tally['sarcasm']} | propaganda {tally['propaganda']} | "
            f"regional {tally['regional_decision']}"
        ))
        # by topic
        for topic, n in sorted(tally["by_policy_topic"].items(), key=lambda x: -x[1]):
            self.stdout.write(f"  topic {topic:>14s}: {n}")
