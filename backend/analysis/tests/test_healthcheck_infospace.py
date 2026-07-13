"""Самоперевірка якості: канарки + стадія info_healthcheck (тихий злам скрапера)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from analysis.models import Source
from analysis.services.infospace import stages
from analysis.services.infospace.adapters.base import RateLimited, RawItem

from .factories import SourceFactory, SubscriptionFactory

pytestmark = pytest.mark.django_db


def _item(text, title="T"):
    return RawItem(external_id="1", url="https://ex.org/1", title=title, text=text)


class _Kind:
    def __init__(self, kind):
        self.kind = kind

_WEB = _Kind(Source.KIND_WEB)
_RSS = _Kind(Source.KIND_RSS)


# ------------------------------------------------------------ канарки

def test_evaluate_quality_empty_is_suspect():
    ok, note = stages.evaluate_quality(_WEB, [])
    assert ok is False and "0 елементів" in note


def test_evaluate_web_all_thin_is_suspect():
    # web: усі без тіла статті → extraction зламано
    ok, note = stages.evaluate_quality(_WEB, [_item("коротко"), _item("теж")])
    assert ok is False and "extraction" in note


def test_evaluate_web_healthy_with_body():
    ok, note = stages.evaluate_quality(_WEB, [_item("x" * 200), _item("коротко")])
    assert ok is True and note == ""


def test_evaluate_rss_short_text_is_ok():
    # rss: заголовкові стрічки з коротким text — НОРМА (не фолс-позитив)
    ok, note = stages.evaluate_quality(_RSS, [_item("короткий анонс", title="Заголовок")])
    assert ok is True and note == ""


def test_evaluate_rss_all_empty_is_suspect():
    ok, note = stages.evaluate_quality(_RSS, [_item("", title="")])
    assert ok is False and "без контенту" in note


# ------------------------------------------------------------ стадія

class _Stub:
    def __init__(self, items=None, raises=None):
        self._items, self._raises = items, raises

    def fetch(self, source):
        if self._raises:
            raise self._raises
        source.poll_cursor = {"seen_ids": ["mutated"]}   # адаптер мутує poll_cursor
        return self._items


def _web_source_with_sub(**kw):
    sub = SubscriptionFactory(source__kind=Source.KIND_WEB)
    if kw:
        Source.objects.filter(id=sub.source_id).update(**kw)
        sub.source.refresh_from_db()
    return sub.source


def test_healthcheck_healthy_sets_quality_ok(monkeypatch):
    src = _web_source_with_sub(poll_cursor={"seen_ids": ["real"]})
    monkeypatch.setattr(stages, "get_adapter", lambda k: _Stub([_item("x" * 200)]))
    assert stages.info_healthcheck_once() is True
    src.refresh_from_db()
    assert src.quality_ok is True
    assert src.last_healthcheck_at is not None


def test_healthcheck_does_not_touch_real_watermark(monkeypatch):
    """Структурна гарантія: адаптер бачить ПОРОЖНІЙ poll_cursor (backfill на копії),
    а реальний Source.poll_cursor у БД лишається недоторканим — навіть якщо адаптер
    його мутує. Ловить регресію, якщо healthcheck почне fetch на реальному Source."""
    src = _web_source_with_sub(poll_cursor={"seen_ids": ["real"]})
    seen = {}

    class _MutatingStub:
        def fetch(self, source):
            seen["adapter_saw"] = dict(source.poll_cursor)      # має бути {} (backfill)
            source.poll_cursor = {"seen_ids": ["MUTATED"]}       # мутуємо (як реальний адаптер)
            return [_item("x" * 200)]
    monkeypatch.setattr(stages, "get_adapter", lambda k: _MutatingStub())
    stages.info_healthcheck_once()
    src.refresh_from_db()
    assert seen["adapter_saw"] == {}                       # адаптер отримав відчеплену копію
    assert src.poll_cursor == {"seen_ids": ["real"]}             # реальний watermark цілий


def test_healthcheck_empty_flags_suspect(monkeypatch):
    src = _web_source_with_sub()
    monkeypatch.setattr(stages, "get_adapter", lambda k: _Stub([]))
    stages.info_healthcheck_once()
    src.refresh_from_db()
    assert src.quality_ok is False and "0 елементів" in src.quality_note


def test_healthcheck_exception_flags_suspect(monkeypatch):
    src = _web_source_with_sub()
    monkeypatch.setattr(stages, "get_adapter",
                        lambda k: _Stub(raises=RuntimeError("boom")))
    stages.info_healthcheck_once()
    src.refresh_from_db()
    assert src.quality_ok is False and "boom" in src.quality_note


def test_healthcheck_ratelimited_is_no_quality_change(monkeypatch):
    src = _web_source_with_sub(quality_ok=True)
    monkeypatch.setattr(stages, "get_adapter",
                        lambda k: _Stub(raises=RateLimited(60)))
    stages.info_healthcheck_once()
    src.refresh_from_db()
    assert src.quality_ok is True   # ліміт — не якісна проблема


def test_healthcheck_skips_telegram_kind(monkeypatch):
    # telegram не в HEALTHCHECK_KINDS → нема роботи
    SubscriptionFactory(source__kind=Source.KIND_TELEGRAM)
    monkeypatch.setattr(stages, "get_adapter", lambda k: _Stub([_item("x" * 200)]))
    assert stages.info_healthcheck_once() is False


def test_healthcheck_skips_recently_checked(monkeypatch):
    _web_source_with_sub(last_healthcheck_at=timezone.now())  # щойно перевірено
    monkeypatch.setattr(stages, "get_adapter", lambda k: _Stub([_item("x" * 200)]))
    assert stages.info_healthcheck_once() is False   # не due (< 24год)


def test_healthcheck_claims_due_source(monkeypatch):
    _web_source_with_sub(last_healthcheck_at=timezone.now() - timedelta(hours=25))
    monkeypatch.setattr(stages, "get_adapter", lambda k: _Stub([_item("x" * 200)]))
    assert stages.info_healthcheck_once() is True    # старіше 24год → due


def test_admin_healthcheck_ratelimited_keeps_quality(monkeypatch):
    """Admin-дія «Самоперевірка зараз» на FloodWait (RateLimited) НЕ позначає
    злам якості (узгоджено з taskless-шляхом)."""
    from django.contrib import admin as dj_admin
    from analysis.admin import SourceAdmin

    src = _web_source_with_sub(quality_ok=True)

    def _rl(source):
        raise RateLimited(60)
    monkeypatch.setattr(stages, "probe_fetch", _rl)

    adm = SourceAdmin(Source, dj_admin.site)
    msgs = []
    adm.message_user = lambda req, m, level=None: msgs.append(m)
    adm.healthcheck_now(None, Source.objects.filter(id=src.id))

    src.refresh_from_db()
    assert src.quality_ok is True                 # ліміт не змінив якість
    assert any("ліміт" in m for m in msgs)
