"""Monitor-стадії: відсів анкерів каналу у mon_filter + регіон-контекст tagger'а."""
import pytest

from analysis.models import AnalysisTask, Channel, Post, Region
from analysis.services.monitor_stages import _post_region, mon_filter_once

from .factories import TaskFactory

pytestmark = pytest.mark.django_db


def _monitor_task(**kw):
    return TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR, **kw)


def _post(task, text, **kw):
    return Post.objects.create(
        task=task, url=f"https://t.me/c/1/{Post.objects.count() + 1}",
        text=text, stage=Post.STAGE_MON_COLLECTED, **kw)


def test_mon_filter_drops_channel_reposts():
    """Анкер каналу не має доходити до платного prescreen, навіть якщо довжина ок."""
    task = _monitor_task()
    text = "Влада знову обіцяє відремонтувати дорогу до кінця року, як і минулого."
    anchor = _post(task, text, is_channel_repost=True)
    human = _post(task, text)

    assert mon_filter_once(task) is True

    anchor.refresh_from_db()
    assert anchor.stage == Post.STAGE_DONE
    assert anchor.is_relevant is False
    assert anchor.classification["exclusion_label"] == "channel_repost"

    human.refresh_from_db()
    assert human.stage == Post.STAGE_MON_FILTERED


def test_post_region_prefers_post_fk_then_channel():
    """Регіон беремо з поста, далі з каналу, і лише потім — резерв."""
    task = _monitor_task()
    tuva = Region.objects.create(name="Тива", kind="республіка")
    khakassia = Region.objects.create(name="Хакасія", kind="республіка")
    chat = Channel.objects.create(username="chat1", region_subject=khakassia)

    assert _post_region(_post(task, "t", region_subject=tuva, channel=chat)) == "Тива"
    assert _post_region(_post(task, "t", channel=chat)) == "Хакасія"
    assert _post_region(_post(task, "t"), "резерв") == "резерв"


def test_post_region_is_per_post_not_per_task():
    """Задача на багато регіонів: сусідні пости однієї задачі дають РІЗНИЙ контекст
    (до фіксу всі отримували перший сегмент slug'а — 'fedcrit')."""
    task = _monitor_task(slug="fedcrit-sib-dv")
    a = _post(task, "t", region_subject=Region.objects.create(name="Бурятія"))
    b = _post(task, "t", region_subject=Region.objects.create(name="Чукотський АО"))

    assert {_post_region(a), _post_region(b)} == {"Бурятія", "Чукотський АО"}
