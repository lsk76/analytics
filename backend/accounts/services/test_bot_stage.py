"""test_bot — taskless-стадія для run_worker.py: черга TestBotJob.

Патерн claim'у (SELECT ... FOR UPDATE SKIP LOCKED, гейт по scheduled_at) —
той самий, що й у analysis/services/stages.py::_claim_chunk для CollectChunk.
"""
import random
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone as djtz

from ..models import TestBotJob
from .telegram_client import TelegramUserClient

LOCK_TIMEOUT = timedelta(minutes=10)
RETRY_DELAYS = [30, 60, 90]  # секунд, після 1-ї/2-ї/3-ї помилки; 4-та — вже failed остаточно


def _claim_job():
    now = djtz.now()
    cutoff = now - LOCK_TIMEOUT
    with transaction.atomic():
        job = (TestBotJob.objects
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


def _unlock_next(job) -> None:
    """Розблокувати наступне завдання в batch (лише коли поточне дійшло до фінального стану)."""
    next_job = (TestBotJob.objects
               .filter(batch_id=job.batch_id, order=job.order + 1, status="queued")
               .first())
    if next_job:
        delay = random.uniform(next_job.pause_min * 60, next_job.pause_max * 60)
        next_job.status = "pending"
        next_job.scheduled_at = djtz.now() + timedelta(seconds=delay)
        next_job.save(update_fields=["status", "scheduled_at"])


def test_bot_once() -> bool:
    """Забрати одне готове завдання, виконати. True = була робота (незалежно від результату)."""
    job = _claim_job()
    if not job:
        return False

    res = TelegramUserClient.test_bot_flow_sync(
        job.account, job.bot_username, feedback_text=job.feedback_text,
    )
    job.result = res
    job.error = res.get("error") or ""

    if res.get("ok"):
        job.status = "done"
        job.finished_at = djtz.now()
        job.save(update_fields=["status", "result", "error", "finished_at"])
        _unlock_next(job)
    elif job.attempts <= len(RETRY_DELAYS):
        delay = RETRY_DELAYS[job.attempts - 1]
        job.status = "pending"
        job.scheduled_at = djtz.now() + timedelta(seconds=delay)
        job.save(update_fields=["status", "scheduled_at", "result", "error"])
    else:
        job.status = "failed"
        job.finished_at = djtz.now()
        job.save(update_fields=["status", "result", "error", "finished_at"])
        _unlock_next(job)
    return True
