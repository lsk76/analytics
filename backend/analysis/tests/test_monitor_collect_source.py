"""monitor збирається двома способами — і це має бути видно, а не вгадуватись.

Поле AnalysisTask.mon_collect_source: telezip (суцільний потік, платний) або
tg_sample (випадкова вибірка Telegram-акаунтами, команда monitor_sample_collect).
Тести тримають три речі: картка задачі показує саме той спосіб, який стоїть;
поля TeleZip у режимі вибірки з картки зникають (інакше вона брехала б);
платний шлях у вибіркову задачу не заходить ні через чергу, ні через воркер.
"""
import pytest

from analysis.models import AnalysisTask, CollectChunk
from analysis.services import stages
from analysis.services.mcp_api.monitoring import _task_config
from analysis.services.monitor_stages import mon_collect_once

from .factories import TaskFactory

pytestmark = pytest.mark.django_db


def _task(source):
    return TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR,
                       mon_collect_source=source)


def test_card_shows_telezip_fields_only_for_telezip_source():
    card = _task_config(_task(AnalysisTask.MON_SRC_TELEZIP))
    assert "Спосіб збору коментарів" in card
    assert "telezip" in card
    assert "Пошуковий запит TeleZip" in card


def test_card_of_sampled_task_hides_telezip_query():
    card = _task_config(_task(AnalysisTask.MON_SRC_TG_SAMPLE))
    assert "Спосіб збору коментарів" in card
    assert "tg_sample" in card
    assert "monitor_sample_collect" in card
    # головне: у вибірковій задачі запит і чанк не читаються — їх не показуємо
    assert "Пошуковий запит TeleZip" not in card
    assert "Розмір чанка збору" not in card


def test_enqueue_collection_refuses_sampled_task():
    from datetime import date
    t = _task(AnalysisTask.MON_SRC_TG_SAMPLE)
    with pytest.raises(ValueError, match="monitor_sample_collect"):
        stages.enqueue_collection(t, date(2026, 9, 1), date(2026, 9, 2))
    assert CollectChunk.objects.filter(task=t).count() == 0


def test_telezip_task_still_enqueues():
    from datetime import date
    t = _task(AnalysisTask.MON_SRC_TELEZIP)
    assert stages.enqueue_collection(t, date(2026, 9, 1), date(2026, 9, 2)) == 2


def test_mon_collect_worker_skips_sampled_task():
    """Старі чанки в черзі не мають витрачати гроші після перемикання на вибірку."""
    from datetime import date
    t = _task(AnalysisTask.MON_SRC_TELEZIP)
    stages.enqueue_collection(t, date(2026, 9, 1), date(2026, 9, 1))
    t.mon_collect_source = AnalysisTask.MON_SRC_TG_SAMPLE
    t.save(update_fields=["mon_collect_source"])

    assert mon_collect_once(t) is False            # чанк не захоплено
    assert CollectChunk.objects.filter(task=t, status="pending").count() == 1


def test_admin_form_of_sampled_task_has_no_telezip_query():
    """У вибірковій задачі поля TeleZip немає навіть у формі — його не можна ні
    показати, ні випадково зробити обовʼязковим при збереженні."""
    from django.contrib.admin.sites import AdminSite
    from django.contrib.admin.utils import flatten_fieldsets
    from analysis.admin import AnalysisTaskAdmin

    class _User:
        is_superuser = True

    class _Req:
        GET = {}
        user = _User()

    site_admin = AnalysisTaskAdmin(AnalysisTask, AdminSite())
    req = _Req()
    sampled = _task(AnalysisTask.MON_SRC_TG_SAMPLE)
    telezip = _task(AnalysisTask.MON_SRC_TELEZIP)

    fields_sampled = flatten_fieldsets(site_admin.get_fieldsets(req, sampled))
    fields_telezip = flatten_fieldsets(site_admin.get_fieldsets(req, telezip))
    assert "mon_collect_source" in fields_sampled
    assert "telezip_query" not in fields_sampled
    assert "collect_chunk_days" not in fields_sampled
    assert "telezip_query" in fields_telezip


def test_card_of_vk_task_hides_telezip_query():
    """Третій спосіб: коментарі спільнот VK. Запит TeleZip тут теж не читається."""
    card = _task_config(_task(AnalysisTask.MON_SRC_VK))
    assert "vk_comments" in card
    assert "VK" in card
    assert "Пошуковий запит TeleZip" not in card


def test_vk_task_enqueues_chunks_like_telezip():
    """Збір VK іде тими самими «Зборами» і чанками — лише безкоштовно."""
    from datetime import date
    t = _task(AnalysisTask.MON_SRC_VK)
    assert stages.enqueue_collection(t, date(2026, 9, 1), date(2026, 9, 2)) == 2
