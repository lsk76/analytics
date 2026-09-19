"""MCP-шар TeleZip: побудова критеріїв, вікна, форматування, ad-hoc вставка.

Мережі тут немає: клієнт підмінюється фейком, який повертає ті самі структури,
що й живий API (перевірені на api.telezip.net 2026-09-18).
"""
import pytest
from django.utils import timezone

from analysis.models import AnalysisTask, Post, TelezipSlot
from analysis.services import mcp_api
from analysis.services.mcp_api import telezip as tz
from analysis.services.mcp_api.registry import ToolError

from .factories import TaskFactory

pytestmark = pytest.mark.django_db


class FakeClient:
    """Async-контекст із тими самими сигнатурами, що й TelezipClient."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _answer(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        val = self.answers.get(name)
        if isinstance(val, Exception):
            raise val
        return val

    async def search(self, criteria, **kw):
        return self._answer("search", criteria, **kw)

    async def search_stats(self, criteria):
        return self._answer("search_stats", criteria)

    async def search_channels(self, **kw):
        return self._answer("search_channels", **kw)

    async def search_users(self, **kw):
        return self._answer("search_users", **kw)

    async def users_by_username(self, names):
        return self._answer("users_by_username", names)

    async def message_context(self, *args, **kw):
        return self._answer("message_context", *args, **kw)

    async def find_posts_range(self, *args, **kw):
        return self._answer("find_posts_range", *args, **kw)

    async def index_stats(self):
        return self._answer("index_stats")

    async def search_macros(self):
        return self._answer("search_macros")


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def use(**answers):
        client = FakeClient(**answers)
        holder["client"] = client
        monkeypatch.setattr(tz, "_client", lambda timeout=180: client)
        return client
    return use


MSG = {"mid": 1, "channel_id": 77, "channel_name": "sakhaday", "message_id": 228790,
       "message_url": "https://t.me/sakhaday/228790", "date": "2026-09-17T08:35:00+02:00",
       "content": "Чиновница принесла извинения", "content_hash": "h1",
       "from_user_name": "sakhaday", "from_user_id": 5}


# --- критерії ---------------------------------------------------------------

def test_criteria_requires_at_least_one_search_mode():
    with pytest.raises(ToolError, match="хоча б один критерій"):
        tz._criteria()


def test_criteria_adds_star_term_for_channel_only_filter():
    """API не приймає фільтр по каналу без тексту — підставляємо `*` самі."""
    c = tz._criteria(channels="@sakhaday, 123")
    assert c["searchTerm"] == "*"
    assert c["channelNames"] == ["sakhaday"] and c["channelIds"] == [123]


def test_criteria_maps_all_modes_and_filters():
    c = tz._criteria(query="дрон", exact="сво", regex="\\d{16}", channel_term="крым",
                     users="@ivan, 42", languages="ru uk", tags="army",
                     exclude_tags="ads", has_media=True, unique=False, source="darkzip",
                     thread=555, extra='{"customField": 1}')
    assert c["searchTerm"] == "дрон" and c["exactTerm"] == "сво"
    assert c["regexPattern"] == "\\d{16}" and c["channelTerm"] == "крым"
    assert c["fromUserName"] == ["ivan"] and c["fromUserId"] == [42]
    assert c["languages"] == ["ru", "uk"]
    assert c["requiredTags"] == ["army"] and c["excludedTags"] == ["ads"]
    assert c["hasMedia"] is True and c["unique"] is False
    assert c["source"] == "DarkZip" and c["topMessageId"] == 555
    assert c["customField"] == 1


def test_criteria_rejects_bad_source_and_extra():
    with pytest.raises(ToolError, match="source"):
        tz._criteria(query="x", source="вконтакте")
    with pytest.raises(ToolError, match="не JSON"):
        tz._criteria(query="x", extra="{зламано}")


# --- вікно ------------------------------------------------------------------

def test_window_defaults_to_last_n_days():
    d_from, d_to, span = tz._window(3, "", "")
    assert span == 3 and d_to.date() == timezone.now().date()


def test_window_rejects_half_range_and_too_long():
    with pytest.raises(ToolError, match="ОБИДВІ дати"):
        tz._window(1, "2026-01-01", "")
    with pytest.raises(ToolError, match="завелике"):
        tz._window(400, "", "")
    # статистика не тягне повідомлення — їй довге вікно дозволене
    assert tz._window(400, "", "", allow_long=True)[2] == 400


# --- пошук і статистика -----------------------------------------------------

def test_tz_search_renders_volume_channels_and_samples(fake):
    fake(search={"total": 2, "next_page_token": "TOK", "messages": [MSG, dict(MSG, mid=2)]})
    out = mcp_api.call("tz_search", {"query": "Якутия", "channels": "@sakhaday",
                                     "days": 3, "samples": 1})
    assert "@sakhaday" in out and "t.me/sakhaday/228790" in out
    assert "page_token=TOK" in out          # видно, що є наступна сторінка
    assert "09-17:2" in out                 # розкладка по днях


def test_tz_search_warns_about_negation_and_star(fake):
    fake(search={"total": 0, "next_page_token": None, "messages": []})
    out = mcp_api.call("tz_search", {"query": "дрон -(всу фронт)"})
    assert "негація" in out
    out = mcp_api.call("tz_search", {"query": "*"})
    assert "весь індекс" in out


def test_tz_stats_summarises_without_downloading(fake):
    client = fake(search_stats={
        "messageCount": 50, "userCount": 7, "channelCount": 2,
        "channels": [{"id": 1, "name": "sakhaday", "messageCount": 40},
                     {"id": 2, "name": "ykt", "messageCount": 10}],
        "firstMessageDate": "2026-09-16T00:00:47+03:00",
        "lastMessageDate": "2026-09-17T05:58:44+03:00",
        "messagesPerHour": {"2026-09-16T00:00:00Z": 20, "2026-09-17T01:00:00Z": 30}})
    out = mcp_api.call("tz_stats", {"query": "Якутия", "days": 2})
    assert "повідомлень" in out and "50" in out
    assert "@sakhaday" in out and "80%" in out      # частка каналу
    assert "09-16:20" in out and "09-17:30" in out
    # статистика рахується на боці TeleZip — повідомлення не викачуються
    assert [c[0] for c in client.calls] == ["search_stats"]
    assert "1 запит" in out and "$0.10" in out      # ціна виклику перед очима


def test_tz_calibrate_projects_volume_and_repost_ratio(fake):
    stats = {"messageCount": 100, "userCount": 10, "channelCount": 5, "channels": [],
             "messagesPerHour": {}}
    client = FakeClient()
    calls = {"n": 0}

    async def search_stats(criteria):
        calls["n"] += 1
        return dict(stats, messageCount=100 if criteria.get("unique") else 300)
    client.search_stats = search_stats
    import analysis.services.mcp_api.telezip as mod
    mod._client = lambda timeout=180: client

    out = mcp_api.call("tz_calibrate", {"query": "мигрант", "days": 1, "project_days": 30})
    assert "×3.0" in out                      # репости
    assert "~3000" in out                     # проєкція 100/добу × 30
    assert calls["n"] == 2
    assert "$0.20" in out                     # два виклики по $0.10


# --- довідники --------------------------------------------------------------

def test_tz_context_default_anchor_and_friendly_404(fake):
    fake(message_context={"channelId": 77, "messageId": 228790,
                          "anchor": {"messageId": 228790, "date": "2026-09-17T08:35",
                                     "content": "якір"},
                          "before": [{"messageId": 228789, "date": "2026-09-17T08:17",
                                      "content": "до"}],
                          "after": []})
    out = mcp_api.call("tz_context", {"channel": "77", "message_id": 228790})
    assert "▶ " in out and "якір" in out

    fake(message_context=ToolError("TeleZip 404: ANCHOR_NOT_FOUND"))
    with pytest.raises(ToolError, match="anchor_date"):
        mcp_api.call("tz_context", {"channel": "77", "message_id": 1})


def test_tz_user_maps_username_to_id_and_survives_empty_profile(fake):
    fake(users_by_username={"durov": [1006503122]},
         search_users=ToolError("TeleZip 404: not found"))
    out = mcp_api.call("tz_user", {"ref": "@durov"})
    assert "1006503122" in out
    assert "немає" in out            # профілю нема, але id знайдено — не падаємо


def test_tz_macros_filters(fake):
    fake(search_macros=[{"name": "##вч_93498", "value": '"93498 часть"~2'},
                        {"name": "##дрон", "value": "дрон fpv"}])
    out = mcp_api.call("tz_macros", {"filter": "вч"})
    assert "##вч_93498" in out and "##дрон" not in out


# --- дії --------------------------------------------------------------------

def test_tz_ingest_dry_run_writes_nothing(fake):
    task = TaskFactory(pipeline=AnalysisTask.PIPELINE_EVENTS, slug="ev-task")
    fake(find_posts_range=[MSG])
    out = mcp_api.call("tz_ingest", {"task": "ev-task", "query": "*",
                                     "channels": "@sakhaday"})
    assert "нічого не записано" in out
    assert Post.objects.count() == 0


def test_tz_ingest_writes_posts_like_collect_worker(fake):
    task = TaskFactory(pipeline=AnalysisTask.PIPELINE_EVENTS, slug="ev-task2")
    fake(find_posts_range=[MSG])
    mcp_api.call("tz_ingest", {"task": "ev-task2", "query": "*",
                               "channels": "@sakhaday", "dry_run": False})
    post = Post.objects.get()
    assert post.task_id == task.id and post.url == MSG["message_url"]
    assert post.stage == Post.STAGE_COLLECTED          # далі його бере конвеєр
    assert post.channel_name == "sakhaday" and post.telezip_mid == 1
    assert post.classification["_tz_channel_id"] == 77
    # повторний прогін не дублює (unique task+url)
    mcp_api.call("tz_ingest", {"task": "ev-task2", "query": "*",
                               "channels": "@sakhaday", "dry_run": False})
    assert Post.objects.count() == 1


def test_tz_ingest_refuses_monitor_pipeline():
    TaskFactory(pipeline=AnalysisTask.PIPELINE_MONITOR, slug="mon-task")
    with pytest.raises(ToolError, match="run_create"):
        mcp_api.call("tz_ingest", {"task": "mon-task", "query": "*"})


def test_tz_slots_set_changes_global_limit():
    # таблиця слотів спільна для всіх процесів і могла лишитись засіяною
    TelezipSlot.objects.all().delete()
    TelezipSlot.objects.bulk_create([TelezipSlot(slot=0), TelezipSlot(slot=1)])
    mcp_api.call("tz_slots_set", {"count": 1})
    assert list(TelezipSlot.objects.values_list("slot", flat=True)) == [0]
    with pytest.raises(ToolError, match="1..8"):
        mcp_api.call("tz_slots_set", {"count": 99})


def test_tz_probe_validates_input():
    with pytest.raises(ToolError, match="GET і POST"):
        mcp_api.call("tz_probe", {"endpoint": "/v4/stats", "method": "DELETE"})
    with pytest.raises(ToolError, match="починатись"):
        mcp_api.call("tz_probe", {"endpoint": "v4/stats"})


def test_tz_syntax_is_offline_and_covers_modes():
    out = mcp_api.call("tz_syntax")
    for mode in ("query", "exact", "regex", "channel_term", "users", "source"):
        assert mode in out
