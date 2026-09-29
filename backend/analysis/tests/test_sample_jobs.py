"""Вибірковий збір (tg_sample) запускається З MCP, а не лише з консолі сервера.

Раніше `monitor_sample_collect` можна було виконати тільки руками на сервері:
у мережевому MCP такого інструмента не було взагалі, тож вибіркове дослідження
неможливо було зібрати (інцидент 29.09 із задачею vladacrit-8rep-2026). Тепер
замовлення йде в чергу `MonitorSampleJob`, а виконує його стадія `mon_sample`.

Тести тримають: платний шлях у вибіркову задачу не заходить і навпаки;
справжній збір без згоди людини не ставиться; воркер викликає саму команду й
не бере чужі задачі.
"""
from datetime import date

import pytest

from accounts.models import TelegramAccount
from analysis.models import AnalysisTask, Channel, MonitorChat, MonitorSampleJob
from analysis.services import sample_stage
from analysis.services.mcp_api import monitoring
from analysis.services.mcp_api.registry import ToolError

from .factories import TaskFactory

pytestmark = pytest.mark.django_db


def _sampled_task(**kw):
    t = TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR,
                    mon_collect_source=AnalysisTask.MON_SRC_TG_SAMPLE, **kw)
    acc = TelegramAccount.objects.create(name="Збирач", phone_number="+70000000009",
                                         is_authenticated=True)
    ch = Channel.objects.create(username="chat1", title="Чат 1")
    MonitorChat.objects.create(task=t, channel=ch, tg_account=acc)
    return t


def _queue(task, **kw):
    kw.setdefault("date_from", "2026-08-01")
    kw.setdefault("date_to", "2026-08-31")
    return monitoring.sample_collect(task=task.slug, **kw)


def test_dry_run_queues_job_without_confirm():
    t = _sampled_task()
    out = _queue(t, mode="dry_run")
    job = MonitorSampleJob.objects.get(task=t)
    assert job.status == "pending" and job.mode == MonitorSampleJob.MODE_DRY_RUN
    assert f"#{job.id}" in out


def test_real_collect_needs_human_consent():
    t = _sampled_task()
    out = _queue(t, mode="collect")
    assert "НЕ поставлено" in out and "confirm=true" in out
    assert not MonitorSampleJob.objects.exists()

    _queue(t, mode="collect", confirm=True)
    job = MonitorSampleJob.objects.get(task=t)
    assert job.mode == MonitorSampleJob.MODE_COLLECT and job.status == "pending"


def test_telezip_task_is_sent_to_run_create():
    t = TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR,
                    mon_collect_source=AnalysisTask.MON_SRC_TELEZIP)
    with pytest.raises(ToolError, match="run_create"):
        _queue(t, mode="dry_run")


def test_task_without_authorised_account_is_refused():
    t = TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR,
                    mon_collect_source=AnalysisTask.MON_SRC_TG_SAMPLE)
    MonitorChat.objects.create(task=t, channel=Channel.objects.create(username="c2"))
    with pytest.raises(ToolError, match="акаунт"):
        _queue(t, mode="dry_run")


def test_second_job_refused_while_one_is_active():
    t = _sampled_task()
    _queue(t, mode="dry_run")
    with pytest.raises(ToolError, match="вже є завдання"):
        _queue(t, mode="probe")


def test_cancel_only_before_it_started():
    t = _sampled_task()
    _queue(t, mode="dry_run")
    job = MonitorSampleJob.objects.get(task=t)
    assert "знято" in monitoring.sample_cancel(job_id=job.id)
    job.refresh_from_db()
    assert job.status == "cancelled"

    job.status = "running"
    job.save(update_fields=["status"])
    with pytest.raises(ToolError, match="pending"):
        monitoring.sample_cancel(job_id=job.id)


def test_worker_runs_the_command_and_closes_the_job(monkeypatch, tmp_path):
    t = _sampled_task()
    _queue(t, mode="collect", per_region=300, seed=7, confirm=True)
    job = MonitorSampleJob.objects.get(task=t)
    monkeypatch.setattr(sample_stage, "SAMPLES_DIR", str(tmp_path))

    calls = []

    def fake(name, *a, stdout=None, stderr=None, **opts):
        calls.append((name, opts))
        stdout.write("зібрано 300\n")

    monkeypatch.setattr(sample_stage, "call_command", fake)
    assert sample_stage.mon_sample_once(t) is True
    assert sample_stage.mon_sample_once(t) is False      # черга спорожніла

    name, opts = calls[0]
    assert name == "monitor_sample_collect"
    assert opts == {"task": t.slug, "date_from": "2026-08-01", "date_to": "2026-08-31",
                    "per_region": 300, "seed": 7}
    job.refresh_from_db()
    assert job.status == "done" and "зібрано 300" in job.log and job.finished_at


def test_worker_marks_failure_and_keeps_the_output(monkeypatch, tmp_path):
    t = _sampled_task()
    _queue(t, mode="probe", probe_ids=50)
    monkeypatch.setattr(sample_stage, "SAMPLES_DIR", str(tmp_path))

    def boom(name, *a, stdout=None, stderr=None, **opts):
        assert opts["probe"] == 50 and "per_region" not in opts
        stdout.write("чат 1: FloodWait 600\n")
        raise RuntimeError("FloodWait 600")

    monkeypatch.setattr(sample_stage, "call_command", boom)
    assert sample_stage.mon_sample_once(t) is True
    job = MonitorSampleJob.objects.get(task=t)
    assert job.status == "failed" and "FloodWait" in job.error
    assert "FloodWait 600" in job.log


def test_worker_ignores_telezip_task():
    t = TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR,
                    mon_collect_source=AnalysisTask.MON_SRC_TELEZIP)
    MonitorSampleJob.objects.create(task=t, date_from=date(2026, 8, 1),
                                    date_to=date(2026, 8, 31))
    assert sample_stage.mon_sample_once(t) is False
    assert MonitorSampleJob.objects.get(task=t).status == "pending"


def test_samples_list_shows_the_queue_and_the_log(monkeypatch, tmp_path):
    t = _sampled_task()
    _queue(t, mode="dry_run")
    monkeypatch.setattr(sample_stage, "SAMPLES_DIR", str(tmp_path))
    out = monitoring.samples_list(task=t.slug, log=True)
    assert "Завдання на вибірку" in out and "2026-08-01…2026-08-31" in out
    assert "вікон немає" in out          # паспортів ще нема — збір не йшов
