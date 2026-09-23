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
    task = TaskFactory(slug="run-task", collect_chunk_days=1,
                       pipeline=AnalysisTask.PIPELINE_EVENTS)
    out = mcp_api.call("run_create", {"task": "run-task", "date_from": "2026-01-01",
                                      "date_to": "2026-01-03"})
    run = ResearchRun.objects.get()
    assert run.status == "collecting" and run.started_at
    assert CollectChunk.objects.filter(job=run).count() == 3
    assert f"run_id={run.id}" in out
    assert "$0.30" in out          # 3 чанки × $0.10 — ціна видна ДО запуску

    # той самий період удруге — нових чанків нема (enqueue_collection ідемпотентний)
    out2 = mcp_api.call("run_create", {"task": "run-task", "date_from": "2026-01-01",
                                       "date_to": "2026-01-03"})
    assert "0 чанків" in out2 and "уже покрито" in out2
    assert CollectChunk.objects.count() == 3
    assert task.runs.count() == 2


def test_run_create_validates_period():
    TaskFactory(slug="v-task", pipeline=AnalysisTask.PIPELINE_EVENTS)
    with pytest.raises(ToolError, match="раніше за"):
        mcp_api.call("run_create", {"task": "v-task", "date_from": "2026-02-01",
                                    "date_to": "2026-01-01"})
    with pytest.raises(ToolError, match="400"):
        mcp_api.call("run_create", {"task": "v-task", "date_from": "2020-01-01",
                                    "date_to": "2026-01-01"})


def test_run_cancel_drops_pending_chunks_only():
    task = TaskFactory(slug="c-task", collect_chunk_days=1,
                       pipeline=AnalysisTask.PIPELINE_EVENTS)
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


def test_problem_sources_ignore_unsubscribed_ones():
    """Джерело без активної підписки info_collect не бере — його прострочений
    next_poll_at не проблема, а норма (інакше діагностика кричить вовк)."""
    stale = timezone.now() - timezone.timedelta(days=2)
    SourceFactory(name="Нічиє джерело", next_poll_at=stale)
    sub = SubscriptionFactory(task__pipeline=AnalysisTask.PIPELINE_INFOSPACE)
    Source.objects.filter(pk=sub.source_id).update(next_poll_at=stale)

    out = mcp_api.call("sources_list", {"problems_only": True})
    assert sub.source.name in out
    assert "Нічиє джерело" not in out

    health = mcp_api.call("service_health")
    assert "без підписок 1" in health
    assert "прострочений полінг (>30хв) : 1" in health


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


def test_task_update_changes_collection_params_and_shows_old_query():
    task = TaskFactory(slug="q-task", telezip_query="старий запит",
                       pipeline=AnalysisTask.PIPELINE_EVENTS)
    out = mcp_api.call("task_update", {"ref": "q-task", "telezip_query": "новий +(запит)",
                                       "languages": "ru,uk", "unique": True,
                                       "chunk_days": 2})
    task.refresh_from_db()
    assert task.telezip_query == "новий +(запит)"
    assert task.languages == ["ru", "uk"] and task.telezip_unique is True
    assert task.collect_chunk_days == 2
    assert "старий запит" in out          # є куди відкотитись
    assert "run_create task=q-task" in out

    assert "нічого не змінено" in mcp_api.call("task_update", {"ref": "q-task"})


# --- довідник каналів -------------------------------------------------------

def test_channel_add_is_idempotent_and_merges_topics():
    out = mcp_api.call("channel_add", {"url": "@NewChan", "title": "Новий", "topics": "новини, політика"})
    assert "створено" in out
    ch = Channel.objects.get(username="newchan")
    assert ch.url == "https://t.me/newchan" and ch.topics == ["новини", "політика"]
    out = mcp_api.call("channel_add", {"url": "https://t.me/newchan", "topics": "Політика, етнічне"})
    assert "уже був" in out and "додано теми: етнічне" in out
    ch.refresh_from_db()
    assert ch.topics == ["новини", "політика", "етнічне"] and Channel.objects.count() == 1


def test_channel_update_tags_region_and_type():
    from analysis.models import Region
    Region.objects.create(name="Республіка Дагестан")
    ch = Channel.objects.create(username="dag", title="Дагестан-чат", topics=["барахолка", "новини"])
    out = mcp_api.call("channel_update", {"ref": "@dag", "add_topics": "етнічне",
                                          "remove_topics": "барахолка", "region": "Дагестан",
                                          "chat_type": "chat"})
    ch.refresh_from_db()
    assert ch.topics == ["новини", "етнічне"] and ch.chat_type == "chat"
    assert ch.region_subject.name == "Республіка Дагестан" and "Республіка Дагестан" in out
    with pytest.raises(ToolError, match="chat_type"):
        mcp_api.call("channel_update", {"ref": str(ch.id), "chat_type": "bogus"})
    with pytest.raises(ToolError, match="channel_add"):
        mcp_api.call("channel_update", {"ref": "@nonexistent", "add_topics": "x"})


# --- джерела: створити й підписати ------------------------------------------

def test_source_add_creates_and_subscribes():
    task = TaskFactory(slug="inf-1")
    out = mcp_api.call("source_add", {"url": "https://example.org/news/rss.xml", "task": "inf-1",
                                      "name": "Новини", "poll_interval_sec": 300})
    src = Source.objects.get()
    assert src.kind == Source.KIND_RSS and src.name == "Новини" and src.poll_interval_sec == 300
    assert src.subscriptions.filter(task=task, is_active=True).exists()
    assert "створено" in out and "підписка inf-1: додано" in out
    # повтор — те саме джерело, підписка вже є
    out = mcp_api.call("source_add", {"url": "https://example.org/news/rss.xml", "task": "inf-1"})
    assert "уже було" in out and "уже є" in out and Source.objects.count() == 1
    # telegram — за посиланням
    mcp_api.call("source_add", {"url": "@some_channel"})
    assert Source.objects.get(channel__username="some_channel").kind == Source.KIND_TELEGRAM


def test_source_subscribe_toggle_and_priority():
    task = TaskFactory(slug="inf-2")
    src = SourceFactory()
    out = mcp_api.call("source_subscribe", {"ref": str(src.id), "task": "inf-2", "priority": 5})
    sub = src.subscriptions.get(task=task)
    assert sub.is_active and sub.priority == 5 and "створено" in out
    mcp_api.call("source_subscribe", {"ref": str(src.id), "task": "inf-2", "active": False})
    sub.refresh_from_db()
    assert sub.is_active is False


# --- задачі: розширене редагування ------------------------------------------

def test_infospace_is_not_treated_as_events_or_monitor():
    TaskFactory(slug="info-only")
    listed = mcp_api.call("tasks_list")
    assert "infospace" in listed and "інформпр" not in listed
    with pytest.raises(ToolError, match="classify_prompt пише лише events"):
        mcp_api.call("task_update", {"ref": "info-only", "classify_prompt": "чужий промпт"})
    with pytest.raises(ToolError, match="run_create збирає TeleZip"):
        mcp_api.call("run_create", {"task": "info-only", "date_from": "2026-01-01",
                                    "date_to": "2026-01-02"})
    ev = TaskFactory(slug="ev-only", pipeline=AnalysisTask.PIPELINE_EVENTS)
    src = SourceFactory()
    with pytest.raises(ToolError, match="лише для infospace"):
        mcp_api.call("source_subscribe", {"ref": str(src.id), "task": ev.slug})
    assert AnalysisTask.objects.get(slug="info-only").classify_system_prompt != "чужий промпт"


def test_task_update_extended_fields():
    TaskFactory(slug="t-ext", description="старий", pipeline=AnalysisTask.PIPELINE_EVENTS)
    out = mcp_api.call("task_update", {"ref": "t-ext", "name": "Нова назва", "description": "-",
                                       "search_comments": False, "geo_enabled": True,
                                       "dedup_window_days": 9, "classify_prompt": "Класифікуй"})
    t = AnalysisTask.objects.get(slug="t-ext")
    assert t.name == "Нова назва" and t.description == "" and t.search_comments is False
    assert t.geo_enabled is True and t.dedup_window_days == 9
    assert t.classify_system_prompt == "Класифікуй" and "опис очищено" in out
    mcp_api.call("task_update", {"ref": "t-ext", "classify_prompt": "-"})
    assert AnalysisTask.objects.get(slug="t-ext").classify_system_prompt == ""


def test_task_show_returns_every_pipeline_field_including_prompts():
    from analysis.services.infospace.prompts import INFO_JUDGE_PROMPT
    TaskFactory(slug="show-info", description="повний опис задачі без обрізання",
                info_screen_prompt="МІЙ СКРІН ПРОМПТ УНІКАЛЬНИЙ",
                info_judge_prompt="", info_tagger_prompt="")
    out = mcp_api.call("task_show", {"ref": "show-info"})
    assert "infospace —" in out and "[info_screen_prompt]" in out
    assert "run_create і classify_prompt цей конвеєр не читає" in out
    assert "МІЙ СКРІН ПРОМПТ УНІКАЛЬНИЙ" in out
    assert "повний опис задачі без обрізання" in out
    assert "порожньо в базі — нижче дефолт із коду" in out
    assert "deduplication judge for a live news-event feed" in out
    assert INFO_JUDGE_PROMPT.strip() in out
    assert "порожньо (лише підказки категорій тегів)" in out
    assert "Свіжість: макс. вік новини (днів)" in out

    long_query = "довгий-запит-" + ("x" * 240)
    TaskFactory(slug="show-ev", pipeline=AnalysisTask.PIPELINE_EVENTS,
                classify_system_prompt="КЛАСИФІКУЙ ЦЕ ПОВНІСТЮ",
                telezip_query=long_query)
    ev = mcp_api.call("task_show", {"ref": "show-ev"})
    assert "events —" in ev and "[classify_system_prompt]" in ev
    assert "КЛАСИФІКУЙ ЦЕ ПОВНІСТЮ" in ev
    assert long_query in ev
    assert "Вікно дедупу (днів)" in ev
    assert "Свіжість: макс. вік новини (днів)" not in ev
    assert "deduplication judge" not in ev


# --- події: список, картка, аудит, теги --------------------------------------

@pytest.fixture
def events():
    from analysis.models import Event, Region, Tag, TagCategory
    task = TaskFactory(slug="ev-task")
    dag = Region.objects.create(name="Республіка Дагестан")
    TagCategory.objects.create(key="topic", label="Тема", closed=False)
    TagCategory.objects.create(key="nationality", label="Національність", closed=True)
    mig = Tag.objects.create(name="мігранти", category="topic")
    uz = Tag.objects.create(name="узбек", category="nationality")
    today = timezone.now().date()
    e1 = Event.objects.create(task=task, event_date=today, summary="бійка на ринку",
                              region_subject=dag, channel_count=3, reach=1000,
                              review_status=Event.REVIEW_APPROVED)
    e1.tags.add(mig, uz)
    e2 = Event.objects.create(task=task, event_date=today - timezone.timedelta(days=60),
                              summary="стара подія", review_status=Event.REVIEW_APPROVED)
    e3 = Event.objects.create(task=task, event_date=today, summary="на аудит",
                              review_status=Event.REVIEW_PENDING)
    return task, e1, e2, e3


def test_events_list_filters_like_admin(events):
    task, e1, e2, e3 = events
    out = mcp_api.call("events_list", {"task": "ev-task"})           # дефолт: approved, 30 дн
    assert "бійка" in out and "стара подія" not in out and "на аудит" not in out
    out = mcp_api.call("events_list", {"task": "ev-task", "days": 400})
    assert "стара подія" in out
    out = mcp_api.call("events_list", {"task": "ev-task", "review_status": "pending"})
    assert "на аудит" in out and "бійка" not in out
    out = mcp_api.call("events_list", {"task": "ev-task", "review_status": "all", "days": 400,
                                       "tag": "topic:мігранти, узбек", "region": "Дагестан",
                                       "min_channels": 2, "min_reach": 500})
    assert "бійка" in out and "стара" not in out and "Події: 1" in out
    assert "нічого не знайдено" in mcp_api.call("events_list", {"task": "ev-task", "tag": "topic:немає"})
    with pytest.raises(ToolError, match="review_status"):
        mcp_api.call("events_list", {"review_status": "bogus"})
    with pytest.raises(ToolError, match="order"):
        mcp_api.call("events_list", {"order": "bogus"})


def test_event_show_and_update(events):
    from analysis.models import Event, Tag
    task, e1, e2, e3 = events
    out = mcp_api.call("event_show", {"ref": str(e1.id)})
    assert "topic:мігранти" in out and "nationality:узбек" in out and "Дагестан" in out

    out = mcp_api.call("event_update", {"ref": str(e3.id), "review": "reject", "notes": "дубль"})
    e3.refresh_from_db()
    assert e3.review_status == Event.REVIEW_REJECTED and e3.review_notes == "дубль" \
        and e3.reviewed_at is not None and "відхилено" in out

    mcp_api.call("event_update", {"ref": str(e3.id), "review": "pending"})
    e3.refresh_from_db()
    assert e3.review_status == Event.REVIEW_PENDING and e3.reviewed_at is None

    # теги: наявний — додається; закрита категорія без словника — пропускається з поясненням
    out = mcp_api.call("event_update", {"ref": str(e2.id), "add_tags": "topic:мігранти, nationality:марсіанин",
                                        "remove_tags": "nationality:ніщо", "settlement": "Хасавюрт",
                                        "event_date": "2026-09-01"})
    e2.refresh_from_db()
    assert list(e2.tags.values_list("name", flat=True)) == ["мігранти"]
    assert "НЕ додано" in out and "марсіанин" in out
    assert e2.settlement == "Хасавюрт" and str(e2.event_date) == "2026-09-01"
    out = mcp_api.call("event_update", {"ref": str(e1.id), "remove_tags": "узбек"})
    assert list(e1.tags.values_list("name", flat=True)) == ["мігранти"] and "−nationality:узбек" in out
    with pytest.raises(ToolError, match="категорія:тег"):
        mcp_api.call("event_update", {"ref": str(e1.id), "add_tags": "безкатегорії"})
    with pytest.raises(ToolError, match="review"):
        mcp_api.call("event_update", {"ref": str(e1.id), "review": "maybe"})
    assert "нічого не змінено" in mcp_api.call("event_update", {"ref": str(e1.id)})


def test_tag_categories_lists_examples(events):
    out = mcp_api.call("tag_categories", {"task": "ev-task"})
    assert "topic" in out and "мігранти(1)" in out and "закрита" in out


def test_tag_category_and_tag_crud(events):
    from analysis.models import Event, ResearchRubric, Tag, TagCategory
    task, e1, *_ = events
    out = mcp_api.call("tag_category_create", {
        "key": "Importance", "label": "Важливість", "closed": True,
        "hint": "шкала 1–5", "order": 3})
    assert "створена" in out and "закрита" in out
    cat = TagCategory.objects.get(key="importance")
    assert cat.closed and cat.order == 3 and cat.hint == "шкала 1–5"
    with pytest.raises(ToolError, match="уже є"):
        mcp_api.call("tag_category_create", {"key": "importance", "label": "ще"})
    mcp_api.call("tag_category_update", {"key": "importance", "closed": False, "hint": "-"})
    cat.refresh_from_db()
    assert cat.closed is False and cat.hint == ""
    card = mcp_api.call("tag_category_show", {"key": "importance"})
    assert "відкрита" in card and "підказка" in card

    made = mcp_api.call("tag_create", {"category": "importance", "name": "важливість_4"})
    assert "створено" in made
    again = mcp_api.call("tag_create", {"category": "importance", "name": "Важливість_4"})
    assert "уже був" in again and Tag.objects.filter(category="importance").count() == 1
    with pytest.raises(ToolError, match="немає"):
        mcp_api.call("tag_create", {"category": "немає", "name": "x"})

    listed = mcp_api.call("tags_list", {"category": "importance", "query": "важлив"})
    assert "важливість_4" in listed
    mcp_api.call("tag_update", {"ref": "importance:важливість_4", "name": "важливість_5"})
    tag = Tag.objects.get(category="importance")
    assert tag.name == "важливість_5"
    shown = mcp_api.call("tag_show", {"ref": str(tag.id)})
    assert "важливість_5" in shown

    e1.tags.add(tag)
    with pytest.raises(ToolError, match="confirm=true"):
        mcp_api.call("tag_delete", {"ref": f"#{tag.id}"})
    gone = mcp_api.call("tag_delete", {"ref": str(tag.id), "confirm": True})
    assert "видалено" in gone and "подій 1" in gone
    assert not Tag.objects.filter(pk=tag.id).exists()
    assert not e1.tags.filter(category="importance").exists()

    task.tag_categories.add(cat)
    with pytest.raises(ToolError, match="confirm=true"):
        mcp_api.call("tag_category_delete", {"key": "importance"})
    ResearchRubric.objects.create(task=task, tag_category="importance", tag_name="рубрика")
    with pytest.raises(ToolError, match="рубрики"):
        mcp_api.call("tag_category_delete", {"key": "importance", "confirm": True})
    ResearchRubric.objects.filter(task=task).delete()
    out = mcp_api.call("tag_category_delete", {"key": "importance", "confirm": True})
    assert "видалено" in out and not TagCategory.objects.filter(key="importance").exists()
    assert not task.tag_categories.filter(key="importance").exists()
    assert Event.objects.filter(pk=e1.pk).exists()


def test_event_add_uses_link_service(events, monkeypatch):
    from analysis.models import Event
    from analysis.services import event_by_link
    task, e1, *_ = events
    monkeypatch.setattr(event_by_link, "create_event", lambda t, url, user=None: (e1, False))
    out = mcp_api.call("event_add", {"task": "ev-task", "url": "https://t.me/x/1"})
    assert "уже була" in out and f"#{e1.id}" in out
    monkeypatch.setattr(event_by_link, "create_event",
                        lambda t, url, user=None: (_ for _ in ()).throw(event_by_link.LinkError("закритий канал")))
    with pytest.raises(ToolError, match="закритий канал"):
        mcp_api.call("event_add", {"task": "ev-task", "url": "https://t.me/x/2"})
    with pytest.raises(ToolError, match="посилання"):
        mcp_api.call("event_add", {"task": "ev-task", "url": ""})


# --- публікації: профілі й журнал ---------------------------------------------

def test_publish_config_create_update_show():
    from analysis.models import PublishConfig, Region, Tag
    TaskFactory(slug="pub-task")
    Region.objects.create(name="Республіка Дагестан")
    Tag.objects.create(name="мігранти", category="topic")
    Tag.objects.create(name="важливість 1", category="importance")
    out = mcp_api.call("publish_config_create", {
        "name": "Дагестан-канал", "chat_id": "-1001234567890", "task": "pub-task",
        "tags": "topic:мігранти", "exclude_tags": "importance:важливість 1",
        "regions": "Дагестан", "max_age_days": 3, "bot_token": "123:SECRET"})
    cfg = PublishConfig.objects.get(name="Дагестан-канал")
    assert cfg.task.slug == "pub-task" and cfg.is_active is False and cfg.max_age_days == 3
    assert [t.name for t in cfg.tags.all()] == ["мігранти"]
    assert [t.name for t in cfg.exclude_tags.all()] == ["важливість 1"]
    assert [r.name for r in cfg.regions.all()] == ["Республіка Дагестан"]
    assert "SECRET" not in out and "bot token: заданий" in out and "вимкнений" in out

    out = mcp_api.call("publish_config_update", {"ref": "Дагестан", "is_active": True,
                                                 "tags": "-", "task": "-", "raw_mode": True,
                                                 "publish_from": "2026-09-01"})
    cfg.refresh_from_db()
    assert cfg.is_active and cfg.task is None and cfg.raw_mode and cfg.tags.count() == 0
    assert str(cfg.publish_from) == "2026-09-01" and "tags: очищено" in out
    assert "нічого не змінено" in mcp_api.call("publish_config_update", {"ref": str(cfg.id)})
    with pytest.raises(ToolError, match="forward_account"):
        mcp_api.call("publish_config_update", {"ref": str(cfg.id), "post_as_account": True})
    with pytest.raises(ToolError, match="немає"):
        mcp_api.call("publish_config_update", {"ref": str(cfg.id), "tags": "topic:неіснуючий"})
    assert "сирий" in mcp_api.call("publish_config_show", {"ref": str(cfg.id)})


def test_published_list_and_show():
    from analysis.models import Event, PublishConfig, PublishedEvent
    task = TaskFactory(slug="pub-task2")
    cfg = PublishConfig.objects.create(name="Канал", chat_id="-1001234567890", task=task)
    e1 = Event.objects.create(task=task, event_date="2026-09-20", summary="бійка на ринку")
    e2 = Event.objects.create(task=task, event_date="2026-09-20", summary="реклама")
    p1 = PublishedEvent.objects.create(config=cfg, event=e1, status="published", tg_message_id=42,
                                       post_text="Пост про бійку", published_at=timezone.now())
    PublishedEvent.objects.create(config=cfg, event=e2, status="skipped", ai_verdict=False,
                                  ai_reason="реклама, не подія")
    out = mcp_api.call("published_list", {})
    assert "Пост про бійку" in out and "https://t.me/c/1234567890/42" in out and "реклама" not in out
    out = mcp_api.call("published_list", {"status": "skipped"})
    assert "реклама, не подія" in out and "бійку" not in out
    out = mcp_api.call("published_list", {"status": "all", "config": "Канал", "task": "pub-task2",
                                          "query": "бійк"})
    assert "Публікації: 1" in out
    with pytest.raises(ToolError, match="status"):
        mcp_api.call("published_list", {"status": "bogus"})
    out = mcp_api.call("published_show", {"ref": str(p1.id)})
    assert "Пост про бійку" in out and "t.me/c/1234567890/42" in out
