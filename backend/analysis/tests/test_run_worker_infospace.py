"""run_worker: роутинг info_ → pipeline="infospace" + механізм taskless-стадій."""
import itertools

import pytest
from django.core.management import call_command

from analysis.management.commands import run_worker
from analysis.models import AnalysisTask

from .factories import TaskFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def four_tasks():
    return {
        "events": TaskFactory(pipeline=AnalysisTask.PIPELINE_EVENTS),
        "monitor": TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR),
        "research": TaskFactory(pipeline=AnalysisTask.PIPELINE_RESEARCH),
        "infospace": TaskFactory(pipeline=AnalysisTask.PIPELINE_INFOSPACE),
    }


@pytest.mark.parametrize("stage,pipeline", [
    ("enrich", "events"),          # без префікса → events (як було)
    ("mon_filter", "monitor"),
    ("res_filter", "research"),
    ("info_screen", "infospace"),  # нова гілка
    ("info_event", "infospace"),
])
def test_stage_prefix_routes_to_own_pipeline(four_tasks, stage, pipeline):
    got = run_worker.Command()._tasks(None, stage)
    assert [t.id for t in got] == [four_tasks[pipeline].id]


def test_inactive_infospace_task_not_picked(four_tasks):
    four_tasks["infospace"].is_active = False
    four_tasks["infospace"].save(update_fields=["is_active"])
    assert run_worker.Command()._tasks(None, "info_screen") == []


def test_review_stage_sees_events_and_infospace(four_tasks):
    # авто-аудит спільний: review-воркер бачить events + infospace (не monitor/research)
    got = {t.id for t in run_worker.Command()._tasks(None, "review")}
    assert four_tasks["events"].id in got
    assert four_tasks["infospace"].id in got
    assert four_tasks["monitor"].id not in got
    assert four_tasks["research"].id not in got


def test_taskless_stage_registered():
    assert "info_collect" in run_worker.TASKLESS_STAGES


def test_taskless_runner_called_without_task_arg(monkeypatch):
    """Taskless-ранер викликається БЕЗ аргументу задачі, навіть коли задач нема."""
    calls = []

    def fake_runner():  # сигнатура без task — контракт taskless
        calls.append(1)
        return False    # нема роботи

    monkeypatch.setitem(run_worker.ALL_RUNNERS, "info_collect", fake_runner)
    call_command("run_worker", stage="info_collect", once=True)
    assert calls == [1]


def test_taskless_drains_until_empty(monkeypatch):
    """Ранер смикається, поки повертає True (є робота), потім idle-sleep."""
    results = itertools.chain([True, True, False], itertools.repeat(False))
    calls = []

    def fake_runner():
        calls.append(1)
        return next(results)

    class _Stop(Exception):
        pass

    def fake_sleep(_):
        raise _Stop  # перший idle-sleep = кінець тесту

    monkeypatch.setitem(run_worker.ALL_RUNNERS, "info_collect", fake_runner)
    monkeypatch.setattr(run_worker.time, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        call_command("run_worker", stage="info_collect")
    # раунд 1: True, True, False (drain); раунд 2: False → idle → sleep
    assert len(calls) == 4


def test_taskless_exception_keeps_worker_alive(monkeypatch):
    def boom():
        raise RuntimeError("adapter failed")

    monkeypatch.setitem(run_worker.ALL_RUNNERS, "info_collect", boom)
    # --once: виняток ловиться, команда завершується без падіння
    call_command("run_worker", stage="info_collect", once=True)
