"""Збір коментарів VK у monitor-конвеєр (mon_collect_source=vk_comments).

VK підмінено: перевіряємо, що з відповідей VK виходять правильні `Post`
(1 коментар = 1 рядок, посилання унікальне), що закрита спільнота не валить
чанк і що чанк проходить тим самим шляхом, що й TeleZip-збір.
"""
from datetime import date, datetime, timezone
from unittest import mock

import pytest

from analysis.models import AnalysisTask, Channel, CollectChunk, MonitorChat, Post
from analysis.services import vk, vk_monitor
from analysis.services.monitor_stages import mon_collect_once

from .factories import TaskFactory

pytestmark = pytest.mark.django_db

SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 9, 2, 23, 59, tzinfo=timezone.utc)


def _task():
    return TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR,
                       mon_collect_source=AnalysisTask.MON_SRC_VK)


def _community(task, url="https://vk.com/club42"):
    ch, _ = Channel.ensure(url, name="Спільнота")
    MonitorChat.objects.create(task=task, channel=ch)
    return ch


def _wall(items):
    return mock.patch.object(vk, "wall_posts_between", return_value=items)


def _comments(items, authors=None):
    return mock.patch.object(vk, "all_comments", return_value=(items, authors or {}))


def _post_item(pid, n_comments=2):
    return {"id": pid, "date": int(SINCE.timestamp()), "text": "пост",
            "comments": {"count": n_comments}}


def _comment(cid, text="влада знову нічого не зробила", from_id=7):
    return {"id": cid, "date": int(SINCE.timestamp()), "text": text, "from_id": from_id}


def test_collect_window_makes_one_post_per_comment():
    t = _task()
    _community(t)
    with mock.patch.object(vk, "resolve_owner", return_value=(-42, "group")), \
            _wall([_post_item(5)]), _comments([_comment(1), _comment(2, from_id=8)],
                                              {7: {"first_name": "Іван", "last_name": "П"}}):
        created = vk_monitor.collect_window(t, SINCE, UNTIL)
    assert created == 2
    posts = list(Post.objects.filter(task=t).order_by("url"))
    assert [p.url for p in posts] == ["https://vk.com/wall-42_5?reply=1",
                                      "https://vk.com/wall-42_5?reply=2"]
    assert {p.stage for p in posts} == {Post.STAGE_MON_COLLECTED}
    assert posts[0].author_name == "Іван П"
    assert posts[0].author_tg_id == 7          # «скільки різних людей» рахується цим полем
    assert posts[0].classification["_collect_source"] == "vk_comments"


def test_owner_id_cached_in_directory():
    t = _task()
    ch = _community(t)
    with mock.patch.object(vk, "resolve_owner", return_value=(-42, "group")) as res, \
            _wall([]), _comments([]):
        vk_monitor.collect_window(t, SINCE, UNTIL)
        ch.refresh_from_db()
        assert ch.raw_meta["vk_owner_id"] == -42
        vk_monitor.collect_window(t, SINCE, UNTIL)
    assert res.call_count == 1


def test_same_comment_twice_is_not_duplicated():
    """Коментар під старим постом потрапляє у два сусідні чанки — рядок один."""
    t = _task()
    _community(t)
    with mock.patch.object(vk, "resolve_owner", return_value=(-42, "group")), \
            _wall([_post_item(5)]), _comments([_comment(1)]):
        assert vk_monitor.collect_window(t, SINCE, UNTIL) == 1
        assert vk_monitor.collect_window(t, SINCE, UNTIL) == 0
    assert Post.objects.filter(task=t).count() == 1


def test_closed_community_is_skipped_not_fatal():
    t = _task()
    _community(t, "https://vk.com/club1")
    _community(t, "https://vk.com/club2")
    owners = {"https://vk.com/club1": (-1, "group"), "https://vk.com/club2": (-2, "group")}

    def wall(owner_id, *a, **kw):
        if owner_id == -1:
            raise vk.VkAccessDenied("Access denied", vk.ERR_ACCESS_DENIED)
        return [_post_item(5)]

    with mock.patch.object(vk, "resolve_owner", side_effect=lambda url: owners[url]), \
            mock.patch.object(vk, "wall_posts_between", side_effect=wall), \
            _comments([_comment(1)]):
        assert vk_monitor.collect_window(t, SINCE, UNTIL) == 1


def test_task_without_vk_communities_complains():
    t = _task()
    with pytest.raises(ValueError, match="спільноти VK"):
        vk_monitor.collect_window(t, SINCE, UNTIL)


def test_worker_processes_chunk_through_vk():
    """Той самий чанк, що й у TeleZip-зборі, але джерело — VK (і задарма)."""
    t = _task()
    _community(t)
    chunk = CollectChunk.objects.create(task=t, date_from=date(2026, 9, 1),
                                        date_to=date(2026, 9, 2), status="pending")
    with mock.patch.object(vk, "resolve_owner", return_value=(-42, "group")), \
            _wall([_post_item(5)]), _comments([_comment(1)]):
        assert mon_collect_once(t) is True
    chunk.refresh_from_db()
    assert chunk.status == "done"
    assert chunk.posts_collected == 1
    assert Post.objects.filter(task=t).count() == 1


def test_rate_limit_keeps_chunk_pending():
    t = _task()
    _community(t)
    chunk = CollectChunk.objects.create(task=t, date_from=date(2026, 9, 1),
                                        date_to=date(2026, 9, 1), status="pending")
    with mock.patch.object(vk_monitor, "collect_window",
                           side_effect=vk.VkRateLimited("часто", retry_after=30)):
        assert mon_collect_once(t) is True
    chunk.refresh_from_db()
    assert chunk.status == "pending"          # день не винен — просто темп
    assert chunk.next_retry_at is not None
