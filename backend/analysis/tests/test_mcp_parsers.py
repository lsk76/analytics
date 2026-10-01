"""`parsers_health`: чи вердикт відрізняє «помер один сайт» від «зламався парсер».

Сенс інструмента — не перелічити проблеми, а не кричати вовк: поодиноке джерело
з нулем елементів нормальне (сайт помер, редизайн, бан по IP), а половина типу
одночасно — ні, бо сайти не домовляються переверстатись разом. Тому тести
стоять саме на стелях і на тому, що мертвий СТОРОЖ (детектор якості, прогін
tgstat) теж вважається поломкою.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from analysis.models import Post, Setting, Source, SourceSubscription
from analysis.services.mcp_api import TOOLS, parsers
from analysis.tests.factories import SourceFactory, TaskFactory

pytestmark = pytest.mark.django_db

HEALTHY = {"quality_ok": True, "consecutive_failures": 0}


def make_sources(task, kind, total, bad=0, *, checked_ago_h=1):
    checked = timezone.now() - timedelta(hours=checked_ago_h)
    out = []
    for i in range(total):
        s = SourceFactory(kind=kind, url=f"https://site{kind}{i}.ru/news/",
                          name=f"{kind} {i}")
        s.quality_ok = i >= bad
        s.quality_note = "" if s.quality_ok else "0 елементів (discovery порожній)"
        s.last_healthcheck_at = checked
        s.save(update_fields=["quality_ok", "quality_note", "last_healthcheck_at"])
        SourceSubscription.objects.create(task=task, source=s)
        out.append(s)
    return out


def fresh_posts(task, n=3):
    for i in range(n):
        Post.objects.create(task=task, url=f"https://x.ru/{i}", text="t", stage="done")


@pytest.fixture
def task():
    return TaskFactory()


def health(**kw):
    return TOOLS["parsers_health"](kw)


# ------------------------------------------------------ норма vs поломка адаптера

def test_single_dead_site_is_a_warning_not_a_breakage(task):
    make_sources(task, "web", 10, bad=1)
    fresh_posts(task)
    out = health()
    assert "🟡 є підозри" in out
    assert "1 джерел із 10 під підозрою" in out


def test_half_of_a_kind_silent_is_a_broken_adapter(task):
    make_sources(task, "web", 8, bad=5)
    fresh_posts(task)
    out = health()
    assert "ЩОСЬ ЗЛАМАЛОСЬ" in out
    assert "не окремі сайти, а адаптер/проксі" in out


def test_two_of_three_is_not_enough_to_cry_wolf(task):
    """Частка висока, але джерел мало — на трьох сайтах висновок про адаптер
    робити рано (стеля BROKEN_MIN)."""
    make_sources(task, "rss", 3, bad=2)
    fresh_posts(task)
    out = health()
    assert "🟡 є підозри" in out and "адаптер" not in out


def test_all_healthy_is_ok(task):
    make_sources(task, "rss", 5)
    make_sources(task, "web", 5)
    fresh_posts(task)
    Setting.objects.create(key="tgstat_selftest_last",
                           value="2026-10-01T05:00:00+00:00 ok: усі 5 перевірки пройшли")
    out = health()
    assert "✓ парсинги працюють" in out
    assert "Що робити" not in out


def test_unsubscribed_sources_do_not_count(task):
    """Джерело без підписки воркер не бере — його нуль нічого не означає."""
    make_sources(task, "web", 4)
    lonely = SourceFactory(kind="web", url="https://nobody.ru/news/", name="нічий")
    lonely.quality_ok = False
    lonely.quality_note = "0 елементів (нічий)"
    lonely.save(update_fields=["quality_ok", "quality_note"])
    fresh_posts(task)
    Setting.objects.create(key="tgstat_selftest_last", value="2026-10-01 ok: усі пройшли")
    out = health()
    assert "✓ парсинги працюють" in out
    # ні в лічильниках, ні в таблиці прикладів: воркер цього джерела не бере
    assert "нічий" not in out
    assert "Під підозрою" not in out


# ------------------------------------------------------------- мертві сторожі

def test_stale_quality_detector_is_a_breakage(task):
    make_sources(task, "rss", 5, checked_ago_h=parsers.HEALTHCHECK_STALE_HOURS + 2)
    fresh_posts(task)
    out = health()
    assert "ЩОСЬ ЗЛАМАЛОСЬ" in out
    assert "детектор якості стоїть" in out and "worker-info-healthcheck" in out


def test_tgstat_never_checked_is_a_warning(task):
    make_sources(task, "rss", 5)
    fresh_posts(task)
    out = health()
    assert "ще не запускався" in out and "🟡" in out


def test_tgstat_broken_line_propagates(task):
    make_sources(task, "rss", 5)
    fresh_posts(task)
    Setting.objects.create(key="tgstat_selftest_last",
                           value="2026-10-01T05:00:00+00:00 broken: зламалось 1 з 5: channel_card")
    out = health()
    assert "ЩОСЬ ЗЛАМАЛОСЬ" in out and "РОЗБІР tgstat ЗЛАМАВСЯ" in out


def test_tgstat_stale_run_is_a_warning(task):
    make_sources(task, "rss", 5)
    fresh_posts(task)
    row = Setting.objects.create(key="tgstat_selftest_last", value="old ok: усі пройшли")
    Setting.objects.filter(pk=row.pk).update(
        updated_at=timezone.now() - timedelta(hours=parsers.TGSTAT_STALE_HOURS + 1))
    out = health()
    assert "старший за" in out and "🟡 є підозри" in out


# --------------------------------------------------------- збір і TeleZip

def test_no_posts_for_a_day_is_a_breakage(task):
    make_sources(task, "rss", 5)
    out = health()
    assert "ЩОСЬ ЗЛАМАЛОСЬ" in out and "0 постів за добу" in out


def test_all_recent_telezip_chunks_empty_is_a_breakage(task):
    from analysis.models import CollectChunk
    make_sources(task, "rss", 5)
    fresh_posts(task)
    for i in range(parsers.TELEZIP_LAST_CHUNKS):
        CollectChunk.objects.create(task=task, date_from="2026-09-01", date_to="2026-09-02",
                                    status="done", posts_collected=0,
                                    finished_at=timezone.now())
    out = health()
    assert "ЩОСЬ ЗЛАМАЛОСЬ" in out
    assert "усі останні чанки TeleZip порожні" in out
    assert "чужому діалекті" in out


def test_one_empty_chunk_among_full_ones_is_quiet(task):
    from analysis.models import CollectChunk
    make_sources(task, "rss", 5)
    fresh_posts(task)
    for i in range(10):
        CollectChunk.objects.create(task=task, date_from="2026-09-01", date_to="2026-09-02",
                                    status="done", posts_collected=0 if i == 0 else 50,
                                    finished_at=timezone.now())
    out = health()
    assert "9 з 10 останніх чанків принесли пости" in out
    assert "ЩОСЬ ЗЛАМАЛОСЬ" not in out
