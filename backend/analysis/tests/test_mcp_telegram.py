"""MCP-шар tg_*: вибір акаунта (лише видимі), скоупи, трансляція помилок gateway,
рендер. Сам gateway підмінено: registry.get → фейк з .tg()/.join()."""
import pytest
from django.contrib.auth.models import User

from accounts.models import TelegramAccount
from accounts.services import registry as acc_registry
from accounts.services.managed import AccountUnavailable, RateLimited
from analysis.services import mcp_api
from analysis.services.mcp_api import Actor
from analysis.services.mcp_api.registry import ToolError
from mcpauth.models import McpRole

pytestmark = pytest.mark.django_db


class FakeManaged:
    def __init__(self, account_id, box):
        self.id, self.box = account_id, box

    def tg(self, op, **kw):
        self.box.append((self.id, op, kw))
        if isinstance(self.box_result, Exception):
            raise self.box_result
        return self.box_result

    box_result = {"ok": True, "message_id": 9}

    def join(self, handles):
        return {"ok": True, "joined": handles, "failed": [], "flood_wait": None}


@pytest.fixture
def gw(monkeypatch):
    calls = []
    fake = {"result": {"ok": True, "message_id": 9}}

    def get(account_id):
        m = FakeManaged(account_id, calls)
        m.box_result = fake["result"]
        return m
    monkeypatch.setattr(acc_registry, "get", get)
    fake["calls"] = calls
    return fake


def make_actor(name, cap=""):
    """Скоупи — з прав Django (усі), `cap` звужує (McpRole.max_scope)."""
    from django.contrib.auth.models import Permission
    from mcpauth.policy import scope_summary, scopes_for
    user = User.objects.create_user(name, is_staff=True, password="x")
    user.user_permissions.set(Permission.objects.all())
    McpRole.objects.create(user=user, max_scope=cap)
    user = User.objects.get(pk=user.pk)
    scopes = scopes_for(user, cap)
    return Actor(user=user, scopes=scopes, role=scope_summary(scopes))


@pytest.fixture
def acc():
    return TelegramAccount.objects.create(name="Мій", phone_number="+70000000031",
                                         is_active=True, is_authenticated=True)


def test_auto_picks_first_available_account(gw, acc):
    TelegramAccount.objects.create(name="Вимкнений", phone_number="+70000000030", is_active=False,
                                   is_authenticated=True)
    gw["result"] = [{"id": 1, "kind": "channel", "name": "К", "username": "k", "unread": 2,
                     "pinned": False, "last": "2026-09-23", "last_text": "hi"}]
    out = mcp_api.call("tg_dialogs", {})
    assert f"акаунт #{acc.id} Мій" in out and "@k" in out
    assert gw["calls"][0][0] == acc.id and gw["calls"][0][1] == "tg_dialogs"


def test_no_available_account_is_clear_error(gw):
    with pytest.raises(ToolError, match="немає доступного акаунта"):
        mcp_api.call("tg_dialogs", {})


def test_foreign_account_is_invisible(gw, acc):
    alice = make_actor("alice", cap="mcp:write")
    bob = User.objects.create_user("bob")
    acc.user = bob
    acc.save()
    with pytest.raises(ToolError, match="немає"):
        mcp_api.call("tg_history", {"chat": "@c", "account": str(acc.id)}, who=alice)


def test_send_requires_admin_scope(gw, acc):
    analyst = make_actor("ann", cap="mcp:create")
    with pytest.raises(ToolError, match="mcp:admin"):
        mcp_api.call("tg_send", {"chat": "@c", "text": "hi"}, who=analyst)
    # читання й правки — можна
    gw["result"] = []
    assert "порожньо" in mcp_api.call("tg_history", {"chat": "@c"}, who=analyst)
    gw["result"] = {"ok": True}
    assert "закріплено" in mcp_api.call("tg_pin", {"chat": "@c", "msg_id": 3}, who=analyst)
    root = make_actor("root")
    gw["result"] = {"ok": True, "message_id": 9}
    assert "надіслано #9" in mcp_api.call("tg_send", {"chat": "@c", "text": "hi"}, who=root)
    assert gw["calls"][-1][2]["text"] == "hi" and gw["calls"][-1][2]["parse_mode"] == "md"


def test_gateway_errors_become_tool_errors(gw, acc):
    gw["result"] = RateLimited(120, "flood", "flood")
    with pytest.raises(ToolError, match="пауза 120с"):
        mcp_api.call("tg_history", {"chat": "@c"})
    gw["result"] = AccountUnavailable("banned", "бан")
    with pytest.raises(ToolError, match="недоступний \\(banned\\)"):
        mcp_api.call("tg_history", {"chat": "@c"})
    gw["result"] = {"ok": False, "error": "у повідомлення немає кнопок"}
    with pytest.raises(ToolError, match="немає кнопок"):
        mcp_api.call("tg_click", {"chat": "@c", "msg_id": 1})


def test_argument_parsing(gw, acc):
    gw["result"] = {"ok": True, "deleted": 2}
    assert "видалено 2" in mcp_api.call("tg_delete", {"chat": "@c", "ids": "5, 6"})
    assert gw["calls"][-1][2]["ids"] == [5, 6]
    with pytest.raises(ToolError, match="id повідомлень"):
        mcp_api.call("tg_delete", {"chat": "@c", "ids": "abc"})
    with pytest.raises(ToolError, match="2–10 варіантів"):
        mcp_api.call("tg_poll", {"chat": "@c", "question": "?", "options": "один"})
    with pytest.raises(ToolError, match="рівно одне"):
        mcp_api.call("tg_send_file", {"chat": "@c"})
    gw["result"] = {"ok": True}
    mcp_api.call("tg_admin", {"chat": "@c", "user": "@u", "rights": "ban_users, pin_messages"})
    assert gw["calls"][-1][2]["rights"] == {"ban_users": True, "pin_messages": True}
    out = mcp_api.call("tg_join", {"chats": "@a, t.me/b"})
    assert "@a, t.me/b" in out


def test_messages_render_buttons_and_full_text(gw, acc):
    gw["result"] = [{"id": 1, "date": "2026-09-23T10:00:00+00:00", "sender": "Бот", "sender_id": 3,
                     "text": "довгий " * 50, "buttons": [{"row": 0, "col": 0, "text": "Так", "url": None, "data": "y"}],
                     "reactions": {"👍": 2}, "views": 1, "forwards": 0}]
    out = mcp_api.call("tg_messages", {"chat": "@c", "ids": "1"})
    assert "[0,0] Так" in out and ("довгий " * 50).strip() in out and "👍" in out
