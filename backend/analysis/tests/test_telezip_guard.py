"""Запобіжники платного TeleZip: згода людини на кожен виклик і каскад
«довідник → резолв акаунтом → TeleZip».

Гроші за пошук платить власник, а не той, хто питає, тож модель не має права
витрачати їх сама, а конкретні канали спершу шукаються безкоштовно.
"""
import pytest
from django.contrib.auth.models import Permission, User

from accounts.models import Proxy, TelegramAccount
from analysis.models import Channel, Setting
from analysis.services import mcp_api
from analysis.services.mcp_api import Actor, registry
from analysis.services.mcp_api.registry import NeedsConfirmation, ToolError
from mcpauth.models import McpPendingCall, McpRole

pytestmark = pytest.mark.django_db


@pytest.fixture
def who():
    user = User.objects.create_user("tz", is_staff=True, password="x")
    user.user_permissions.set(Permission.objects.all())
    McpRole.objects.create(user=user)
    user = User.objects.get(pk=user.pk)
    from mcpauth.policy import scopes_for
    return Actor(user=user, scopes=scopes_for(user), role="rwca")


@pytest.fixture
def probe():
    """Інструмент-подразник: витрачає гроші лише після згоди."""
    @registry.tool("tz_probe", group="telezip", params={"confirm": "код згоди", "q": "запит"})
    def tz_probe(q: str = "", confirm: str = ""):
        """Тестовий платний пошук."""
        registry.require_confirmation("tz_probe", {"q": q, "confirm": confirm}, what=f"q={q}")
        registry.charge(1)
        return f"ЗНАЙДЕНО за {q}"
    try:
        yield
    finally:
        registry.TOOLS.pop("tz_probe", None)


def _code(text):
    import re
    m = re.search(r'confirm="([^"]+)"', text)
    assert m, f"у відповіді немає коду згоди:\n{text}"
    return m.group(1)


# --- згода людини ---------------------------------------------------------------

def test_first_call_asks_for_consent_and_spends_nothing(who, probe):
    with pytest.raises(NeedsConfirmation) as e:
        mcp_api.call("tz_probe", {"q": "мігранти"}, who=who)
    text = str(e.value)
    assert "ПОТРІБНА ЗГОДА ЛЮДИНИ" in text and "≈$0.10" in text and "q=мігранти" in text
    assert "Сама собі згоду не вигадуй" in text
    row = McpPendingCall.objects.get()
    assert row.tool == "tz_probe" and row.user == who.user and row.used_at is None
    # платного виклику не було: у квоті нічого не списано
    from mcpauth.policy import telezip_used
    assert telezip_used(who.user) == 0


def test_consent_code_unlocks_exactly_these_parameters(who, probe):
    with pytest.raises(NeedsConfirmation) as e:
        mcp_api.call("tz_probe", {"q": "мігранти"}, who=who)
    code = _code(str(e.value))

    # той самий код з ІНШИМИ параметрами не працює
    with pytest.raises(NeedsConfirmation, match="параметри пошуку змінилися"):
        mcp_api.call("tz_probe", {"q": "інше", "confirm": code}, who=who)

    assert "ЗНАЙДЕНО за мігранти" in mcp_api.call("tz_probe", {"q": "мігранти", "confirm": code},
                                                 who=who)
    from mcpauth.policy import telezip_used
    assert telezip_used(who.user) == 1

    # код одноразовий
    with pytest.raises(NeedsConfirmation, match="уже використаний"):
        mcp_api.call("tz_probe", {"q": "мігранти", "confirm": code}, who=who)


def test_invented_code_is_refused(who, probe):
    with pytest.raises(NeedsConfirmation, match="такого коду немає"):
        mcp_api.call("tz_probe", {"q": "х", "confirm": "abcd1234"}, who=who)


def test_expired_code_is_refused(who, probe):
    from datetime import timedelta
    from django.utils import timezone
    with pytest.raises(NeedsConfirmation) as e:
        mcp_api.call("tz_probe", {"q": "х"}, who=who)
    code = _code(str(e.value))
    McpPendingCall.objects.update(created_at=timezone.now() - timedelta(minutes=16))
    with pytest.raises(NeedsConfirmation, match="протермінований"):
        mcp_api.call("tz_probe", {"q": "х", "confirm": code}, who=who)


def test_owner_can_switch_confirmation_off_without_deploy(who, probe):
    Setting.objects.create(key=registry.CONFIRM_SETTING, value="0")
    assert "ЗНАЙДЕНО" in mcp_api.call("tz_probe", {"q": "х"}, who=who)
    assert McpPendingCall.objects.count() == 0


def test_local_stdio_also_asks(probe):
    """Навіть на машині власника модель питає: гроші витрачає не вона."""
    with pytest.raises(NeedsConfirmation):
        mcp_api.call("tz_probe", {"q": "х"})


def test_every_paid_tool_requires_confirmation():
    """Кожен платний tz_-інструмент має параметр confirm (інакше він витрачає
    гроші без згоди)."""
    import inspect
    from analysis.services.mcp_api import telezip
    paid = ("tz_find", "tz_channels", "tz_users")
    for name in paid:
        sig = inspect.signature(registry.TOOLS[name].fn)
        assert "confirm" in sig.parameters, f"{name} без параметра confirm"
        src = inspect.getsource(registry.TOOLS[name].fn)
        assert "require_confirmation" in src, f"{name} не питає згоди"
    assert "require_confirmation" not in inspect.getsource(telezip.tz_status)   # безкоштовний


# --- каскад: довідник → акаунт → TeleZip ----------------------------------------

def test_known_channels_need_no_paid_search(who):
    Channel.objects.create(username="sakhaday", title="Саха день", tg_id=111, subscribers=5000)
    out = mcp_api.call("tz_channels", {"name": "sakhaday"}, who=who)
    assert "Знайдено без TeleZip" in out and "Саха день" in out
    assert "платний пошук не потрібен" in out
    assert McpPendingCall.objects.count() == 0        # згоди навіть не просили


def test_unknown_channel_tries_account_then_asks_for_telezip(who, monkeypatch):
    """Невідомий канал: спершу резолв акаунтом (безкоштовно), далі — згода на TeleZip."""
    from accounts.services import registry as acc_registry
    acc = TelegramAccount.objects.create(name="A", phone_number="+70000000041", is_active=True,
                                         is_authenticated=True,
                                         proxy=Proxy.objects.create(proxy_string="p:1:u:p"))

    class Fake:
        def __init__(self, i):
            self.id = i

        def channel_meta(self, handle):
            return {"tg_id": 222, "username": handle, "title": f"Канал {handle}",
                    "subscribers": 7, "description": ""}
    monkeypatch.setattr(acc_registry, "get", lambda i: Fake(i))
    out = mcp_api.call("tz_channels", {"name": "newchan"}, who=who)
    assert f"акаунт #{acc.id}" in out and "Канал newchan" in out
    assert Channel.objects.filter(tg_id=222).exists()       # рядок довідника створено
    assert "платний пошук не потрібен" in out

    # той, кого не резолвить і акаунт → просимо згоду на TeleZip
    monkeypatch.setattr(acc_registry, "get", lambda i: type("F", (), {
        "channel_meta": lambda self, h: {}})())
    with pytest.raises(NeedsConfirmation) as e:
        mcp_api.call("tz_channels", {"name": "ghostchan"}, who=who)
    assert "немає ні в довіднику, ні через акаунт" in str(e.value) and "ghostchan" in str(e.value)


def test_free_text_search_goes_straight_to_consent(who):
    """Вільний пошук по змісту локально не перевіриш — одразу згода."""
    with pytest.raises(NeedsConfirmation) as e:
        mcp_api.call("tz_channels", {"term": "Якутия новости"}, who=who)
    assert "Якутия новости" in str(e.value)
