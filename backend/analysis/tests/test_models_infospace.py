"""Моделі infospace: Source/SourceSubscription + адитивні поля Post/Event/Task."""
import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from analysis.models import AnalysisTask, Event, Post, Source, SourceSubscription

from .factories import SourceFactory, SubscriptionFactory, TaskFactory

pytestmark = pytest.mark.django_db


def test_source_unique_kind_url():
    SourceFactory(kind=Source.KIND_RSS, url="https://a.example/feed.xml")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            SourceFactory(kind=Source.KIND_RSS, url="https://a.example/feed.xml")
    # той самий url з ІНШИМ kind — дозволено (rss-стрічка і web-лістинг)
    SourceFactory(kind=Source.KIND_WEB, url="https://a.example/feed.xml")


def test_source_defaults_ready_to_poll():
    src = SourceFactory()
    assert src.is_active is True
    assert src.poll_interval_sec == 600
    assert src.consecutive_failures == 0
    assert src.poll_cursor == {} and src.config == {}
    # нове джерело одразу в черзі полінгу
    assert src.next_poll_at <= timezone.now()


def test_subscription_unique_per_task_source():
    sub = SubscriptionFactory()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            SubscriptionFactory(task=sub.task, source=sub.source)


def test_task_infospace_defaults():
    task = TaskFactory()
    assert task.pipeline == AnalysisTask.PIPELINE_INFOSPACE
    assert task.info_match_window_hours == 24
    assert task.info_retention_days == 2
    assert task.info_update_summaries is True
    # дефолтні промпти підтягнулись із infospace/prompts.py — маркери
    # УНІКАЛЬНІ для кожного, щоб зловити swap screen↔judge (обидва містять "JSON")
    assert '"relevant"' in task.info_screen_prompt   # лише у скрін-промпті
    assert '"relevant"' not in task.info_judge_prompt
    assert '"verdict"' in task.info_judge_prompt      # лише у судді
    assert '"verdict"' not in task.info_screen_prompt


def test_post_info_stages_and_source_fk():
    task = TaskFactory()
    src = SourceFactory()
    stage_keys = dict(Post.STAGE_CHOICES)
    assert Post.STAGE_INFO_COLLECTED in stage_keys
    assert Post.STAGE_INFO_SCREENED in stage_keys
    # стадії влазять у max_length поля
    max_len = Post._meta.get_field("stage").max_length
    assert len(Post.STAGE_INFO_COLLECTED) <= max_len
    assert len(Post.STAGE_INFO_SCREENED) <= max_len

    post = Post.objects.create(
        task=task, source=src, stage=Post.STAGE_INFO_COLLECTED,
        url="https://a.example/news/1", title="Заголовок статті",
        text="Текст", posted_at=timezone.now(),
    )
    post.refresh_from_db()
    assert post.source_id == src.id and post.title == "Заголовок статті"


def test_event_last_post_at_nullable_and_settable():
    task = TaskFactory()
    ev = Event.objects.create(task=task, summary="жива подія")
    assert ev.last_post_at is None
    now = timezone.now()
    ev.last_post_at = now
    ev.save(update_fields=["last_post_at"])
    ev.refresh_from_db()
    assert ev.last_post_at == now


def test_pipeline_choice_registered():
    assert AnalysisTask.PIPELINE_INFOSPACE in dict(AnalysisTask.PIPELINE_CHOICES)
    max_len = AnalysisTask._meta.get_field("pipeline").max_length
    assert len(AnalysisTask.PIPELINE_INFOSPACE) <= max_len
