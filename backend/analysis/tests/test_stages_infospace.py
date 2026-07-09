"""Стадії infospace: collect (фан-аут/розклад/збій), screen, event, retention."""
from datetime import timedelta

import pytest
from django.utils import timezone

from analysis.models import Event, Post, Source, SourceSubscription
from analysis.services.infospace import stages
from analysis.services.infospace.adapters.base import RawItem

from .factories import SourceFactory, SubscriptionFactory, TaskFactory

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------ collect

class _StubAdapter:
    def __init__(self, items, raises=None):
        self._items, self._raises = items, raises

    def fetch(self, source):
        if self._raises:
            raise self._raises
        source.state = {"seen_ids": [i.external_id for i in self._items]}
        return self._items


def _items(n=2):
    now = timezone.now()
    return [RawItem(external_id=f"g{i}", url=f"https://ex.org/n/{i}",
                    title=f"Заголовок {i}", text=f"Текст {i}",
                    posted_at=now - timedelta(hours=i)) for i in range(n)]


def test_collect_fanout_creates_posts_and_schedules(monkeypatch):
    sub = SubscriptionFactory()  # infospace-задача + джерело + активна підписка
    monkeypatch.setattr(stages, "get_adapter", lambda k: _StubAdapter(_items(2)))
    assert stages.info_collect_once() is True
    posts = Post.objects.filter(task=sub.task)
    assert posts.count() == 2
    p = posts.first()
    assert p.stage == Post.STAGE_INFO_COLLECTED and p.source_id == sub.source_id
    sub.source.refresh_from_db()
    assert sub.source.consecutive_failures == 0
    assert sub.source.last_ok_at is not None
    assert sub.source.next_poll_at > timezone.now()      # заплановано наперед
    assert sub.source.locked_at is None
    assert sub.source.state["seen_ids"] == ["g0", "g1"]  # watermark збережено


def test_collect_idempotent_no_dupes(monkeypatch):
    sub = SubscriptionFactory()
    monkeypatch.setattr(stages, "get_adapter", lambda k: _StubAdapter(_items(2)))
    stages.info_collect_once()
    sub.source.next_poll_at = timezone.now() - timedelta(seconds=1)  # знову due
    sub.source.save(update_fields=["next_poll_at"])
    stages.info_collect_once()
    assert Post.objects.filter(task=sub.task).count() == 2  # без дублів (unique task,url)


def test_collect_fanout_to_two_tasks(monkeypatch):
    src = SourceFactory()
    t1, t2 = TaskFactory(), TaskFactory()
    SubscriptionFactory(task=t1, source=src)
    SubscriptionFactory(task=t2, source=src)
    monkeypatch.setattr(stages, "get_adapter", lambda k: _StubAdapter(_items(1)))
    stages.info_collect_once()
    assert Post.objects.filter(task=t1).count() == 1
    assert Post.objects.filter(task=t2).count() == 1


def test_collect_failure_backoff(monkeypatch):
    sub = SubscriptionFactory()
    monkeypatch.setattr(stages, "get_adapter",
                        lambda k: _StubAdapter(None, raises=RuntimeError("net down")))
    assert stages.info_collect_once() is True  # воркер живий
    sub.source.refresh_from_db()
    assert sub.source.consecutive_failures == 1
    assert "net down" in sub.source.last_error
    assert sub.source.next_poll_at > timezone.now()
    assert Post.objects.filter(task=sub.task).count() == 0


def test_collect_skips_source_without_active_subscription(monkeypatch):
    SourceFactory()  # джерело без підписок
    monkeypatch.setattr(stages, "get_adapter", lambda k: _StubAdapter(_items(1)))
    assert stages.info_collect_once() is False  # нема роботи


# ------------------------------------------------------------------ screen

def _mk_post(task, **kw):
    d = dict(task=task, stage=Post.STAGE_INFO_COLLECTED,
             url=f"https://ex.org/{timezone.now().timestamp()}-{id(kw)}",
             title="T", text="body", posted_at=timezone.now())
    d.update(kw)
    return Post.objects.create(**d)


def _fake_screen(verdicts):
    async def _run(posts, system, model):
        return {p.id: verdicts.get(p.id) for p in posts}
    return _run


def test_screen_relevant_advances(monkeypatch):
    task = TaskFactory(geo_enabled=False)
    p = _mk_post(task)
    monkeypatch.setattr(stages, "_llm_screen", _fake_screen({p.id: {
        "relevant": True, "signature": "хабар Улан-Уде",
        "summary": "Затримали чиновника.", "reason": "подія", "tags": {}}}))
    assert stages.info_screen_once(task) is True
    p.refresh_from_db()
    assert p.stage == Post.STAGE_INFO_SCREENED and p.is_relevant is True
    assert p.classification["signature"] == "хабар Улан-Уде"


def test_screen_irrelevant_to_done(monkeypatch):
    task = TaskFactory(geo_enabled=False)
    p = _mk_post(task)
    monkeypatch.setattr(stages, "_llm_screen", _fake_screen({p.id: {
        "relevant": False, "signature": "", "summary": ""}}))
    stages.info_screen_once(task)
    p.refresh_from_db()
    assert p.stage == Post.STAGE_DONE and p.is_relevant is False


def test_screen_broken_json_bumps_attempts(monkeypatch):
    task = TaskFactory(geo_enabled=False)
    p = _mk_post(task)
    monkeypatch.setattr(stages, "_llm_screen", _fake_screen({p.id: None}))
    stages.info_screen_once(task)
    p.refresh_from_db()
    assert p.stage == Post.STAGE_INFO_COLLECTED and p.stage_attempts == 1
    assert p.stage_locked_at is None


# ------------------------------------------------------------------ event

def _screened(task, sig, summary, when=None):
    return Post.objects.create(
        task=task, stage=Post.STAGE_INFO_SCREENED,
        url=f"https://ex.org/e/{id(sig)}-{timezone.now().timestamp()}",
        title="T", text="body", posted_at=when or timezone.now(),
        is_relevant=True,
        classification={"signature": sig, "summary": summary, "tags": {}})


def test_event_new_without_candidates(monkeypatch):
    task = TaskFactory(geo_enabled=False)
    p = _screened(task, "хабар мерія", "Чиновника затримали за хабар.")
    # без кандидатів суддя НЕ викликається — впаде, якщо викличе
    monkeypatch.setattr(stages.llm, "query", _boom)
    assert stages.info_event_once(task) is True
    p.refresh_from_db()
    ev = Event.objects.get(task=task)
    assert p.event_id == ev.id and p.stage == Post.STAGE_DONE
    assert ev.review_status == Event.REVIEW_APPROVED
    assert ev.last_post_at == p.posted_at
    assert ev.summary == "Чиновника затримали за хабар."


def test_event_attach_updates_counts_and_summary(monkeypatch):
    task = TaskFactory(geo_enabled=False)
    # наявна жива подія у вікні; summary має fuzzy-збігатись із signature поста,
    # інакше кандидат відсіється порогом FUZZY_FLOOR ще до судді
    ev = Event.objects.create(task=task, event_date=timezone.now().date(),
                              summary="Хабар у мерії Улан-Уде: затримано чиновника.",
                              last_post_at=timezone.now(), post_count=1,
                              review_status=Event.REVIEW_APPROVED)
    p = _screened(task, "хабар мерія Улан-Уде чиновник",
                  "Новий пост про той самий хабар у мерії.")

    async def _judge(messages, **kw):
        import json
        return json.dumps({"verdict": "attach", "event_id": ev.id,
                           "update_summary": True, "new_summary": "Оновлений опис із деталями."})
    monkeypatch.setattr(stages.llm, "query", _judge)

    assert stages.info_event_once(task) is True
    p.refresh_from_db(); ev.refresh_from_db()
    assert p.event_id == ev.id and p.stage == Post.STAGE_DONE
    assert ev.post_count == 1  # приєднано (лише цей пост має FK на подію)
    assert ev.summary == "Оновлений опис із деталями."
    assert Event.objects.filter(task=task).count() == 1  # НЕ створено нову


def test_event_outside_window_makes_new(monkeypatch):
    task = TaskFactory(geo_enabled=False, info_match_window_hours=24)
    old = timezone.now() - timedelta(hours=48)
    Event.objects.create(task=task, event_date=old.date(), summary="Давня подія.",
                         last_post_at=old, review_status=Event.REVIEW_APPROVED)
    p = _screened(task, "нова подія", "Свіжий інцидент.")
    monkeypatch.setattr(stages.llm, "query", _boom)  # поза вікном → без судді
    stages.info_event_once(task)
    assert Event.objects.filter(task=task).count() == 2


async def _boom(*a, **k):
    raise AssertionError("LLM не мав викликатись")


# ------------------------------------------------------------------ retention

def test_retention_deletes_old_irrelevant_only():
    task = TaskFactory(info_retention_days=2)
    old = timezone.now() - timedelta(days=3)
    fresh = timezone.now()
    # ціль: старий, done, нерелевантний, без події
    doomed = _mk_post(task, stage=Post.STAGE_DONE, is_relevant=False, posted_at=old)
    # зберегти: свіжий
    keep_fresh = _mk_post(task, stage=Post.STAGE_DONE, is_relevant=False, posted_at=fresh)
    # зберегти: релевантний
    keep_rel = _mk_post(task, stage=Post.STAGE_DONE, is_relevant=True, posted_at=old)
    # зберегти: приєднаний до події
    ev = Event.objects.create(task=task, summary="x")
    keep_ev = _mk_post(task, stage=Post.STAGE_DONE, is_relevant=False,
                       posted_at=old, event=ev)

    assert stages.info_retention_once(task) is True
    ids = set(Post.objects.filter(task=task).values_list("id", flat=True))
    assert doomed.id not in ids
    assert {keep_fresh.id, keep_rel.id, keep_ev.id} <= ids
    # вдруге — нема що чистити
    assert stages.info_retention_once(task) is False
