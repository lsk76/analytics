"""Запуск тегування створюється сам, коли вікно зібране й ще не тегувалось.

Інцидент 30.09: вересневий збір задачі vladacrit-8rep-2026 завершився, прескрін
виділив 1298 кандидатів, а розмітка не починалась годину. Причина не в поломці:
пачки нарізаються тільки в межах `ResearchRun`, а вибірковий збір веде своя
черга і запуску не створює. Тобто на кожен новий період потрібен був ручний
виклик, і поки його немає, кандидати стоять, а ранер простоює без жодної
помилки в логах — саме тому проблему й не видно.

Тести тримають чотири умови, кожна з яких колись коштувала б грошей або даних:
вікно береться з паспорта вибірки (інакше чисельник і знаменник з різних
періодів), другий запуск на те саме вікно не створюється (подвійна оплата),
незавершений збір не тегується (неповний знаменник), і потік TeleZip автостарт
не чіпає (його веде run_create).
"""
from datetime import date, datetime, timezone as dtz

import pytest

from analysis.models import (AnalysisTask, MonitorSample, MonitorSampleJob,
                             Channel, Post, ResearchRun)
from analysis.services import agent_runner

from .factories import TaskFactory

pytestmark = pytest.mark.django_db

WIN = (date(2026, 9, 1), date(2026, 9, 30))


def _task(**kw):
    kw.setdefault("mon_collect_source", AnalysisTask.MON_SRC_TG_SAMPLE)
    return TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR, **kw)


def _passport(task, window=WIN):
    ch = Channel.objects.create(tg_id=-100123, title="чат", username="chat")
    MonitorSample.objects.create(task=task, channel=ch, period_start=window[0],
                                 period_end=window[1], id_lo=1, id_hi=1000,
                                 n_requested=100, n_returned=90, n_text=80)


def _collect_job(task, window=WIN, status="done"):
    return MonitorSampleJob.objects.create(
        task=task, date_from=window[0], date_to=window[1],
        mode=MonitorSampleJob.MODE_COLLECT, status=status)


def _candidate(task, day=date(2026, 9, 10)):
    """Пост, який підлягає тегуванню: прескрін сказав «так», тегів немає."""
    return Post.objects.create(
        task=task, text="Опять чиновники обещают и ничего не делают",
        posted_at=datetime(day.year, day.month, day.day, 12, tzinfo=dtz.utc),
        stage=Post.STAGE_MON_PRESCREENED,
        classification={"is_filtered": False,
                        "_prescreen": {"could_be_criticism": True}})


def test_створює_запуск_на_зібране_вікно():
    task = _task()
    _passport(task)
    _collect_job(task)
    _candidate(task)

    assert agent_runner._autostart(task) is True

    run = ResearchRun.objects.get(task=task)
    # вікно саме з паспорта: за ним рахується знаменник частки
    assert (run.date_from, run.date_to) == WIN
    # 'collected' — бо збирати вже нічого; саме цей стан підхоплює mon_runs
    assert run.status == "collected"


def test_другий_запуск_на_те_саме_вікно_не_створюється():
    task = _task()
    _passport(task)
    _collect_job(task)
    _candidate(task)
    assert agent_runner._autostart(task) is True

    # запуск уже закрився, а пости лишились без тегів — повторно не беремо:
    # інакше кожен тік створював би новий запуск і платив за те саме
    ResearchRun.objects.filter(task=task).update(status="done")
    assert agent_runner._autostart(task) is False
    assert ResearchRun.objects.filter(task=task).count() == 1


def test_поки_запуск_живий_нового_не_створюємо():
    task = _task()
    _passport(task)
    _collect_job(task)
    _candidate(task)
    ResearchRun.objects.create(task=task, date_from=date(2026, 8, 1),
                               date_to=date(2026, 8, 31), status="awaiting_agent")

    assert agent_runner._autostart(task) is False


def test_незавершений_збір_не_тегується():
    """Неповне вікно дало б заниження частки, схоже на справжнє падіння."""
    task = _task()
    _passport(task)
    _collect_job(task, status="running")
    _candidate(task)

    assert agent_runner._autostart(task) is False
    assert not ResearchRun.objects.filter(task=task).exists()


def test_немає_кандидатів_немає_запуску():
    task = _task()
    _passport(task)
    _collect_job(task)
    # пост є, але прескрін його відсіяв — тегувати нічого
    Post.objects.create(
        task=task, text="Погода норм", posted_at=datetime(2026, 9, 10, 12, tzinfo=dtz.utc),
        stage=Post.STAGE_DONE,
        classification={"_prescreen": {"could_be_criticism": False}})

    assert agent_runner._autostart(task) is False


def test_потік_telezip_автостарт_не_чіпає():
    """Там запуск створює run_create разом із платними чанками збору."""
    task = _task(mon_collect_source=AnalysisTask.MON_SRC_TELEZIP)
    _passport(task)
    _collect_job(task)
    _candidate(task)

    assert agent_runner._autostart(task) is False
