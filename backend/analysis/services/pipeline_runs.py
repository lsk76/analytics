"""
Ранер monitor-запусків — ГІБРИДНИЙ конвеєр критики в UI.

Життєвий цикл ResearchRun (kind визначається task.pipeline == "monitor"):

  collecting        воркери mon-collect/mon-filter/mon-prescreen розгрібають
                    чанки й пости вікна; ранер лише чекає, поки все розгребено.
  → prepare         ранер сам готує батчі (prescreen-позитивні, ще не теговані)
                    у _dir/runs/run_<id>/ (batch_NNN.json + SYSTEM_PROMPT.md).
  → awaiting_agent  ГІБРИД: тегує НЕ система, а Claude-агенти. Людина каже
                    Claude Code: «протегуй батчі запуску #N» — агент читає
                    SYSTEM_PROMPT.md, пише batch_NNN_done.json.
  → ingest          ранер бачить усі *_done.json → monitor_ingest_tags →
                    events 1:1 (sync_comment_event усередині інжесту) →
                    пости вікна закриваються в done → лічильники → done.

Ідемпотентно і резюмабельно: кожен крок перевіряє фактичний стан БД/файлів.
Реєструється як стадія воркера "mon_runs" (docker: worker-mon-runs).
"""
from __future__ import annotations

import glob
import logging
import os

from django.core.management import call_command
from django.utils import timezone as djtz

from analysis.models import CollectChunk, Post, ResearchRun

log = logging.getLogger(__name__)

RUNS_DIR = "/app/backend/_dir/runs"


def batch_dir(run: ResearchRun) -> str:
    return f"{RUNS_DIR}/run_{run.id}"


def _batches(bdir: str):
    all_json = glob.glob(f"{bdir}/batch_*.json")
    done = [p for p in all_json if p.endswith("_done.json")]
    todo = [p for p in all_json if not p.endswith("_done.json")]
    return todo, done


def mon_runs_once(task) -> bool:
    """Просунути всі активні monitor-запуски задачі на один крок.
    Повертає True, якщо щось змінилось (контракт run_worker)."""
    did = False
    for run in (ResearchRun.objects
                .filter(task=task, status__in=["collecting", "collected", "awaiting_agent"])
                .order_by("id")):
        try:
            did = _advance(run) or did
        except Exception as e:  # noqa: BLE001 — лишити запуск живим, помилку показати
            run.error = repr(e)[:2000]
            run.save(update_fields=["error"])
            log.exception("mon_runs: run #%s advance failed", run.id)
    return did


def _advance(run: ResearchRun) -> bool:
    if run.status in ("collecting", "collected"):
        return _try_prepare(run)
    if run.status == "awaiting_agent":
        return _try_ingest(run)
    return False


def _window_posts(run: ResearchRun):
    return Post.objects.filter(task=run.task,
                               posted_at__date__gte=run.date_from,
                               posted_at__date__lte=run.date_to)


def _try_prepare(run: ResearchRun) -> bool:
    # 1) усі чанки збору закриті?
    unfinished = (CollectChunk.objects.filter(job=run)
                  .exclude(status__in=["done", "split"]).count())
    if unfinished:
        return False
    # 2) mon-failed пости вікна (напр. транзієнтний 401 від OpenRouter на
    #    прескріні) — НЕ «готово»: ре-чергуємо до 3 разів, далі блокуємо запуск
    #    з помилкою. Без цього ранер закривав запуск, "не бачачи" failed.
    failed = _window_posts(run).filter(stage=Post.STAGE_FAILED,
                                       stage_error__startswith="mon_")
    n_failed = failed.count()
    if n_failed:
        stats = dict(run.stats or {})
        retries = stats.get("failed_retries", 0)
        if retries >= 3:
            run.error = (f"{n_failed} постів застрягло у failed після "
                         f"{retries} ре-черг — розберись вручну (stage_error)")
            run.save(update_fields=["error"])
            return False
        failed.filter(stage_error__startswith="mon_prescreen").update(
            stage=Post.STAGE_MON_FILTERED, stage_attempts=0,
            stage_error="", stage_locked_at=None)
        failed.filter(stage_error__startswith="mon_filter").update(
            stage=Post.STAGE_MON_COLLECTED, stage_attempts=0,
            stage_error="", stage_locked_at=None)
        stats["failed_retries"] = retries + 1
        run.stats = stats
        run.save(update_fields=["stats"])
        log.warning("mon_runs: run #%s — %s failed постів повернуто в чергу "
                    "(спроба %s/3)", run.id, n_failed, retries + 1)
        return True
    # 3) фільтр і прескрін розгребли вікно? (їх ведуть окремі воркери)
    backlog = _window_posts(run).filter(
        stage__in=[Post.STAGE_MON_COLLECTED, Post.STAGE_MON_FILTERED]).count()
    if backlog:
        return False
    # 4) готуємо батчі для агентів
    bdir = batch_dir(run)
    os.makedirs(bdir, exist_ok=True)
    call_command("monitor_prepare_batches",
                 task=run.task.slug, out_dir=bdir, require_prescreen=True,
                 date_from=run.date_from.isoformat(),
                 date_to=run.date_to.isoformat())
    todo, done = _batches(bdir)
    stats = dict(run.stats or {})
    stats.update(batch_dir=bdir, batches=len(todo), batches_done=len(done))
    if not todo:                      # нічого тегувати — одразу фініш
        _finish(run, stats)
        return True
    run.stats = stats
    run.status = "awaiting_agent"
    run.save(update_fields=["stats", "status"])
    log.info("mon_runs: run #%s → awaiting_agent (%s батчів у %s)",
             run.id, len(todo), bdir)
    return True


def _try_ingest(run: ResearchRun) -> bool:
    bdir = batch_dir(run)
    todo, done = _batches(bdir)
    stats = dict(run.stats or {})
    if stats.get("batches_done") != len(done):   # тримати лічильник свіжим у UI
        stats["batches_done"] = len(done)
        run.stats = stats
        run.save(update_fields=["stats"])
    if len(done) < len(todo):
        return False
    # всі батчі відтеговані агентами → інжест (events 1:1 створює сам інжест
    # через sync_comment_event) → закрити пости вікна → фініш
    call_command("monitor_ingest_tags", task=run.task.slug, done_dir=bdir)
    # закрити вікно: відтеговані (is_classified) І prescreen-негативні — усі
    # доїхали до термінала; лишати їх на mon_prescreened = вічна «черга» в UI
    (_window_posts(run)
     .filter(stage=Post.STAGE_MON_PRESCREENED)
     .update(stage=Post.STAGE_DONE))
    _finish(run, stats)
    return True


def _finish(run: ResearchRun, stats: dict):
    w = _window_posts(run)
    run.posts_collected = w.count()
    run.posts_relevant = w.filter(is_relevant=True).count()
    run.events_total = w.filter(is_relevant=True, event__isnull=False).count()
    run.stats = stats
    run.status = "done"
    run.finished_at = djtz.now()
    run.save(update_fields=["posts_collected", "posts_relevant", "events_total",
                            "stats", "status", "finished_at"])
    log.info("mon_runs: run #%s DONE (posts=%s relevant=%s events=%s)",
             run.id, run.posts_collected, run.posts_relevant, run.events_total)


# ============================================================================
# EVENTS-запуски: агент-аудит подій (гібрид, симетрично monitor-тегуванню)
#
#   collecting/collected  чанки збору + events-стадії постів вікна (enrich...
#                         dedup) розгрібають воркери; якщо увімкнено —
#                         чекаємо й авто-аудит (перший прохід дешевою LLM).
#   → prepare             батчі approved-подій вікна (без уже агент-ревʼюнутих)
#                         у _dir/runs/run_<id>/ + SYSTEM_PROMPT.md
#                         (= task.agent_review_prompt або EVENT_REVIEW_PROMPT.md).
#   → awaiting_agent      агенти пишуть batch_NNN_done.json (формат §5 промпту:
#                         keep/reject + region_fix + tags_add + reason).
#   → ingest              reject -> rejected + '[agent review] '+reason;
#                         region_fix -> Event.region; tags_add -> теги. -> done.
# ============================================================================

_EV_OPEN_STAGES = [Post.STAGE_COLLECTED, Post.STAGE_ENRICHED,
                   Post.STAGE_PRECLUSTERED, Post.STAGE_CLASSIFIED,
                   Post.STAGE_DEDUPED]
_EV_BATCH_SIZE = 80


def _default_agent_review_prompt() -> str:
    import analysis.pilot as _pilot
    from pathlib import Path
    return (Path(_pilot.__file__).parent / "EVENT_REVIEW_PROMPT.md").read_text()


def _window_events(run):
    from analysis.models import Event
    return Event.objects.filter(task=run.task,
                                event_date__gte=run.date_from,
                                event_date__lte=run.date_to)


def ev_runs_once(task) -> bool:
    """Просунути активні EVENTS-запуски задачі (контракт run_worker)."""
    did = False
    for run in (ResearchRun.objects
                .filter(task=task, status__in=["collecting", "collected", "awaiting_agent"])
                .order_by("id")):
        try:
            if run.status == "awaiting_agent":
                did = _ev_try_ingest(run) or did
            else:
                did = _ev_try_prepare(run) or did
        except Exception as e:  # noqa: BLE001
            run.error = repr(e)[:2000]
            run.save(update_fields=["error"])
            log.exception("ev_runs: run #%s advance failed", run.id)
    return did


def _ev_try_prepare(run) -> bool:
    import json
    from analysis.models import Event
    # 1) збір закритий?
    if CollectChunk.objects.filter(job=run).exclude(status__in=["done", "split"]).exists():
        return False
    # 2) конвеєр постів вікна дожував? (enrich→...→dedup ведуть воркери)
    if _window_posts(run).filter(stage__in=_EV_OPEN_STAGES).exists():
        return False
    # 3) авто-аудит (перший прохід) закінчив? (лише якщо увімкнений)
    if run.task.review_enabled and _window_events(run).filter(
            review_status=Event.REVIEW_PENDING).exists():
        return False
    # 4) батчі approved-подій, ще не бачених агент-аудитом
    evs = (_window_events(run).filter(review_status=Event.REVIEW_APPROVED)
           .exclude(review_notes__icontains="agent review")
           .prefetch_related("tags").order_by("id"))
    items = [{"id": e.id,
              "event_date": e.event_date.isoformat() if e.event_date else None,
              "region": e.region or (e.region_subject.name if e.region_subject_id else ""),
              "summary": e.summary,
              "tags": [{"category": t.category, "name": t.name} for t in e.tags.all()]}
             for e in evs]
    bdir = batch_dir(run)
    os.makedirs(bdir, exist_ok=True)
    prompt = run.task.agent_review_prompt or _default_agent_review_prompt()
    with open(f"{bdir}/SYSTEM_PROMPT.md", "w") as f:
        f.write(prompt)
    n_batches = 0
    for i in range(0, len(items), _EV_BATCH_SIZE):
        n_batches += 1
        with open(f"{bdir}/batch_{n_batches:03d}.json", "w") as f:
            json.dump({"meta": {"task_slug": run.task.slug, "batch_id": n_batches,
                                "kind": "event_review"},
                       "items": items[i:i + _EV_BATCH_SIZE]}, f, ensure_ascii=False)
    stats = dict(run.stats or {})
    stats.update(batch_dir=bdir, batches=n_batches, batches_done=0,
                 kind="event_review", events_to_review=len(items))
    if not n_batches:
        _ev_finish(run, stats)
        return True
    run.stats = stats
    run.status = "awaiting_agent"
    run.save(update_fields=["stats", "status"])
    log.info("ev_runs: run #%s → awaiting_agent (%s подій у %s батчах)",
             run.id, len(items), n_batches)
    return True


def _ev_try_ingest(run) -> bool:
    import json
    from analysis.models import Event, Tag
    bdir = batch_dir(run)
    todo, done = _batches(bdir)
    stats = dict(run.stats or {})
    if stats.get("batches_done") != len(done):
        stats["batches_done"] = len(done)
        run.stats = stats
        run.save(update_fields=["stats"])
    if len(done) < len(todo):
        return False
    n_keep = n_reject = n_fix = 0
    for f in sorted(done):
        data = json.load(open(f))
        for it in (data.get("items") or []):
            ev = Event.objects.filter(id=it.get("id"), task=run.task).first()
            if not ev:
                continue
            verdict = (it.get("verdict") or "").lower()
            reason = (it.get("reason") or "")[:200]
            if verdict == "reject":
                ev.review_status = Event.REVIEW_REJECTED
                note = f"[agent review] {reason or 'відхилено агент-аудитом'}"
                ev.review_notes = (ev.review_notes + " | " + note) if ev.review_notes else note
                ev.reviewed_at = djtz.now()
                ev.save(update_fields=["review_status", "review_notes", "reviewed_at"])
                n_reject += 1
                continue
            # keep: можливі виправлення
            upd = []
            if it.get("region_fix") is not None:
                ev.region = it["region_fix"] or ""
                upd.append("region")
            note = "[agent review] ok" + (f": {reason}" if reason else "")
            ev.review_notes = (ev.review_notes + " | " + note) if ev.review_notes else note
            ev.reviewed_at = djtz.now()
            upd += ["review_notes", "reviewed_at"]
            ev.save(update_fields=upd)
            for pair in (it.get("tags_add") or []):
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    tg, _ = Tag.objects.get_or_create(category=pair[0], name=pair[1])
                    ev.tags.add(tg)
                    n_fix += 1
            n_keep += 1
    stats.update(agent_keep=n_keep, agent_reject=n_reject, agent_tag_fixes=n_fix)
    _ev_finish(run, stats)
    return True


def _ev_finish(run, stats: dict):
    from analysis.models import Event
    W = _window_posts(run)
    run.posts_collected = W.count()
    run.posts_relevant = W.filter(is_relevant=True).count()
    evq = _window_events(run)
    run.events_total = evq.filter(review_status=Event.REVIEW_APPROVED).count()
    run.stats = stats
    run.status = "done"
    run.finished_at = djtz.now()
    run.save(update_fields=["posts_collected", "posts_relevant", "events_total",
                            "stats", "status", "finished_at"])
    log.info("ev_runs: run #%s DONE (approved=%s, agent: keep=%s reject=%s)",
             run.id, run.events_total, stats.get("agent_keep"), stats.get("agent_reject"))
