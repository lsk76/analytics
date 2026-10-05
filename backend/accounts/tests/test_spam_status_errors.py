from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib import admin
from django.test import RequestFactory

from accounts.models import TelegramAccount
from accounts.services import registry, spam_status_stage
from accounts.services.managed import AccountUnavailable, GatewayDown, RateLimited
from analysis.services.mcp_api.accounts import account_spam_check

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("surface", ["admin", "mcp", "worker"])
@pytest.mark.parametrize("error", [
    GatewayDown("tg-gateway недоступний"),
    AccountUnavailable("needs_proxy", "Акаунту потрібна проксі"),
    RateLimited(60, "Повторити перевірку за 60с"),
])
def test_spam_check_preserves_gateway_error(monkeypatch, surface, error):
    account = TelegramAccount.objects.create(phone_number="+70000000001")
    managed = SimpleNamespace(spam_status=Mock(side_effect=error))
    monkeypatch.setattr(registry, "get", lambda account_id: managed)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    rendered = ""
    if surface == "admin":
        model_admin = admin.site._registry[TelegramAccount]
        message_user = Mock()
        monkeypatch.setattr(model_admin, "message_user", message_user)
        model_admin.check_spam_status(RequestFactory().get("/"),
                                      TelegramAccount.objects.filter(pk=account.pk))
        rendered = message_user.call_args.args[1]
    elif surface == "mcp":
        rendered = account_spam_check(str(account.pk), pause=0)
    else:
        monkeypatch.setattr(spam_status_stage, "_claim_account", lambda: account)
        assert spam_status_stage.spam_status_check_once()
    account.refresh_from_db()
    assert account.spam_status == "unknown"
    assert str(error) in account.spam_status_detail
    assert account.spam_status_checked_at is not None
    if surface != "worker":
        assert str(error) in rendered


@pytest.mark.parametrize("result,expected", [
    ({"status": "free", "detail": "Good news, no limits!"}, "Good news, no limits!"),
    ({"status": "unknown", "detail": "Немає відповіді за 15с", "ok": False}, "Немає відповіді за 15с"),
    ({"status": "unknown"}, "Не отримано результат перевірки SpamBot"),
])
def test_spam_reply_or_empty_result_is_displayed(monkeypatch, result, expected):
    account = TelegramAccount.objects.create(phone_number="+70000000002")
    monkeypatch.setattr(registry, "get", lambda account_id: SimpleNamespace(
        spam_status=lambda: result))
    assert expected in account_spam_check(str(account.pk), pause=0)
    account.refresh_from_db()
    assert account.spam_status_detail == expected
    assert account.spam_status == result["status"]
