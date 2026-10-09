import json

import pytest
from django.contrib.auth.models import Permission, User

from analysis.models import Event, PublishConfig, PublishedEvent
from analysis.services import mcp_api
from analysis.services.mcp_api import Actor, ToolError
from mcpauth.models import McpAuditLog
from .factories import TaskFactory

pytestmark = pytest.mark.django_db


def publication(owner=None, **kwargs):
    task = TaskFactory(owner=owner)
    cfg = PublishConfig.objects.create(name="Профіль", owner=owner, task=task, chat_id="@test")
    ev = Event.objects.create(task=task, event_date="2026-10-09", summary="Подія")
    return PublishedEvent.objects.create(config=cfg, event=ev, **kwargs)


def test_preview_no_changes_and_confirm_preserves_events_and_configs():
    a = publication(status="published", tg_message_id=42)
    b = publication(status="failed", error="Збій")
    refs = f"#{a.id}, #{b.id}, #{a.id}"
    out = mcp_api.call("published_requeue", {"ref": refs})
    assert "2 записів (перегляд, без змін)" in out
    assert "confirm=true" in out and "Telegram-пости залишаються" in out
    assert PublishedEvent.objects.count() == 2
    out = mcp_api.call("published_requeue", {"ref": refs, "confirm": True})
    assert "Перечерговано 2" in out
    assert not PublishedEvent.objects.exists()
    assert Event.objects.count() == PublishConfig.objects.count() == 2


def test_missing_id_cancels_all():
    p = publication()
    with pytest.raises(ToolError, match="нічого не змінено"):
        mcp_api.call("published_requeue", {"ref": f"#{p.id}, #999999", "confirm": True})
    assert PublishedEvent.objects.filter(pk=p.pk).exists()


@pytest.mark.parametrize("ref", ["[]", "{}", "[", "x", '[12]', json.dumps(["1"] * 101)])
def test_invalid_refs(ref):
    with pytest.raises(ToolError):
        mcp_api.call("published_requeue", {"ref": ref, "confirm": True})


def test_ownership_permissions_and_audit():
    user = User.objects.create_user("publisher")
    actor = Actor(user=user, scopes=["mcp:read", "mcp:write"])
    mine = publication(owner=user)
    foreign = publication()
    with pytest.raises(ToolError, match="бракує права в адмінці"):
        mcp_api.call("published_requeue", {"ref": str(mine.id)}, who=actor)
    user.user_permissions.add(Permission.objects.get(
        content_type__app_label="analysis", codename="delete_publishedevent"))
    actor = Actor(user=User.objects.get(pk=user.pk), scopes=["mcp:read", "mcp:write"])
    with pytest.raises(ToolError, match="недоступні"):
        mcp_api.call("published_requeue", {"ref": f"{mine.id}, {foreign.id}", "confirm": True}, who=actor)
    assert PublishedEvent.objects.count() == 2
    out = mcp_api.call("published_requeue", {"ref": json.dumps([str(mine.id)]), "confirm": True}, who=actor)
    assert "Перечерговано 1" in out
    assert PublishedEvent.objects.get().pk == foreign.pk
    log = McpAuditLog.objects.filter(tool="published_requeue", ok=True).get()
    assert log.user_id == user.pk and log.payload["confirm"] is True


def test_readonly(monkeypatch):
    monkeypatch.setenv("MCP_READONLY", "1")
    with pytest.raises(ToolError, match="лише-читання"):
        mcp_api.call("published_requeue", {"ref": "1", "confirm": True})
