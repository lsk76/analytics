import json

import pytest
from django.contrib.auth.models import Permission, User

from analysis.models import Channel, Source, SourceSubscription
from analysis.services import mcp_api
from analysis.services.mcp_api import Actor, ToolError
from .factories import SourceFactory, SubscriptionFactory, TaskFactory

pytestmark = pytest.mark.django_db


def batch(name, items, **kwargs):
    return mcp_api.call(name, {"items": json.dumps(items)}, **kwargs)


def who(name, permissions):
    user = User.objects.create_user(name)
    user.user_permissions.set(Permission.objects.filter(
        content_type__app_label="analysis", codename__in=permissions))
    return Actor(user=user, scopes=["mcp:read", "mcp:write", "mcp:create"])


def test_add_batch_idempotent_and_subscribes_each_task():
    a = TaskFactory(slug="news-a")
    b = TaskFactory(slug="news-b")
    items = [{"url": "https://example.org/feed", "kind": "rss", "task": a.slug},
             {"url": "@alpha", "task": b.slug, "name": "Альфа"}]
    for _ in range(2):
        assert "успішно 2, помилок 0" in batch("sources_add_batch", items)
    assert Source.objects.count() == Channel.objects.count() == 2
    assert SourceSubscription.objects.count() == 2
    assert Source.objects.get(channel__username="alpha").subscriptions.get().task == b


def test_add_batch_rolls_back_channel_source_and_subscription(monkeypatch):
    original = mcp_api.TOOLS["source_add"].fn
    task = TaskFactory()

    def failure(**payload):
        result = original(**payload)
        if payload["url"] == "@bad":
            raise ToolError("помилка після створення підписки")
        return result

    monkeypatch.setattr(mcp_api.TOOLS["source_add"], "fn", failure)
    out = batch("sources_add_batch", [
        {"url": "@bad", "task": task.slug}, {"url": "@good", "task": task.slug}])
    assert "успішно 1, помилок 1" in out
    assert list(Channel.objects.values_list("username", flat=True)) == ["good"]
    assert Source.objects.count() == SourceSubscription.objects.count() == 1


def test_update_individual_changes_missing_and_boolean_validation():
    a, b = SourceFactory(), SourceFactory()
    out = batch("sources_update_batch", [
        {"ref": f"#{a.id}", "is_active": False, "poll_interval_sec": 30},
        {"ref": "#999999", "poll_now": True},
        {"ref": f"#{b.id}", "is_active": "false"},
        {"ref": f"#{b.id}", "poll_interval_sec": 900, "poll_now": True},
    ])
    assert "успішно 2, помилок 2" in out
    a.refresh_from_db(); b.refresh_from_db()
    assert not a.is_active and a.poll_interval_sec == 60
    assert b.is_active and b.poll_interval_sec == 900


def test_update_cursor_reset_requires_confirmation_in_batch():
    a, b = SourceFactory(poll_cursor={"last_id": 10}), SourceFactory()
    out = batch("sources_update_batch", [
        {"ref": f"#{a.id}", "reset_cursor": True},
        {"ref": f"#{b.id}", "is_active": False}])
    assert "confirm=true" in out and "успішно 1, помилок 1" in out
    a.refresh_from_db()
    assert a.poll_cursor == {"last_id": 10}
    batch("sources_update_batch", [
        {"ref": f"#{a.id}", "reset_cursor": True, "confirm": True},
        {"ref": f"#{b.id}", "is_active": True}])
    a.refresh_from_db()
    assert a.poll_cursor == {}


def test_get_batch_order_ambiguity_missing_and_url_with_comma():
    a = SourceFactory(name="Спільна назва один", url="https://a.org/feed?q=a,b")
    b = SourceFactory(name="Спільна назва два")
    out = mcp_api.call("sources_get_batch", {"refs": json.dumps([
        f"#{b.id}", a.url, "Спільна назва", "#999999"])})
    assert "знайдено 2, помилок 2" in out
    assert out.index(f"#{b.id} ·") < out.index(f"#{a.id} ·")
    assert "неоднозначне" in out and f"/admin/analysis/source/{a.id}/change/" in out
    out = batch("sources_update_batch", [{"ref": a.url, "is_active": False}])
    assert "успішно 1, помилок 0" in out
    assert "знайдено 2, помилок 0" in mcp_api.call(
        "sources_get_batch", {"refs": f"#{a.id}, #{b.id}"})


def test_visibility_foreign_updates_and_hidden_subscriptions():
    actor = who("owner", ["view_source", "change_source", "add_source"])
    mine = TaskFactory(owner=actor.user, slug="my-study")
    foreign = TaskFactory(slug="foreign-study")
    shared = SourceFactory()
    hidden = SourceFactory()
    SubscriptionFactory(source=shared, task=mine, is_active=False)
    SubscriptionFactory(source=shared, task=foreign)
    SubscriptionFactory(source=hidden, task=foreign)
    out = mcp_api.call("sources_get_batch", {"refs": f"#{shared.id}, #{hidden.id}"}, who=actor)
    assert "знайдено 1, помилок 1" in out
    assert "my-study" in out and "foreign-study" not in out
    out = batch("sources_update_batch", [
        {"ref": f"#{hidden.id}", "is_active": False}], who=actor)
    assert "помилок 1" in out
    hidden.refresh_from_db()
    assert hidden.is_active
    out = batch("sources_add_batch", [{"url": hidden.url, "poll_interval_sec": 90}], who=actor)
    assert "помилок 1" in out
    hidden.refresh_from_db()
    assert hidden.poll_interval_sec != 90
    # Чужа задача не може отримати підписку через add.
    out = batch("sources_add_batch", [{"url": "@new", "task": foreign.slug}], who=actor)
    assert "помилок 1" in out
    assert not Channel.objects.filter(username="new").exists()


@pytest.mark.parametrize("name", ["sources_add_batch", "sources_update_batch"])
def test_readonly_and_django_permissions(name, monkeypatch):
    monkeypatch.setenv("MCP_READONLY", "1")
    with pytest.raises(ToolError, match="лише-читання"):
        batch(name, [])
    monkeypatch.delenv("MCP_READONLY")
    actor = who("reader", ["view_source"])
    with pytest.raises(ToolError, match="бракує права в адмінці"):
        batch(name, [], who=actor)


@pytest.mark.parametrize("name,param", [
    ("sources_add_batch", "items"), ("sources_update_batch", "items"),
    ("sources_get_batch", "refs")])
@pytest.mark.parametrize("value", ["[]", "{}", "[", json.dumps([{}] * 101)])
def test_invalid_envelopes(name, param, value):
    with pytest.raises(ToolError):
        mcp_api.call(name, {param: value})


def test_add_100_repeated_entries_and_null_update():
    assert "успішно 100, помилок 0" in batch(
        "sources_add_batch", [{"url": "https://example.org/feed"}] * 100)
    src = Source.objects.get()
    batch("sources_update_batch", [{"ref": f"#{src.id}", "is_active": None}])
    src.refresh_from_db()
    assert src.is_active
