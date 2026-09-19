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

def test_tz_find_returns_messages_with_links(fake):
    fake(search={"total": 2, "next_page_token": "TOK", "messages": [MSG, dict(MSG, mid=2)]})
    out = mcp_api.call("tz_find", {"text": "Якутия", "channel": "@sakhaday",
                                   "days": 3, "samples": 1})
    assert "@sakhaday" in out and "t.me/sakhaday/228790" in out
    assert "page_token=TOK" in out and "09-17:2" in out


def test_tz_find_stats_mode_counts_without_downloading(fake):
    client = fake(search_stats={
        "messageCount": 50, "userCount": 7, "channelCount": 2,
        "channels": [{"id": 1, "name": "sakhaday", "messageCount": 40},
                     {"id": 2, "name": "ykt", "messageCount": 10}],
        "firstMessageDate": "2026-09-16T00:00:47+03:00",
        "lastMessageDate": "2026-09-17T05:58:44+03:00",
        "messagesPerHour": {"2026-09-16T00:00:00Z": 20, "2026-09-17T01:00:00Z": 30}})
    out = mcp_api.call("tz_find", {"text": "Якутия", "days": 2, "stats": True})
    assert "50" in out and "@sakhaday" in out and "80%" in out
    assert "09-16:20" in out and "09-17:30" in out
    # лічильники рахує TeleZip — повідомлення не викачуються
    assert [c[0] for c in client.calls] == ["search_stats"]


def test_stats_mode_does_not_send_unique(fake):
    """/FindStats не приймає unique — не шлемо його й не обіцяємо співвідношення."""
    client = fake(search_stats={"messageCount": 1, "channels": [], "messagesPerHour": {}})
    mcp_api.call("tz_find", {"text": "x", "stats": True, "unique": True})
    assert "unique" not in client.calls[0][1]


def test_tz_find_warns_about_negation_and_star(fake):
    fake(search={"total": 0, "next_page_token": None, "messages": []})
    assert "негація" in mcp_api.call("tz_find", {"text": "дрон -(всу фронт)"})
    assert "весь індекс" in mcp_api.call("tz_find", {"text": "*"})


# --- довідники --------------------------------------------------------------

# --- контракт описів --------------------------------------------------------
# Описи — це ІНТЕРФЕЙС для моделі: вона бачить лише їх, а синтаксис TeleZip не
# збігається з очікуваннями (пробіл = АБО). Тому перевіряємо їх як код.

def test_every_param_of_every_tool_is_documented():
    """Не лише telezip: без опису модель вгадує значення будь-якого аргумента.

    Саме на цьому зловили дірку 2026-09-19 — описи були тільки в tz_*, а
    операційні інструменти (акаунти, збори, джерела) лишались голими.
    """
    missing = [(m["name"], p["name"]) for m in mcp_api.manifest()
               for p in m["params"] if not p["doc"]]
    assert not missing, f"параметри без опису: {missing}"


def test_enumerated_params_list_their_allowed_values():
    """Там, де значення обмежене набором, набір має бути в описі."""
    by_name = {m["name"]: m for m in mcp_api.manifest()}
    cases = [("tasks_list", "pipeline", ["events", "monitor", "infospace", "tgsearch"]),
             ("runs_list", "status", ["collecting", "awaiting_agent", "cancelled"]),
             ("sources_list", "kind", ["telegram", "rss", "web"]),
             ("events_stats", "review_status", ["approved", "pending", "rejected", "all"]),
             ("account_jobs", "kind", ["warm_up", "test_bot"])]
    for tool, param, values in cases:
        doc = [p for p in by_name[tool]["params"] if p["name"] == param][0]["doc"]
        for v in values:
            assert v in doc, f"{tool}.{param}: у описі немає значення «{v}»"


def test_date_params_say_the_format_and_inclusivity():
    run_create = {p["name"]: p["doc"] for p in
                  [m for m in mcp_api.manifest() if m["name"] == "run_create"][0]["params"]}
    assert "YYYY-MM-DD" in run_create["date_from"]
    assert "ВКЛЮЧНО" in run_create["date_to"]


def test_descriptions_do_not_mention_removed_tools_or_renamed_params():
    """Найчастіша гниль у доці: імена, які пережили перейменування."""
    dead = ["tz_probe", "tz_syntax", "tz_calibrate", "tz_stats(", "tz_search(",
            "channel_term=", "query=\""]
    for m in mcp_api.manifest():
        blob = m["doc"] + " ".join(p["doc"] for p in m["params"])
        for name in dead:
            assert name not in blob, f"{m['name']}: згадка неіснуючого «{name}»"


def test_tool_description_carries_the_whole_docstring():
    """У схему має йти ВЕСЬ докстрінг, а не перший абзац: застереження в кінці."""
    find = [m for m in mcp_api.manifest() if m["name"] == "tz_find"][0]
    assert "ПРОБІЛ = АБО" in find["doc"], "застереження не дійшло до опису"
    assert find["summary"] and "\n" not in find["summary"]


def test_telezip_surface_mirrors_the_api():
    """Три ендпоінти TeleZip — три інструменти, без вигаданих обгорток."""
    tz = {m["name"] for m in mcp_api.manifest() if m["group"] == "telezip"}
    assert {"tz_find", "tz_channels", "tz_users"} <= tz
    # ці були обгортками над тим самим /FIND і прибрані
    assert not ({"tz_search", "tz_stats", "tz_calibrate", "tz_channel",
                 "tz_channel_posts", "tz_user", "tz_context", "tz_macros",
                 "tz_probe", "tz_syntax", "tz_ingest", "tz_slots_set"} & tz)
    # лишається рівно три ендпоінти + діагностика
    assert tz == {"tz_find", "tz_channels", "tz_users", "tz_status"}


def test_query_docs_warn_about_the_or_default():
    """Найчастіша помилка: пробіл сприймають як І."""
    for name in ("tz_find",):
        spec = [m for m in mcp_api.manifest() if m["name"] == name][0]
        q = [p for p in spec["params"] if p["name"] == "text"][0]
        assert "АБО" in q["doc"] and "+" in q["doc"]


