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


def _claim_job():
    now = djtz.now()
    cutoff = now - LOCK_TIMEOUT
    with transaction.atomic():
        job = (WarmUpJob.objects
               .filter(Q(status="pending") | Q(status="running", locked_at__lt=cutoff))
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
    job.status = "done" if res.get("ok") else "failed"
    job.finished_at = djtz.now()
    job.save(update_fields=["status", "result", "error", "finished_at"])
    return True
