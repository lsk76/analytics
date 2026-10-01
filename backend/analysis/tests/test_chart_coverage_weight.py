"""Графік «% від усіх повідомлень»: при зборі вибіркою коментарі зважуються.

Квота збору однакова на кожен період, а обсяг розмови — ні: тихий тиждень і
тиждень виборів дають однакову вибірку, хоч у другому розмови втричі більше.
Якщо складати зібране зі зібраним, тихий період тягне частку нарівні з
гучним і підсумок з'їжджає. Тому кожен коментар іде в суму зі своєю вагою з
паспорта вибірки: повідомлень у чаті за період ÷ запитано номерів.

Тести тримають: ваги справді застосовуються при складанні періодів;
автопересилки каналу в знаменник не йдуть; коментарі без паспорта важать 1
(старі задачі рахуються як раніше); періоди збору не дають перекриватись.
"""
import json
from datetime import date, datetime, timezone

import pytest
from django.contrib.auth.models import User

from analysis.models import (AnalysisTask, Channel, Event, MonitorSample, Post,
                             Region)
from analysis.services.mcp_api import monitoring
from analysis.services.mcp_api.registry import ToolError

from .factories import TaskFactory

pytestmark = pytest.mark.django_db


def _coverage(client, **params):
    r = client.get("/admin/analysis/event/charts/", {"gran": "month",
                                                     "review_status": "all", **params})
    assert r.status_code == 200
    return json.loads(r.context["data"])["coverage"]


@pytest.fixture
def admin_client(client):
    u = User.objects.create_superuser("sampler", "s@x.y", "pw")
    client.force_login(u)
    return client


@pytest.fixture
def task():
    return TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR,
                       mon_collect_source=AnalysisTask.MON_SRC_TG_SAMPLE)


def _sample(task, channel, start, end, *, span, asked):
    """Паспорт вибірки з наперед заданою вагою span/asked."""
    return MonitorSample.objects.create(
        task=task, channel=channel, period_start=start, period_end=end,
        id_lo=1000, id_hi=1000 + span, n_requested=asked, n_returned=asked,
        n_text=asked, n_user=asked)


def _comments(task, channel, region, day, n, *, sample=None, critical=0, repost=False):
    """n коментарів за один день; перші `critical` стають подіями."""
    when = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    for i in range(n):
        ev = None
        if i < critical:
            ev = Event.objects.create(task=task, event_date=day, region_subject=region,
                                      summary="критика", review_status=Event.REVIEW_APPROVED)
        Post.objects.create(task=task, channel=channel, region_subject=region,
                            url=f"https://t.me/c/{channel.id}/{day}{i}{int(repost)}",
                            text="текст", posted_at=when, sample=sample, event=ev,
                            is_channel_repost=repost, stage=Post.STAGE_DONE)


def test_periods_are_weighted_by_real_volume(admin_client, task):
    """Тихий період і гучний: зважена частка, а не середнє з двох."""
    reg = Region.objects.create(name="Тива")
    ch = Channel.objects.create(username="tuva_chat", title="Чат", region_subject=reg)
    # однакова вибірка (100 коментарів), але обсяг розмови різний: вага 10 і 30
    quiet = _sample(task, ch, date(2026, 8, 1), date(2026, 8, 31), span=1000, asked=100)
    loud = _sample(task, ch, date(2026, 9, 1), date(2026, 9, 30), span=3000, asked=100)
    _comments(task, ch, reg, date(2026, 8, 10), 100, sample=quiet, critical=4)
    _comments(task, ch, reg, date(2026, 9, 10), 100, sample=loud, critical=10)

    cov = {r["date"]: r for r in _coverage(admin_client, task=task.id)}
    assert cov["2026-08-01"]["t"] == 1000 and cov["2026-08-01"]["n"] == 40
    assert cov["2026-09-01"]["t"] == 3000 and cov["2026-09-01"]["n"] == 300
    # 4% і 10% усередині періодів — ваги там скорочуються
    assert cov["2026-08-01"]["pct"] == 4.0
    assert cov["2026-09-01"]["pct"] == 10.0
    # а разом — 340/4000 = 8.5%, а не середнє 7%
    total = sum(r["n"] for r in cov.values()) / sum(r["t"] for r in cov.values())
    assert round(100 * total, 1) == 8.5


def test_channel_reposts_are_not_in_denominator(admin_client, task):
    """Автопересилки каналу — не думка людини, у знаменник не йдуть."""
    reg = Region.objects.create(name="Бурятія")
    ch = Channel.objects.create(username="bur_chat", title="Чат", region_subject=reg)
    smp = _sample(task, ch, date(2026, 8, 1), date(2026, 8, 31), span=100, asked=100)
    _comments(task, ch, reg, date(2026, 8, 5), 10, sample=smp, critical=1)
    _comments(task, ch, reg, date(2026, 8, 5), 90, sample=smp, repost=True)

    cov = _coverage(admin_client, task=task.id)
    assert len(cov) == 1
    assert cov[0]["t"] == 10          # 90 автопересилок не рахуються
    assert cov[0]["pct"] == 10.0      # а не 1%


def test_posts_without_sample_weigh_one(admin_client, task):
    """Зібране до запровадження ваг рахується поштучно, як раніше."""
    reg = Region.objects.create(name="Саха")
    ch = Channel.objects.create(username="sakha_chat", title="Чат", region_subject=reg)
    _comments(task, ch, reg, date(2026, 8, 5), 50, critical=2)

    cov = _coverage(admin_client, task=task.id)
    assert cov[0]["t"] == 50 and cov[0]["n"] == 2
    assert cov[0]["pct"] == 4.0


def test_overlapping_periods_are_refused():
    """Коментар із перекриття потрапив би в знаменник двічі — збір не ставимо."""
    from accounts.models import TelegramAccount
    from analysis.models import MonitorChat
    t = TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR,
                    mon_collect_source=AnalysisTask.MON_SRC_TG_SAMPLE)
    acc = TelegramAccount.objects.create(name="Збирач", phone_number="+70000000011",
                                         is_authenticated=True)
    ch = Channel.objects.create(username="over_chat", title="Чат")
    MonitorChat.objects.create(task=t, channel=ch, tg_account=acc)
    _sample(t, ch, date(2026, 9, 1), date(2026, 9, 30), span=100, asked=10)

    with pytest.raises(ToolError, match="перекривається"):
        monitoring.sample_collect(task=t.slug, date_from="2026-09-15",
                                  date_to="2026-10-15", mode="collect", confirm=True)
    # той самий період — не перекриття, а перезбір: проходить до попередження
    out = monitoring.sample_collect(task=t.slug, date_from="2026-09-01",
                                    date_to="2026-09-30", mode="collect")
    assert "НЕ поставлено" in out
