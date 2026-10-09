"""Батчі довідника: повторні виклики, часткові помилки, відкат і права MCP."""
import json

import pytest
from django.contrib.auth.models import Permission, User
from django.db import DataError

from analysis.models import Channel, Region
from analysis.services import mcp_api
from analysis.services.mcp_api import Actor, ToolError
from mcpauth.models import McpAuditLog

pytestmark = pytest.mark.django_db


def batch(name, items, **kwargs):
    return mcp_api.call(name, {"items": json.dumps(items)}, **kwargs)


def test_add_batch_idempotent_and_continues_after_rollback():
    items = [
        {"url": "@alpha", "title": "Альфа", "topics": "новини"},
        {"url": "@invalid", "chat_type": "invalid"},
        {"url": "https://example.org/news", "title": "Сайт", "subscribers": 0},
    ]
    out = batch("channels_add_batch", items)
    assert "успішно 2, помилок 1" in out
    assert "Запис 2: ПОМИЛКА" in out
    assert not Channel.objects.filter(username="invalid").exists()
    assert Channel.objects.count() == 2
    alpha = Channel.objects.get(username="alpha")
    out = batch("channels_add_batch", [
        {"url": "https://t.me/ALPHA", "title": "Не замінювати", "topics": "Новини, політика"},
        {"url": "@beta"},
    ])
    alpha.refresh_from_db()
    assert alpha.title == "Альфа"
    assert alpha.topics == ["новини", "політика"]
    assert f"#{alpha.id} уже був" in out
    assert Channel.objects.count() == 3


def test_add_batch_failure_does_not_fill_existing_channel():
    ch, _ = Channel.ensure("@alpha")
    batch("channels_add_batch", [{"url": "@alpha", "title": "Не зберегти", "chat_type": "bad"}])
    ch.refresh_from_db()
    assert ch.title == ""


def test_update_batch_different_changes_and_failed_row_rollback():
    region = Region.objects.create(name="Дагестан")
    alpha, _ = Channel.ensure("@alpha", name="Альфа", region=region)
    alpha.topics = ["новини"]
    alpha.directory_focus = "Старий фокус"
    alpha.save()
    beta, _ = Channel.ensure("@beta", name="Бета")
    out = batch("channels_update_batch", [
        {"ref": f"#{alpha.id}", "region": "-", "focus": "-", "remove_topics": "новини",
         "add_topics": "політика", "subscribers": 0, "discusses_problems": False},
        {"ref": "@beta", "subscribers": 1234, "title": "Не зберегти", "chat_type": "bad"},
        {"ref": "@missing", "title": "Відсутній"},
        {"ref": "https://t.me/beta", "title": "Нова Бета", "settlement": "Місто",
         "audience_note": "дані видання", "subscribers": 200, "discusses_problems": True},
    ])
    assert "успішно 2, помилок 2" in out
    alpha.refresh_from_db()
    beta.refresh_from_db()
    assert alpha.region_subject_id is None
    assert alpha.directory_focus == ""
    assert alpha.topics == ["політика"]
    assert alpha.subscribers == 0
    assert alpha.discusses_problems is False
    assert beta.title == "Нова Бета"
    assert beta.settlement == "Місто"
    assert beta.subscribers == 200
    assert beta.discusses_problems is True
    assert beta.directory_meta["audience_source"] == "дані видання"
    assert Channel.objects.count() == 2


def test_update_batch_null_and_omitted_fields_preserve_values():
    ch, _ = Channel.ensure("@alpha", name="Альфа")
    ch.subscribers = 400
    ch.save()
    batch("channels_update_batch", [{"ref": "@alpha", "title": None, "subscribers": None}])
    ch.refresh_from_db()
    assert ch.title == "Альфа"
    assert ch.subscribers == 400


def test_get_batch_full_cards_order_missing_and_ambiguous():
    alpha, _ = Channel.ensure("@alpha", name="Спільна назва один", language="uk")
    beta, _ = Channel.ensure("@beta", name="Спільна назва два")
    alpha.directory_focus = "Повний фокус"
    alpha.topics = ["політика"]
    alpha.save()
    refs = ["@beta", f"#{alpha.id}", "@missing", "Спільна назва"]
    out = mcp_api.call("channels_get_batch", {"refs": json.dumps(refs)})
    assert "знайдено 2, помилок 2" in out
    assert out.index(f"#{beta.id} · @beta") < out.index(f"#{alpha.id} · #{alpha.id}")
    assert "Повний фокус" in out and "політика" in out and "uk" in out
    assert f"/admin/analysis/channel/{alpha.id}/change/" in out
    assert "неоднозначне" in out
    csv_out = mcp_api.call("channels_get_batch", {"refs": f"#{alpha.id}, https://t.me/beta"})
    assert "знайдено 2, помилок 0" in csv_out


@pytest.mark.parametrize("name,param", [
    ("channels_add_batch", "items"), ("channels_update_batch", "items"),
    ("channels_get_batch", "refs"),
])
@pytest.mark.parametrize("value", ["[]", "{}", "[", json.dumps([{}] * 101)])
def test_batch_envelope_invalid_before_writes(name, param, value):
    with pytest.raises(ToolError):
        mcp_api.call(name, {param: value})
    assert not Channel.objects.exists()


@pytest.mark.parametrize("invalid", [
    {}, "@not-an-object", {"url": ""}, {"url": "@bad", "unknown": "x"},
    {"url": "@bad", "subscribers": True}, {"url": "@bad", "subscribers": "200"},
    {"url": "@bad", "subscribers": -2}, {"url": "@bad", "subscribers": 2**40},
    {"url": "@bad", "topics": ["новини"]},
])
def test_invalid_row_does_not_abort_valid_rows(invalid):
    out = batch("channels_add_batch", [invalid, {"url": "@good"}])
    assert "успішно 1, помилок 1" in out
    assert list(Channel.objects.values_list("username", flat=True)) == ["good"]


def test_update_boolean_string_rejected():
    ch, _ = Channel.ensure("@alpha")
    out = batch("channels_update_batch", [{"ref": "@alpha", "discusses_problems": "false"}])
    assert "помилок 1" in out
    ch.refresh_from_db()
    assert ch.discusses_problems is None


def test_batch_accepts_100_items_and_applies_duplicates_in_order():
    out = batch("channels_add_batch", [{"url": "@alpha"}] * 100)
    assert "успішно 100, помилок 0" in out
    assert Channel.objects.count() == 1
    batch("channels_update_batch", [
        {"ref": "@alpha", "title": "Перша правка"},
        {"ref": "@alpha", "title": "Остання правка"},
    ])
    assert Channel.objects.get().title == "Остання правка"


def test_database_row_error_rolls_back_and_keeps_processing(monkeypatch):
    from analysis.services.mcp_api import channels
    original = mcp_api.TOOLS["channel_add"].fn

    def fails_after_insert(**payload):
        result = original(**payload)
        if payload["url"] == "@bad":
            raise DataError("значення не поміщається в поле")
        return result

    monkeypatch.setattr(mcp_api.TOOLS["channel_add"], "fn", fails_after_insert)
    out = channels.channels_add_batch(json.dumps([{"url": "@bad"}, {"url": "@good"}]))
    assert "успішно 1, помилок 1" in out
    assert list(Channel.objects.values_list("username", flat=True)) == ["good"]


@pytest.mark.parametrize("name", ["channels_add_batch", "channels_update_batch"])
def test_batch_write_readonly(name, monkeypatch):
    monkeypatch.setenv("MCP_READONLY", "1")
    with pytest.raises(ToolError, match="лише-читання"):
        batch(name, [{"url": "@alpha", "ref": "@alpha"}])


def test_batch_permissions_and_audit():
    user = User.objects.create_user("batch-user")
    user.user_permissions.add(Permission.objects.get(
        content_type__app_label="analysis", codename="view_channel"))
    reader = Actor(user=user, scopes=["mcp:read"])
    assert "помилок 1" in mcp_api.call("channels_get_batch", {"refs": "@missing"}, who=reader)
    for name in ("channels_add_batch", "channels_update_batch"):
        with pytest.raises(ToolError, match="бракує прав"):
            batch(name, [], who=reader)
    # Навіть скоуп створення/зміни не обходить відповідне право Django.
    scoped = Actor(user=user, scopes=["mcp:read", "mcp:write", "mcp:create"])
    for name in ("channels_add_batch", "channels_update_batch"):
        with pytest.raises(ToolError, match="бракує права в адмінці"):
            batch(name, [], who=scoped)
    user.user_permissions.add(Permission.objects.get(
        content_type__app_label="analysis", codename="add_channel"))
    creator = Actor(user=User.objects.get(pk=user.pk), scopes=["mcp:read", "mcp:create"])
    batch("channels_add_batch", [{"url": "@alpha"}], who=creator)
    log = McpAuditLog.objects.get(tool="channels_add_batch", ok=True)
    assert log.user_id == user.pk and "@alpha" in log.payload["items"]
    assert Channel.objects.filter(username="alpha").exists()
