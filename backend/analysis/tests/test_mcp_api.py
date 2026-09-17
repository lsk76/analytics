"""MCP-шар керування сервісом: реєстр, резолви, дії, що змінюють стан.

Мережевих інструментів (account_check/spam/proxy/dialogs) тут немає свідомо —
вони ходять у Telegram; їхня логіка — тонка обгортка над accounts.services.
"""
import pytest
from django.utils import timezone

from accounts.models import TelegramAccount
from analysis.models import (AnalysisTask, Channel, CollectChunk, MonitorChat,
                             ResearchRun, Setting, Source)
from analysis.services import mcp_api
from analysis.services.mcp_api import common
from analysis.services.mcp_api.registry import ToolError

from .factories import SourceFactory, SubscriptionFactory, TaskFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    return TelegramAccount.objects.create(name="Тест", phone_number="+70000000001")


@pytest.fixture
def chat():
    task = TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR, slug="mon-task")
    channel = Channel.objects.create(username="testchat", title="Тестовий чат")
    return MonitorChat.objects.create(task=task, channel=channel)


# --- реєстр -----------------------------------------------------------------

def test_manifest_covers_every_tool():
    names = {m["name"] for m in mcp_api.manifest()}
    assert names == set(mcp_api.TOOLS)
    for m in mcp_api.manifest():
        assert m["doc"], f"{m['name']} без докстрінга — модель не зрозуміє, що це"
        for p in m["params"]:
            assert p["type"] in ("str", "int", "float", "bool"), (m["name"], p)


def test_unknown_tool_and_unknown_param():
    with pytest.raises(ToolError, match="невідомий інструмент"):
        mcp_api.call("нема_такого")
    with pytest.raises(ToolError, match="невідомі параметри"):
        mcp_api.call("tasks_list", {"pipelinee": "events"})


def test_readonly_blocks_mutating_tools(monkeypatch):
    monkeypatch.setenv("MCP_READONLY", "1")
    with pytest.raises(ToolError, match="лише-читання"):
        mcp_api.call("setting_set", {"key": "x", "value": "y"})
    # читання лишається доступним
    assert mcp_api.call("tasks_list")


def test_none_values_fall_back_to_defaults(chat):
    """Host-шар шле всі поля схеми; None має означати «не передали»."""
    out = mcp_api.call("chats_list", {"task": "", "active": None, "limit": 5})
    assert "mon-task" in out


# --- резолви ----------------------------------------------------------------

def test_resolve_task_by_id_slug_name():
    task = TaskFactory(slug="ethnic-clashes", name="Етнічні сутички")
    assert common.resolve_task(task.id) == task
    assert common.resolve_task("#%d" % task.id) == task
    assert common.resolve_task("ethnic-clashes") == task
    assert common.resolve_task("сутички") == task
    with pytest.raises(ToolError, match="не знайдено"):
        common.resolve_task("такої немає")


def test_resolve_task_ambiguous_lists_candidates():
    TaskFactory(name="Моніторинг Сибіру", slug="a-sib")
    TaskFactory(name="Моніторинг Кавказу", slug="a-kav")
    with pytest.raises(ToolError, match="неоднозначне"):
        common.resolve_task("Моніторинг")


def test_parse_date_rejects_garbage():
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        common.parse_date("вчора", "date_from")


# --- збори ------------------------------------------------------------------

def test_run_create_plans_chunks_and_is_idempotent():
    task = TaskFactory(slug="run-task", collect_chunk_days=1)
    out = mcp_api.call("run_create", {"task": "run-task", "date_from": "2026-01-01",
                                      "date_to": "2026-01-03"})
    run = ResearchRun.objects.get()
    assert run.status == "collecting" and run.started_at
    assert CollectChunk.objects.filter(job=run).count() == 3
    assert f"run_id={run.id}" in out

    # той самий період удруге — нових чанків нема (enqueue_collection ідемпотентний)
    out2 = mcp_api.call("run_create", {"task": "run-task", "date_from": "2026-01-01",
                                       "date_to": "2026-01-03"})
    assert "0 чанків" in out2 and "уже покрито" in out2
    assert CollectChunk.objects.count() == 3
    assert task.runs.count() == 2


def test_run_create_validates_period():
    TaskFactory(slug="v-task")
    with pytest.raises(ToolError, match="раніше за"):
        mcp_api.call("run_create", {"task": "v-task", "date_from": "2026-02-01",
                                    "date_to": "2026-01-01"})
    with pytest.raises(ToolError, match="400"):
        mcp_api.call("run_create", {"task": "v-task", "date_from": "2020-01-01",
                                    "date_to": "2026-01-01"})


def test_run_cancel_drops_pending_chunks_only():
    task = TaskFactory(slug="c-task", collect_chunk_days=1)
    mcp_api.call("run_create", {"task": "c-task", "date_from": "2026-03-01",
                                "date_to": "2026-03-03"})
    run = ResearchRun.objects.get()
    done = run.chunks.first()
    done.status = "done"
    done.save(update_fields=["status"])

    mcp_api.call("run_cancel", {"run_id": run.id})
    run.refresh_from_db()
    assert run.status == "cancelled" and run.finished_at
    assert list(run.chunks.values_list("status", flat=True)) == ["done"]


def test_run_show_flags_awaiting_agent():
    task = TaskFactory(slug="aw-task")
    run = ResearchRun.objects.create(task=task, date_from="2026-05-01",
                                     date_to="2026-05-02", status="awaiting_agent")
    out = mcp_api.call("run_show", {"run_id": run.id})
    assert "Чекає агента" in out and "_dir/runs/run_%d" % run.id in out


# --- налаштування -----------------------------------------------------------

def test_setting_set_creates_then_updates():
    mcp_api.call("setting_set", {"key": "digest_report_prompt", "value": "перший",
                                 "description": "опис"})
    row = Setting.objects.get(key="digest_report_prompt")
    assert row.value == "перший" and row.description == "опис"

    out = mcp_api.call("setting_set", {"key": "digest_report_prompt", "value": "другий"})
    row.refresh_from_db()
    assert row.value == "другий" and row.description == "опис"
    assert "було: перший" in out


# --- чати моніторингу -------------------------------------------------------

def test_chat_update_sets_and_unlinks_account(chat, account):
    mcp_api.call("chat_update", {"chat": str(chat.id), "account": str(account.id),
                                 "stream_enabled": True, "priority": 5})
    chat.refresh_from_db()
    assert chat.tg_account_id == account.id and chat.stream_enabled and chat.priority == 5

    mcp_api.call("chat_update", {"chat": "@testchat", "account": "-"})
    chat.refresh_from_db()
    assert chat.tg_account_id is None

    out = mcp_api.call("chat_update", {"chat": str(chat.id)})
    assert "нічого не змінено" in out


def test_chats_list_problems_only_finds_orphans(chat):
    assert "mon-task" in mcp_api.call("chats_list", {"problems_only": True})
    chat.tg_account = TelegramAccount.objects.create(name="a", phone_number="+70000000009")
    chat.save()
    assert "немає" in mcp_api.call("chats_list", {"problems_only": True})


# --- джерела ----------------------------------------------------------------

def test_source_update_poll_now_and_reset_cursor():
    src = SourceFactory(poll_cursor={"last_msg_id": 42},
                        next_poll_at=timezone.now() + timezone.timedelta(hours=5))
    mcp_api.call("source_update", {"ref": str(src.id), "poll_now": True,
                                   "reset_cursor": True, "poll_interval_sec": 30})
    src.refresh_from_db()
    assert src.poll_cursor == {}
    assert src.next_poll_at <= timezone.now()
    assert src.poll_interval_sec == 60  # нижня межа, щоб не задовбати джерело


def test_sources_list_filters_by_task():
    sub = SubscriptionFactory()
    other = SourceFactory(name="Чуже джерело")
    out = mcp_api.call("sources_list", {"task": sub.task.slug})
    assert sub.source.name in out and other.name not in out


# --- зведення ---------------------------------------------------------------

def test_service_health_runs_on_empty_db():
    out = mcp_api.call("service_health")
    assert "Задачі" in out and "Черги постів" in out and "Telegram-акаунти" in out


def test_events_stats_rejects_unknown_grouping():
    with pytest.raises(ToolError, match="невідомий group_by"):
        mcp_api.call("events_stats", {"group_by": "по-настрою"})
    with pytest.raises(ToolError, match="категорію"):
        mcp_api.call("events_stats", {"group_by": "tag"})


def test_account_tools_render_without_network(account):
    assert account.phone_number in mcp_api.call("accounts_list")
    assert "НЕ ПРИЗНАЧЕНА" in mcp_api.call("account_show", {"ref": str(account.id)})
    out = mcp_api.call("account_update", {"ref": account.phone_number,
                                          "is_active": False, "add_tags": "проблемний"})
    account.refresh_from_db()
    assert account.is_active is False and "проблемний" in out
    assert account.tags.filter(name="проблемний").exists()


def test_account_warm_up_needs_channels(account):
    with pytest.raises(ToolError, match="нічим гріти"):
        mcp_api.call("account_warm_up", {"ref": str(account.id)})
    Channel.objects.create(username="warmme", title="Канал")
    out = mcp_api.call("account_warm_up", {"ref": str(account.id), "channels": 1})
    assert "job #" in out and account.warm_up_jobs.count() == 1
