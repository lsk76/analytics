"""mon_sample — стадія run_worker: черга завдань на ВИБІРКОВИЙ збір коментарів.

Навіщо стадія, а не прямий запуск: `monitor_sample_collect` читає Telegram
сотнями запитів із паузами (для 8 республік × місяць — десятки хвилин), тож ні
HTTP-запит адмінки, ні виклик MCP такого не витримають. Тому замовник (MCP
`sample_collect`, адмінка) лише створює `MonitorSampleJob`, а робота йде тут —
той самий claim-патерн, що й у WarmUpJob/TestBotJob.

    python manage.py run_worker --stage mon_sample

Прогрес видно НЕ з БД, а з файлу `_dir/samples/job_<id>.log`: команда пише
stdout з асинхронної гілки, де ORM недоступний (SynchronousOnlyOperation), тож
живий вивід іде у файл (том `./backend` спільний для всіх контейнерів), а в
`MonitorSampleJob.log` він переноситься після завершення.
"""
import logging
import os
from datetime import timedelta

from django.core.management import call_command
from django.db import transaction
from django.db.models import Q
from django.utils import timezone as djtz

from analysis.models import AnalysisTask, MonitorSampleJob

log = logging.getLogger(__name__)

SAMPLES_DIR = "/app/backend/_dir/samples"
# Збір місяця по 8 республіках — години: перехоплювати «завислий» claim раніше
# означало б запустити другий збір поверх першого (подвійна вибірка у вікні).
LOCK_TIMEOUT = timedelta(hours=12)
LOG_TAIL = 20000


def log_path(job) -> str:
    return os.path.join(SAMPLES_DIR, f"job_{job.id}.log")


def log_tail(job, limit: int = 4000) -> str:
    """Живий вивід завдання: файл, поки воно біжить, далі — збережений у БД."""
    path = log_path(job)
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[-limit:]
    return (job.log or "")[-limit:]


def _claim(task):
    now = djtz.now()
    cutoff = now - LOCK_TIMEOUT
    with transaction.atomic():
        job = (MonitorSampleJob.objects
               .filter(task=task)
               .filter(Q(status="pending") | Q(status="running", locked_at__lt=cutoff))
               .select_for_update(skip_locked=True)
               .order_by("created_at")
               .first())
        if job:
            job.status = "running"
            job.locked_at = now
            job.attempts += 1
            job.started_at = job.started_at or now
            job.save(update_fields=["status", "locked_at", "attempts", "started_at"])
    return job


def _argv(job) -> dict:
    opts = dict(task=job.task.slug,
                date_from=job.date_from.isoformat(),
                date_to=job.date_to.isoformat())
    if job.mode == MonitorSampleJob.MODE_PROBE:
        opts["probe"] = job.probe_ids or 200
    else:
        opts["per_region"] = job.per_region or 1500
    if job.mode == MonitorSampleJob.MODE_DRY_RUN:
        opts["dry_run"] = True
    if job.seed:
        opts["seed"] = job.seed
    if job.regions.strip():
        opts["regions"] = job.regions.strip()
    if job.resume:
        opts["resume"] = True
    return opts


def mon_sample_once(task) -> bool:
    """Один прохід: забрати завдання вибірки цієї задачі й виконати його.

    Контракт run_worker: True — щось зробили (є сенс шукати наступне завдання).
    """
    if task.mon_collect_source != AnalysisTask.MON_SRC_TG_SAMPLE:
        return False           # задача збирається TeleZip-потоком — не наша черга
    job = _claim(task)
    if not job:
        return False
    os.makedirs(SAMPLES_DIR, exist_ok=True)
    path = log_path(job)
    log.info("mon_sample: завдання #%s %s %s…%s (%s)", job.id, task.slug,
             job.date_from, job.date_to, job.mode)
    try:
        # buffering=1 — рядковий буфер: вивід видно в файлі одразу, а не в кінці
        with open(path, "a", encoding="utf-8", buffering=1) as f:
            f.write(f"=== {djtz.localtime():%Y-%m-%d %H:%M} спроба {job.attempts}: "
                    f"{job.mode} {job.date_from}…{job.date_to}\n")
            try:
                call_command("monitor_sample_collect", stdout=f, stderr=f, **_argv(job))
            except Exception as e:  # noqa: BLE001 — помилка команди = провал завдання
                f.write(f"\n!!! {e!r}\n")
                job.status, job.error = "failed", repr(e)[:2000]
                log.exception("mon_sample: завдання #%s провалилось", job.id)
            else:
                job.status, job.error = "done", ""
    finally:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                job.log = f.read()[-LOG_TAIL:]
        except OSError:
            pass
        job.finished_at = djtz.now()
        MonitorSampleJob.objects.filter(pk=job.pk, status="running").update(
            status=job.status, error=job.error, log=job.log, finished_at=job.finished_at)
    return True


STAGE_RUNNERS = {"mon_sample": mon_sample_once}
