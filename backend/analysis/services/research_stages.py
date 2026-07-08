"""
Конвеєр «Тематичне дослідження» (pipeline="research") — формалізація разових
досліджень задачі 6 (етнічна напруга C2-C4, економіка E1-E4, політика P1-P3).

Історична схема (docs/ethnic-events-pipeline.md, docs/econ-events-pipeline.md):
  локальні канали республік ('*', unique, по одному каналу) → keyword-AND-фільтр
  за рубриками → агентна класифікація (гео=республіка + справжня подія) →
  групування дублів у інциденти → Event з тегом рубрики.

Тут те саме, але постійним конвеєром:
  res_collect  = mon_collect (той самий код: whitelist MonitorChat задачі,
                 '*' unique по каналу; стадії постів переюзаємо mon_*)
  res_filter   : mon_collected -> mon_prescreened (кандидат: довжина >=40 і
                 БУДЬ-ЯКА активна рубрика спрацювала; ключі рубрик у
                 classification._rubrics) | done (не кандидат)
  res_runs     : ранер запусків — готує пачки кандидатів для агентів
                 («чекає агента»), після *_done.json групує підтверджені у
                 події (рубрика+регіон+дата±3д+схожість підсумку) і фінішує.

Граблі, перенесені з доків: гео-правило в промпті обов'язкове; C4-пастка
держпропаганди; AND-фільтр (OR дає шум ×30); URL TeleZip ненадійний — аудит
лише по тексту.
"""
from __future__ import annotations

import json
import logging
import os
import re

from django.utils import timezone as djtz
from rapidfuzz import fuzz

from analysis.models import CollectChunk, Post, ResearchRun

from .monitor_stages import _claim  # клейм-механіка постів (як у monitor)
from . import pipeline_runs as PR

log = logging.getLogger(__name__)

RES_MIN_LEN = 40          # історичний фільтр довжини (collect_econ_multi.py)
BATCH_SIZE = 50           # історичний розмір пачки класифікації
GROUP_DAYS = 3            # вікно склейки дублів одного інциденту
GROUP_FUZZ = 70           # поріг схожості підсумків (token_set_ratio)


# --------------------------------------------------------------------------- фільтр за рубриками

def _compile_rubrics(task):
    """[(key, [regex, regex, ...])] — ІСТОРИЧНА семантика (ТЕМА ∧ ДІЯ):
    keywords рубрики = список РЕГУЛЯРОК; кандидат, якщо КОЖНА збіглась
    (регістронезалежно). Бита регулярка — рубрика пропускається з логом."""
    out = []
    for r in task.rubrics.filter(is_active=True).order_by("order", "key"):
        pats = []
        try:
            for pat in (r.keywords or []):
                pat = str(pat).strip()
                if pat:
                    pats.append(re.compile(pat, re.I))
        except re.error as e:
            log.error("res_filter: рубрика %s — бита регулярка: %s", r.key, e)
            continue
        if pats:
            out.append((r.key, pats))
    return out


def _match_rubrics(text: str, compiled) -> list[str]:
    return [key for key, pats in compiled
            if all(p.search(text) for p in pats)]


def res_filter_once(task):
    """Кандидати за рубриками: kept -> mon_prescreened (+_rubrics), шум -> done."""
    ids = _claim(task, Post.STAGE_MON_COLLECTED, 1000)
    if not ids:
        return False
    compiled = _compile_rubrics(task)
    posts = list(Post.objects.filter(id__in=ids))
    n_kept = n_drop = 0
    for p in posts:
        text = (p.text or "").strip()
        keys = _match_rubrics(text, compiled) if len(text) >= RES_MIN_LEN else []
        cl = dict(p.classification or {})
        if keys:
            cl["_rubrics"] = keys
            p.stage = Post.STAGE_MON_PRESCREENED
            n_kept += 1
        else:
            cl["is_filtered"] = True
            cl["exclusion_label"] = "no_rubric"
            p.is_relevant = False
            p.stage = Post.STAGE_DONE
            n_drop += 1
        p.classification = cl
        p.stage_locked_at = None
    Post.objects.bulk_update(posts, ["classification", "stage", "stage_locked_at",
                                     "is_relevant"], batch_size=500)
    log.info("res_filter[%s]: kept=%s drop=%s", task.slug, n_kept, n_drop)
    return True


# --------------------------------------------------------------------------- ранер запусків

def res_runs_once(task) -> bool:
    did = False
    for run in (ResearchRun.objects
                .filter(task=task, status__in=["collecting", "collected", "awaiting_agent"])
                .order_by("id")):
        try:
            if run.status == "awaiting_agent":
                did = _try_ingest(run) or did
            else:
                did = _try_prepare(run) or did
        except Exception as e:  # noqa: BLE001
            run.error = repr(e)[:2000]
            run.save(update_fields=["error"])
            log.exception("res_runs: run #%s advance failed", run.id)
    return did


def _window_posts(run):
    return Post.objects.filter(task=run.task,
                               posted_at__date__gte=run.date_from,
                               posted_at__date__lte=run.date_to)


def _try_prepare(run) -> bool:
    if CollectChunk.objects.filter(job=run).exclude(status__in=["done", "split"]).exists():
        return False
    if _window_posts(run).filter(stage=Post.STAGE_MON_COLLECTED).exists():
        return False   # фільтр ще працює
    cands = list(_window_posts(run)
                 .filter(stage=Post.STAGE_MON_PRESCREENED, is_classified=False)
                 .select_related("channel", "region_subject")
                 .order_by("posted_at", "id"))
    bdir = PR.batch_dir(run)
    os.makedirs(bdir, exist_ok=True)
    with open(f"{bdir}/SYSTEM_PROMPT.md", "w") as f:
        f.write(_agent_prompt(run.task))
    n_b = 0
    for i in range(0, len(cands), BATCH_SIZE):
        n_b += 1
        items = [{"id": p.id,
                  "chat": p.channel_name,
                  "region": p.region_subject.name if p.region_subject_id else "",
                  "rubrics_hint": (p.classification or {}).get("_rubrics", []),
                  "date": p.posted_at.date().isoformat() if p.posted_at else None,
                  "text": (p.text or "")[:1500]}
                 for p in cands[i:i + BATCH_SIZE]]
        with open(f"{bdir}/batch_{n_b:03d}.json", "w") as f:
            json.dump({"meta": {"task_slug": run.task.slug, "batch_id": n_b,
                                "kind": "research"},
                       "items": items}, f, ensure_ascii=False)
    stats = dict(run.stats or {})
    stats.update(batch_dir=bdir, batches=n_b, batches_done=0, kind="research",
                 candidates=len(cands))
    if not n_b:
        _finish(run, stats)
        return True
    run.stats = stats
    run.status = "awaiting_agent"
    run.save(update_fields=["stats", "status"])
    log.info("res_runs: run #%s → awaiting_agent (%s кандидатів, %s пачок)",
             run.id, len(cands), n_b)
    return True


def _agent_prompt(task) -> str:
    """Промпт агента: база з задачі (tagger_prompt) + правила рубрик."""
    parts = [task.tagger_prompt or ""]
    parts.append("\nRUBRICS (use these exact keys in the verdict):")
    for r in task.rubrics.filter(is_active=True).order_by("order", "key"):
        parts.append(f"- {r.key} — {r.name}")
        if r.extra_prompt:
            parts.append(f"  {r.extra_prompt.strip()}")
    return "\n".join(parts)


def _try_ingest(run) -> bool:
    from analysis.models import Event, Tag
    bdir = PR.batch_dir(run)
    todo, done = PR._batches(bdir)
    stats = dict(run.stats or {})
    if stats.get("batches_done") != len(done):
        stats["batches_done"] = len(done)
        run.stats = stats
        run.save(update_fields=["stats"])
    if len(done) < len(todo):
        return False

    # 1) зібрати вердикти
    confirmed = []           # (post, rubrics, summary)
    post_ids = set()
    for f in sorted(done):
        data = json.load(open(f))
        for it in (data.get("items") or []):
            pid = it.get("id")
            v = it.get("verdict") or {}
            rubs = [k for k in (v.get("rubrics") or []) if k]
            if pid:
                post_ids.add(pid)
            if rubs:
                confirmed.append((pid, rubs, (v.get("summary") or "")[:500]))

    posts = {p.id: p for p in
             Post.objects.filter(id__in=[c[0] for c in confirmed])
             .select_related("channel", "region_subject")}

    # 2) групування в інциденти: рубрика+регіон, дата±GROUP_DAYS, схожий підсумок
    rubric_rows = {r.key: r for r in run.task.rubrics.all()}
    groups = []   # {rubric, region_id, date, summary, members:[(post, summary)]}
    for pid, rubs, summary in sorted(
            confirmed, key=lambda c: (posts[c[0]].posted_at if c[0] in posts and posts[c[0]].posted_at else djtz.now())):
        p = posts.get(pid)
        if not p:
            continue
        d = p.posted_at.date() if p.posted_at else None
        rub = rubs[0]
        placed = False
        for g in groups:
            if g["rubric"] != rub or g["region_id"] != p.region_subject_id:
                continue
            if d and g["date"] and abs((d - g["date"]).days) > GROUP_DAYS:
                continue
            if fuzz.token_set_ratio(summary, g["summary"]) >= GROUP_FUZZ:
                g["members"].append((p, summary))
                placed = True
                break
        if not placed:
            groups.append({"rubric": rub, "region_id": p.region_subject_id,
                           "date": d, "summary": summary, "members": [(p, summary)]})

    # 3) події
    n_ev = 0
    for g in groups:
        members = g["members"]
        ps = [m[0] for m in members]
        rub = rubric_rows.get(g["rubric"])
        ev = Event.objects.create(
            task=run.task,
            event_date=min((p.posted_at.date() for p in ps if p.posted_at), default=g["date"]),
            region=(ps[0].region_subject.name if ps[0].region_subject_id else ""),
            region_subject_id=ps[0].region_subject_id,
            summary=g["summary"] or (ps[0].text or "")[:300],
            post_count=len(ps),
            channel_count=len({p.channel_id for p in ps if p.channel_id}),
            reach=max(((p.channel.subscribers if p.channel_id and p.channel else 0) or 0)
                      for p in ps),
            review_status=Event.REVIEW_APPROVED,
        )
        if rub:
            tag, _ = Tag.objects.get_or_create(category=rub.tag_category, name=rub.tag_name)
            ev.tags.add(tag)
        for p in ps:
            p.event = ev
            p.is_relevant = True
        n_ev += 1

    # 4) закрити пости вікна
    all_posts = list(Post.objects.filter(id__in=post_ids))
    conf_ids = {c[0] for c in confirmed}
    for p in all_posts:
        p.is_classified = True
        if p.id not in conf_ids:
            p.is_relevant = False
        p.stage = Post.STAGE_DONE
    Post.objects.bulk_update(all_posts, ["is_classified", "is_relevant", "stage"],
                             batch_size=500)
    Post.objects.bulk_update([p for g in groups for p, _ in g["members"]],
                             ["event", "is_relevant"], batch_size=500)
    stats.update(confirmed=len(confirmed), incidents=n_ev)
    _finish(run, stats)
    return True


def _finish(run, stats):
    from analysis.models import Event
    w = _window_posts(run)
    run.posts_collected = w.count()
    run.posts_relevant = w.filter(is_relevant=True).count()
    run.events_total = Event.objects.filter(
        task=run.task, event_date__gte=run.date_from,
        event_date__lte=run.date_to).count()
    run.stats = stats
    run.status = "done"
    run.finished_at = djtz.now()
    run.save(update_fields=["posts_collected", "posts_relevant", "events_total",
                            "stats", "status", "finished_at"])
    log.info("res_runs: run #%s DONE (кандидатів=%s подій=%s)",
             run.id, stats.get("candidates"), stats.get("incidents"))
