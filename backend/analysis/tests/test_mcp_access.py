"""Розмежування доступу до MCP: ролі, видимість даних, маскування, аудит.

Це найважливіші тести шару: мережевий MCP не має стати обходом адмінки.
Правило просте — що користувач бачить в адмінці, те саме бачить через MCP.
"""
import pytest
from django.contrib.auth.models import User

from accounts.models import Proxy, TelegramAccount
from analysis.models import AnalysisTask, MonitorChat, Channel
from analysis.services import mcp_api
from analysis.services.mcp_api import Actor
from analysis.services.mcp_api.registry import ToolError
from mcpauth.models import McpAuditLog, McpRole

from .factories import TaskFactory

pytestmark = pytest.mark.django_db


def make_actor(username, role=McpRole.OPERATOR, superuser=False):
    user = User.objects.create_user(username, is_superuser=superuser,
                                    is_staff=True, password="x")
    McpRole.objects.create(user=user, role=role)
    return Actor(user=user, scopes=McpRole.SCOPES[role], role=role)


@pytest.fixture
def alice():
    return make_actor("alice")


@pytest.fixture
def bob():
    return make_actor("bob")


# --- ролі -------------------------------------------------------------------

def test_reader_cannot_change_anything():
    reader = make_actor("reader", McpRole.READER)
    TaskFactory(slug="t1", owner=reader.user)
    assert "t1" in mcp_api.call("tasks_list", {}, who=reader)      # читати можна
    with pytest.raises(ToolError, match="бракує прав"):
        mcp_api.call("task_update", {"ref": "t1", "telezip_query": "щось"}, who=reader)


def test_operator_can_write_but_not_touch_global_config(alice):
    TaskFactory(slug="t2", owner=alice.user)
    out = mcp_api.call("task_update", {"ref": "t2", "chunk_days": 5}, who=alice)
    assert "чанк=5 дн" in out
    for tool, payload in [("setting_set", {"key": "k", "value": "v"}),
                          ("tz_slots_set", {"count": 2}),
                          ("tz_probe", {"endpoint": "/v4/stats"})]:
        with pytest.raises(ToolError, match="mcp:admin"):
            mcp_api.call(tool, payload, who=alice)


def test_admin_role_passes_admin_gate():
    admin = make_actor("adm", McpRole.ADMIN)
    out = mcp_api.call("setting_set", {"key": "test_key", "value": "1"}, who=admin)
    assert "створено" in out


# --- видимість --------------------------------------------------------------

def test_user_sees_only_own_tasks(alice, bob):
    TaskFactory(slug="alice-task", owner=alice.user)
    TaskFactory(slug="bob-task", owner=bob.user)
    out = mcp_api.call("tasks_list", {}, who=alice)
    assert "alice-task" in out and "bob-task" not in out


def test_foreign_task_is_not_reachable_even_by_id(alice, bob):
    foreign = TaskFactory(slug="bob-task", owner=bob.user)
    # ані за id, ані за slug — і повідомлення не зізнається, що задача існує
    with pytest.raises(ToolError, match="немає|не знайдено"):
        mcp_api.call("task_show", {"ref": str(foreign.id)}, who=alice)
    with pytest.raises(ToolError, match="немає|не знайдено"):
        mcp_api.call("run_create", {"task": "bob-task", "date_from": "2026-01-01",
                                    "date_to": "2026-01-02"}, who=alice)


def test_superuser_sees_everything(bob):
    TaskFactory(slug="bob-task", owner=bob.user)
    root = make_actor("root", McpRole.ADMIN, superuser=True)
    assert "bob-task" in mcp_api.call("tasks_list", {}, who=root)


def test_accounts_follow_admin_visibility(alice, bob):
    TelegramAccount.objects.create(name="Алісин", phone_number="+70000000001",
                                   user=alice.user)
    TelegramAccount.objects.create(name="Бобів", phone_number="+70000000002",
                                   user=bob.user)
    TelegramAccount.objects.create(name="Спільний", phone_number="+70000000003")
    out = mcp_api.call("accounts_list", {}, who=alice)
    assert "Алісин" in out and "Спільний" in out and "Бобів" not in out


def test_foreign_chat_cannot_be_edited(alice, bob):
    task = TaskFactory(slug="bob-mon", owner=bob.user,
                       pipeline=AnalysisTask.PIPELINE_MONITOR)
    chat = MonitorChat.objects.create(
        task=task, channel=Channel.objects.create(username="bobchat", title="ч"))
    with pytest.raises(ToolError, match="немає|не знайдено"):
        mcp_api.call("chat_update", {"chat": str(chat.id), "is_active": False},
                     who=alice)
    chat.refresh_from_db()
    assert chat.is_active is True


# --- секрети ----------------------------------------------------------------

def test_proxy_password_hidden_from_non_admin(alice):
    proxy = Proxy.objects.create(proxy_string="ultra.example.com:44445:user1:SeCrEtPaSs")
    TelegramAccount.objects.create(name="А", phone_number="+70000000009",
                                   user=alice.user, proxy=proxy)
    out = mcp_api.call("account_show", {"ref": "+70000000009"}, who=alice)
    assert "ultra.example.com:44445:user1" in out      # яка саме проксі — видно
    assert "SeCrEtPaSs" not in out and "***" in out    # пароль — ні

    root = make_actor("root2", McpRole.ADMIN, superuser=True)
    assert "SeCrEtPaSs" in mcp_api.call("proxies_list", {}, who=root)


def test_local_stdio_actor_keeps_full_access():
    """Локальний режим на ноутбуці власника лишається без обмежень."""
    TaskFactory(slug="any-task")
    assert "any-task" in mcp_api.call("tasks_list")     # без who= — Actor.local()


# --- аудит ------------------------------------------------------------------

def test_every_call_leaves_a_trace(alice):
    TaskFactory(slug="a-task", owner=alice.user)
    mcp_api.call("tasks_list", {"active_only": True}, who=alice)
    row = McpAuditLog.objects.get()
    assert row.user == alice.user and row.tool == "tasks_list" and row.ok
    assert row.payload == {"active_only": True} and row.role == McpRole.OPERATOR


def test_denied_calls_are_logged_too(alice):
    with pytest.raises(ToolError):
        mcp_api.call("setting_set", {"key": "k", "value": "v"}, who=alice)
    row = McpAuditLog.objects.get()
    assert row.tool == "setting_set" and row.ok is False and "mcp:admin" in row.error


def test_local_calls_are_not_logged():
    mcp_api.call("tasks_list")
    assert McpAuditLog.objects.count() == 0
