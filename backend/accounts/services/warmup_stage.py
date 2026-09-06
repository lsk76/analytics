"""warm_up — taskless-стадія для run_worker.py: черга WarmUpJob.

Той самий claim-патерн (SELECT ... FOR UPDATE SKIP LOCKED), що й у
test_bot_stage.py / analysis/services/stages.py::_claim_chunk.
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone as djtz

from ..models import WarmUpJob
from .telegram_client import TelegramUserClient

LOCK_TIMEOUT = timedelta(minutes=10)
RETRY_DELAYS = [30, 60, 90]  # секунд, після 1-ї/2-ї/3-ї помилки; 4-та — вже failed остаточно


def _claim_job():
    now = djtz.now()
    cutoff = now - LOCK_TIMEOUT
    with transaction.atomic():
        job = (WarmUpJob.objects
               .filter(Q(status="pending") | Q(status="running", locked_at__lt=cutoff))
               .filter(Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=now))
               .select_for_update(skip_locked=True)
               .order_by("created_at")
               .first())
        if job:
            job.status = "running"
            job.locked_at = now
            job.attempts += 1
            job.save(update_fields=["status", "locked_at", "attempts"])
    return job


def warm_up_once() -> bool:
    job = _claim_job()
    if not job:
        return False

    res = TelegramUserClient.join_channels_sync(job.account, job.handles)
    job.result = res
    job.error = res.get("error") or ""

    if res.get("ok"):
        job.status = "done"
        job.finished_at = djtz.now()
        job.save(update_fields=["status", "result", "error", "finished_at"])
    elif job.attempts <= len(RETRY_DELAYS):
        delay = RETRY_DELAYS[job.attempts - 1]
        job.status = "pending"
        job.scheduled_at = djtz.now() + timedelta(seconds=delay)
        job.save(update_fields=["status", "scheduled_at", "result", "error"])
    else:
        job.status = "failed"
        job.finished_at = djtz.now()
        job.save(update_fields=["status", "result", "error", "finished_at"])
    return True
