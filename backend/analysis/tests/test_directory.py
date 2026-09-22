"""Довідник каналів і джерел: нормалізація адреси та бекфіл."""
import pytest
from django.core.management import call_command

from analysis.models import Channel, Event, Post, Source, SourceSubscription
from analysis.services import directory as d
from analysis.tests.factories import SourceFactory, TaskFactory

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("raw,expected", [
    ("@KemerTop", ("telegram", "https://t.me/kemertop")),
    ("https://t.me/s/KemerTop", ("telegram", "https://t.me/kemertop")),
    ("t.me/kemertop/26887", ("telegram", "https://t.me/kemertop")),
    ("https://telegram.me/KemerTop/", ("telegram", "https://t.me/kemertop")),
    ("https://t.me/+kzFkyBCF-n00NzUy", ("telegram", "https://t.me/+kzFkyBCF-n00NzUy")),
    ("https://t.me/joinchat/AbCd", ("telegram", "https://t.me/+AbCd")),
    ("https://t.me/c/1234567890/55", ("telegram", "https://t.me/c/1234567890")),
    ("https://m.vk.com/Club123/", ("vk", "https://vk.com/club123")),
    ("http://WWW.NewsOmsk.ru/news/", ("web", "https://newsomsk.ru/news")),
    ("https://14.ru/rss", ("web", "https://14.ru/rss")),
    ("", ("", "")),
])
def test_normalize_url(raw, expected):
    assert d.normalize_url(raw) == expected


def test_rss_hint_and_query_kept():
    assert d.normalize_url("https://site.ru/rss.xml?lang=ru", kind_hint="rss") == \
        ("rss", "https://site.ru/rss.xml?lang=ru")


def test_telegram_url_fallbacks():
    assert d.telegram_url("linked:parent", -1001234567890) == "https://t.me/c/1234567890"
    assert d.telegram_url("", 1848655900) == "https://t.me/c/1848655900"
    assert d.telegram_url("", None) == ""


def test_backfill_links_sources_and_recomputes_reach():
    task = TaskFactory()                                   # infospace
    older = Channel.objects.create(username="kemertop", tg_id=2, subscribers=1)   # дубль за регістром
    ch = Channel.objects.create(username="KemerTop", tg_id=1, subscribers=5000)  # свіжіший — забирає адресу
    tg = SourceFactory(kind="telegram", url="https://t.me/KemerTop", name="Кузбас")
    web = SourceFactory(kind="web", url="http://www.newsomsk.ru/news/", name="Омськ")
    for s in (tg, web):
        SourceSubscription.objects.create(task=task, source=s)
    ev = Event.objects.create(task=task, summary="x", review_status="approved")
    Post.objects.create(task=task, url="https://t.me/KemerTop/1", source=tg, event=ev,
                        text="a", stage="done")
    Post.objects.create(task=task, url="https://newsomsk.ru/n/1", source=web, event=ev,
                        text="b", stage="done")

    call_command("directory_backfill")

    ch.refresh_from_db(); older.refresh_from_db(); tg.refresh_from_db(); web.refresh_from_db(); ev.refresh_from_db()
    assert ch.url == "https://t.me/kemertop" and older.url == "https://t.me/c/2"
    assert tg.channel_id == ch.id
    assert web.channel.platform == "web" and web.channel.url == "https://newsomsk.ru/news" \
        and web.channel.title == "Омськ"
    assert Post.objects.get(source=tg).channel_id == ch.id
    assert Post.objects.get(source=web).channel_id is None
    assert ev.channel_count == 1 and ev.reach == 5000
    # повторний запуск нічого не ламає і не дублює довідник
    n = Channel.objects.count()
    call_command("directory_backfill")
    assert Channel.objects.count() == n
