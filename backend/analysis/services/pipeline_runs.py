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
