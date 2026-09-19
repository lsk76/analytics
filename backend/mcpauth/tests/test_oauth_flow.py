"""Повний шлях доступу: реєстрація клієнта → логін і згода → токен → виклик.

Тест іде тим самим маршрутом, що й реальний клієнт, але без HTTP-шару SDK:
провайдер викликається напряму, а людська частина — через Django test client.
Так перевіряється саме наша логіка (ролі, стеля скоупів, одноразовість коду),
а не транспорт.
"""
import asyncio

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from analysis.services import mcp_api
from analysis.services.mcp_api.registry import ToolError
from analysis.tests.factories import TaskFactory
from mcpauth.models import McpAuthCode, McpAuthRequest, McpClient, McpRole, McpToken
from mcpauth.oauth import DjangoOAuthProvider

pytestmark = pytest.mark.django_db


# Async-методи провайдера — однорядкові обгортки над синхронними (`_authorize`,
# `_load_code`, …). Тестуємо синхронний шар напряму: запис із sync_to_async іде
# ІНШИМ з'єднанням до БД і не відкочується разом із тестовою транзакцією, тож
# рядки протікали б між тестами. Саму обгортку перевіряє окремий тест нижче.
SYNC = {
    "register_client": "_register_client", "get_client": "_get_client",
    "authorize": "_authorize", "load_authorization_code": "_load_code",
    "exchange_authorization_code": "_exchange_code",
    "load_access_token": "_load_access", "load_refresh_token": "_load_refresh",
    "exchange_refresh_token": "_exchange_refresh", "revoke_token": "_revoke",
}


class SyncProvider:
    """Той самий провайдер, але без async-обгорток (див. коментар вище)."""

    def __init__(self):
        self._p = DjangoOAuthProvider()

    def __getattr__(self, name):
        return getattr(self._p, SYNC.get(name, name))


@pytest.fixture
def provider():
    return SyncProvider()


@pytest.fixture
def client_info():
    return OAuthClientInformationFull(
        client_id="cli-1", client_secret=None, client_name="Claude Desktop",
        redirect_uris=["http://127.0.0.1:33418/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"], scope="mcp:read mcp:write")


@pytest.fixture
def operator(db):
    user = User.objects.create_user("operator", password="x", is_staff=True)
    McpRole.objects.create(user=user, role=McpRole.OPERATOR)
    return user


def _authorize(provider, client_info, scopes=("mcp:read", "mcp:write")):
    provider.register_client(client_info)
    params = AuthorizationParams(
        state="st-1", scopes=list(scopes), code_challenge="chal",
        redirect_uri="http://127.0.0.1:33418/callback",
        redirect_uri_provided_explicitly=True, resource=None)
    url = provider.authorize(client_info, params)
    return url, McpAuthRequest.objects.order_by("-id").first()


def test_authorize_sends_user_to_django_consent(provider, client_info):
    url, req = _authorize(provider, client_info)
    # MCP-процес не має сесій, тож людину відправляють у Django
    assert "/mcp/consent/" in url and req.key in url
    assert req.state == "st-1" and req.code_challenge == "chal"


def test_consent_requires_login(client, provider, client_info):
    _, req = _authorize(provider, client_info)
    resp = client.get(f"{reverse('mcpauth:consent')}?req={req.key}")
    assert resp.status_code == 302 and "login" in resp["Location"]


def test_user_without_role_is_refused(client, provider, client_info):
    User.objects.create_user("nobody", password="x")
    client.login(username="nobody", password="x")
    _, req = _authorize(provider, client_info)
    resp = client.get(f"{reverse('mcpauth:consent')}?req={req.key}")
    assert resp.status_code == 403
    assert "немає активної ролі" in resp.content.decode()
    assert not McpAuthCode.objects.exists()


def test_full_flow_issues_token_scoped_by_role(client, provider, client_info, operator):
    client.force_login(operator)
    _, req = _authorize(provider, client_info)

    page = client.get(f"{reverse('mcpauth:consent')}?req={req.key}").content.decode()
    assert "Claude Desktop" in page and "mcp:write" in page

    resp = client.post(reverse("mcpauth:consent"), {"req": req.key, "action": "allow"})
    assert resp.status_code == 302
    assert resp["Location"].startswith("http://127.0.0.1:33418/callback?")
    assert "state=st-1" in resp["Location"]
    code = resp["Location"].split("code=")[1].split("&")[0]

    loaded = provider.load_authorization_code(client_info, code)
    assert loaded.subject == "operator" and loaded.scopes == ["mcp:read", "mcp:write"]

    token = provider.exchange_authorization_code(client_info, loaded)
    assert token.access_token and token.refresh_token
    access = provider.load_access_token(token.access_token)
    assert access.subject == "operator" and access.claims["role"] == McpRole.OPERATOR

    # код одноразовий
    assert provider.load_authorization_code(client_info, code) is None


def test_role_is_the_ceiling_even_if_client_asks_more(client, provider, client_info):
    reader = User.objects.create_user("reader", password="x")
    McpRole.objects.create(user=reader, role=McpRole.READER)
    client.force_login(reader)
    _, req = _authorize(provider, client_info, scopes=("mcp:read", "mcp:write", "mcp:admin"))
    client.post(reverse("mcpauth:consent"), {"req": req.key, "action": "allow"})
    assert McpAuthCode.objects.order_by("-id").first().scopes == ["mcp:read"]


def test_refusal_returns_error_to_client(client, provider, client_info, operator):
    client.force_login(operator)
    _, req = _authorize(provider, client_info)
    resp = client.post(reverse("mcpauth:consent"), {"req": req.key, "action": "deny"})
    assert "error=access_denied" in resp["Location"]
    assert not McpAuthCode.objects.exists()


def test_revoked_role_kills_live_token(client, provider, client_info, operator):
    client.force_login(operator)
    _, req = _authorize(provider, client_info)
    resp = client.post(reverse("mcpauth:consent"), {"req": req.key, "action": "allow"})
    code = resp["Location"].split("code=")[1].split("&")[0]
    token = provider.exchange_authorization_code(
        client_info, provider.load_authorization_code(client_info, code))

    McpRole.objects.filter(user=operator).update(is_active=False)
    # токен ще «живий» за строком, але роль забрали — доступу немає
    assert provider.load_access_token(token.access_token) is None


def test_refresh_rotates_and_kills_the_old_one(client, provider, client_info, operator):
    client.force_login(operator)
    _, req = _authorize(provider, client_info)
    resp = client.post(reverse("mcpauth:consent"), {"req": req.key, "action": "allow"})
    code = resp["Location"].split("code=")[1].split("&")[0]
    first = provider.exchange_authorization_code(
        client_info, provider.load_authorization_code(client_info, code))

    loaded = provider.load_refresh_token(client_info, first.refresh_token)
    second = provider.exchange_refresh_token(client_info, loaded, ["mcp:read"])
    assert second.access_token != first.access_token
    # старий refresh мертвий одразу — украдений не проживе 30 днів
    assert provider.load_refresh_token(client_info, first.refresh_token) is None
    assert second.scope == "mcp:read"


def test_token_identity_drives_tool_visibility(client, provider, client_info, operator):
    """Те, що видав OAuth, і те, що бачить інструмент, — один і той самий юзер."""
    from analysis.services.mcp_api import Actor
    mine = TaskFactory(slug="mine", owner=operator)
    TaskFactory(slug="foreign", owner=User.objects.create_user("other", password="x"))

    client.force_login(operator)
    _, req = _authorize(provider, client_info)
    resp = client.post(reverse("mcpauth:consent"), {"req": req.key, "action": "allow"})
    code = resp["Location"].split("code=")[1].split("&")[0]
    token = provider.exchange_authorization_code(
        client_info, provider.load_authorization_code(client_info, code))
    access = provider.load_access_token(token.access_token)

    who = Actor(user=operator, scopes=access.scopes, role=access.claims["role"])
    out = mcp_api.call("tasks_list", {}, who=who)
    assert "mine" in out and "foreign" not in out


def test_revoke_endpoint_disables_token(provider, client_info, client, operator):
    client.force_login(operator)
    _, req = _authorize(provider, client_info)
    resp = client.post(reverse("mcpauth:consent"), {"req": req.key, "action": "allow"})
    code = resp["Location"].split("code=")[1].split("&")[0]
    token = provider.exchange_authorization_code(
        client_info, provider.load_authorization_code(client_info, code))
    access = provider.load_access_token(token.access_token)
    provider.revoke_token(access)
    assert provider.load_access_token(token.access_token) is None
    assert McpToken.objects.filter(revoked_at__isnull=False).exists()


def test_async_wrappers_delegate_to_the_sync_layer(monkeypatch):
    """SDK викликає провайдер із циклу подій — перевіряємо саме цю обгортку.

    Без БД навмисне: тест із transaction=True робив би flush усієї тестової бази
    (pytest-django), а це стирає дані дата-міграцій — зокрема групу
    «Telegram-акаунти» з accounts/0012, і падали вже ЧУЖІ тести.
    """
    provider = DjangoOAuthProvider()
    seen = {}

    def fake_load(token):
        seen["token"] = token
        return "ACCESS"

    monkeypatch.setattr(provider, "_load_access", fake_load)
    assert asyncio.run(provider.load_access_token("raw-token")) == "ACCESS"
    assert seen["token"] == "raw-token"
