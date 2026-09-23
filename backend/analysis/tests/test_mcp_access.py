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
    # глобальні налаштування — лише адмін
    with pytest.raises(ToolError, match="mcp:admin"):
        mcp_api.call("setting_set", {"key": "k", "value": "v"}, who=alice)


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


# --- секрети в налаштуваннях -------------------------------------------------

def test_settings_hide_credentials_from_non_admin(alice):
    from analysis.models import Setting
    Setting.objects.create(key="infospace_proxy_url",
                           value="http://user1:SeCrEtPaSs@proxy.example.com:8080")
    Setting.objects.create(key="digest_report_prompt", value="Ти — редактор дайджесту")
    out = mcp_api.call("settings_list", {}, who=alice)
    assert "SeCrEtPaSs" not in out and "user1:***@proxy.example.com" in out
    assert "Ти — редактор" in out                       # промпти — відкриті

    root = make_actor("root3", McpRole.ADMIN, superuser=True)
    assert "SeCrEtPaSs" in mcp_api.call("settings_list", {}, who=root)


# --- квота TeleZip -------------------------------------------------------------

def test_telezip_quota_counts_and_blocks(alice, monkeypatch):
    """Платні виклики рахуються в аудиті (paid_requests) і впираються в ліміт ролі."""
    from analysis.services.mcp_api import registry, telezip

    @registry.tool("tz_fake", group="telezip")
    def tz_fake():
        registry.charge(1)
        return "ok"
    try:
        alice.user.mcp_role.telezip_daily_limit = 2
        alice.user.mcp_role.save()
        assert mcp_api.call("tz_fake", {}, who=alice) == "ok"
        assert mcp_api.call("tz_fake", {}, who=alice) == "ok"
        with pytest.raises(ToolError, match="ліміт TeleZip вичерпано: 2 з 2"):
            mcp_api.call("tz_fake", {}, who=alice)
        rows = McpAuditLog.objects.filter(user=alice.user, tool="tz_fake").order_by("id")
        assert [r.paid_requests for r in rows] == [1, 1, 0]
        assert rows.last().ok is False
        # ліміт 0 на ролі = дефолт із Setting; локальний stdio — без ліміту
        from analysis.models import Setting
        alice.user.mcp_role.telezip_daily_limit = 0
        alice.user.mcp_role.save()
        Setting.objects.create(key="mcp_telezip_daily_limit", value="3")
        assert mcp_api.call("tz_fake", {}, who=alice) == "ok"
        with pytest.raises(ToolError, match="3 з 3"):
            mcp_api.call("tz_fake", {}, who=alice)
        assert mcp_api.call("tz_fake") == "ok"
    finally:
        registry.TOOLS.pop("tz_fake", None)


def test_telezip_usage_shown_in_role_admin(alice):
    from mcpauth.policy import telezip_used
    McpAuditLog.objects.create(user=alice.user, tool="tz_find", paid_requests=2)
    McpAuditLog.objects.create(user=alice.user, tool="tz_users", paid_requests=1)
    assert telezip_used(alice.user) == 3


# --- проксі й завдання акаунтів — лише свої ---------------------------------

def test_proxies_scoped_to_visible_accounts(alice, bob):
    mine = Proxy.objects.create(proxy_string="mine.example.com:1:u:p")
    free = Proxy.objects.create(proxy_string="free.example.com:2:u:p")
    foreign = Proxy.objects.create(proxy_string="foreign.example.com:3:u:p")
    TelegramAccount.objects.create(name="А", phone_number="+70000000011", user=alice.user, proxy=mine)
    TelegramAccount.objects.create(name="Б", phone_number="+70000000012", user=bob.user, proxy=foreign)
    out = mcp_api.call("proxies_list", {}, who=alice)
    assert "mine.example.com" in out and "free.example.com" in out
    assert "foreign.example.com" not in out
    with pytest.raises(ToolError, match="немає"):
        mcp_api.call("proxy_check", {"ref": str(foreign.id), "repair": False}, who=alice)
    # вільну проксі можна призначити своєму акаунту
    out = mcp_api.call("account_update", {"ref": "+70000000011", "proxy": str(free.id)}, who=alice)
    assert f"#{free.id}" in out


def test_account_jobs_hide_foreign_accounts(alice, bob):
    from accounts.models import WarmUpJob
    a = TelegramAccount.objects.create(name="Алісин", phone_number="+70000000021", user=alice.user)
    b = TelegramAccount.objects.create(name="Бобів", phone_number="+70000000022", user=bob.user)
    WarmUpJob.objects.create(account=a, handles=["x"])
    WarmUpJob.objects.create(account=b, handles=["y"])
    out = mcp_api.call("account_jobs", {"kind": "warm_up"}, who=alice)
    assert "Алісин" in out and "Бобів" not in out


# --- події: лише свої, зміни лише своїх ------------------------------------

def test_events_visible_and_editable_only_within_own_tasks(alice, bob):
    from analysis.models import Event
    mine = TaskFactory(slug="a-ev", owner=alice.user)
    theirs = TaskFactory(slug="b-ev", owner=bob.user)
    e1 = Event.objects.create(task=mine, event_date="2026-09-20", summary="моя подія",
                              review_status=Event.REVIEW_PENDING)
    e2 = Event.objects.create(task=theirs, event_date="2026-09-20", summary="чужа подія",
                              review_status=Event.REVIEW_PENDING)
    out = mcp_api.call("events_list", {"review_status": "pending"}, who=alice)
    assert "моя подія" in out and "чужа подія" not in out
    with pytest.raises(ToolError, match="немає"):
        mcp_api.call("event_update", {"ref": str(e2.id), "review": "approve"}, who=alice)
    mcp_api.call("event_update", {"ref": str(e1.id), "review": "approve"}, who=alice)
    e1.refresh_from_db(); e2.refresh_from_db()
    assert e1.review_status == Event.REVIEW_APPROVED and "alice" in e1.review_notes
    assert e2.review_status == Event.REVIEW_PENDING


def test_source_subscribe_refuses_foreign_task(alice, bob):
    from .factories import SourceFactory
    src = SourceFactory()
    TaskFactory(slug="bob-inf", owner=bob.user)
    with pytest.raises(ToolError, match="не знайдено|немає"):
        mcp_api.call("source_subscribe", {"ref": str(src.id), "task": "bob-inf"}, who=alice)


def test_account_import_owner_follows_actor(alice, monkeypatch):
    """Імпорт — це створення (mcp:create): оператору зась, аналітик додає СОБІ;
    спільний — лише суперюзер."""
    from accounts.services import tdata_import
    monkeypatch.setattr(tdata_import, "convert_sqlite_to_string_session", lambda p: "1BVtsOK0Bu_STRSESSION")
    meta = '{"phone": "79990000001", "app_id": 1, "app_hash": "h", "device": "PC"}'
    blob = "SQLite format 3\x00" + "x" * 32
    import base64
    b64 = base64.b64encode(blob.encode()).decode()
    with pytest.raises(ToolError, match="mcp:create"):
        mcp_api.call("account_import", {"meta_json": meta, "session_b64": b64}, who=alice)
    analyst = make_actor("ann2", McpRole.ANALYST)
    with pytest.raises(ToolError, match="суперюзер"):
        mcp_api.call("account_import", {"meta_json": meta, "session_b64": b64, "shared": True},
                     who=analyst)
    out = mcp_api.call("account_import", {"meta_json": meta, "session_b64": b64, "tags": "нові"},
                       who=analyst)
    acc = TelegramAccount.objects.get(phone_number="+79990000001")
    assert acc.user == analyst.user and acc.is_authenticated and acc.session_string == "1BVtsOK0Bu_STRSESSION"
    assert "STRSESSION" not in out and "+79990000001" not in out     # секрети не в чаті
    assert [t.name for t in acc.tags.all()] == ["нові"]


# --- просунутий аналітик: створює своє, не бачить чужого ---------------------

def test_operator_cannot_create_but_analyst_can(alice):
    analyst = make_actor("ann", McpRole.ANALYST)
    with pytest.raises(ToolError, match="mcp:create"):
        mcp_api.call("task_create", {"slug": "op-task", "name": "х"}, who=alice)
    with pytest.raises(ToolError, match="mcp:create"):
        mcp_api.call("channel_add", {"url": "@somechan"}, who=alice)
    with pytest.raises(ToolError, match="mcp:create"):
        mcp_api.call("source_add", {"url": "https://example.org/rss.xml"}, who=alice)
    out = mcp_api.call("task_create", {"slug": "ann-task", "name": "Дослідження Анни",
                                       "pipeline": "infospace", "languages": "ru, uk"},
                       who=analyst)
    t = AnalysisTask.objects.get(slug="ann-task")
    assert t.owner == analyst.user and t.languages == ["ru", "uk"] and "створена" in out
    # користується своїм: бачить, править, підписує джерело; чужого — ні
    assert "ann-task" in mcp_api.call("tasks_list", {}, who=analyst)
    assert "ann-task" not in mcp_api.call("tasks_list", {}, who=alice)
    mcp_api.call("source_add", {"url": "https://example.org/rss.xml", "task": "ann-task"}, who=analyst)
    assert t.source_subscriptions.filter(is_active=True).count() == 1
    with pytest.raises(ToolError, match="mcp:admin"):
        mcp_api.call("setting_set", {"key": "k", "value": "v"}, who=analyst)
    with pytest.raises(ToolError, match="уже є"):
        mcp_api.call("task_create", {"slug": "ann-task", "name": "дубль"}, who=analyst)
    with pytest.raises(ToolError, match="slug"):
        mcp_api.call("task_create", {"slug": "Погано!", "name": "x"}, who=analyst)
    with pytest.raises(ToolError, match="pipeline"):
        mcp_api.call("task_create", {"slug": "ok-slug", "name": "x", "pipeline": "bogus"}, who=analyst)


def test_publish_configs_are_private_per_owner(alice, bob):
    from analysis.models import PublishConfig, PublishedEvent, Event
    mine = PublishConfig.objects.create(name="Алісин канал", chat_id="-1", owner=alice.user)
    theirs = PublishConfig.objects.create(name="Бобів канал", chat_id="-2", owner=bob.user)
    t = TaskFactory(slug="b-pub", owner=bob.user)
    ev = Event.objects.create(task=t, event_date="2026-09-20", summary="чуже")
    PublishedEvent.objects.create(config=theirs, event=ev, status="published", post_text="чужий пост")
    with pytest.raises(ToolError, match="немає"):
        mcp_api.call("publish_config_update", {"ref": str(theirs.id), "is_active": True}, who=alice)
    assert "чужий пост" not in mcp_api.call("published_list", {}, who=alice)
    assert "Алісин" in mcp_api.call("publish_config_show", {"ref": str(mine.id)}, who=alice)
    # створення — mcp:create (аналітик), оператору — ні
    with pytest.raises(ToolError, match="mcp:create"):
        mcp_api.call("publish_config_create", {"name": "x", "chat_id": "-3"}, who=alice)
    analyst = make_actor("ann3", McpRole.ANALYST)
    mcp_api.call("publish_config_create", {"name": "Аннин", "chat_id": "-3"}, who=analyst)
    assert PublishConfig.objects.get(name="Аннин").owner == analyst.user
