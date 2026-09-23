"""Моніторинги: задачі, збори (runs), whitelist чатів, джерела інформпростору, зрізи подій."""
from datetime import timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone

from analysis.models import (AnalysisTask, Channel, Event, MonitorChat, Post,
                             ResearchRubric, ResearchRun, Source, SourceSubscription)
from analysis.services.mcp_api import common, fmt, registry
from analysis.services.mcp_api.registry import SCOPE_CREATE, ToolError, tool

# Ключ pipeline має бути видимий агенту як є: скорочення «інформпр.» / слово
# «моніторинг» на всі задачі змушувало плутати п'ять конвеєрів.
_PIPELINE_HOW = {
    "events": "events — події з TeleZip. Збір: run_create. Поля етапів — task_update "
              "з іменами з task_show (classify_prompt = classify_system_prompt).",
    "monitor": "monitor — критика в чатах, не інформпростір. Чати: chat_add. "
               "Промпти: task_update prescreen_prompt, tagger_prompt.",
    "research": "research — тематичне дослідження каналів. Чати: chat_add. "
                "Рубрики: rubric_create. Промпт агента: task_update tagger_prompt.",
    "infospace": "infospace — полінг джерел, не TeleZip. Джерела: source_add. "
                 "Поля етапів — task_update (info_screen_prompt, info_tagger_prompt, "
                 "info_judge_prompt, info_max_age_days, …). run_create цей конвеєр не читає.",
    "tgsearch": "tgsearch — пошук у чатах Telegram, не TeleZip. Чати: chat_add. "
                "Поля: task_update search_terms, stream_regex, prescreen_prompt, tagger_prompt.",
}
_CHAT_PIPES = {"monitor", "research", "tgsearch"}
_TELEZIP_RUNS = {"events", "monitor", "research"}

# Картка task_show = усі поля форми задачі для її конвеєра (ті самі fieldsets,
# що в адмінці). Довгий текст іде окремим блоком і не обрізається.
_TASK_GROUPS = {
    "events": (
        ("Збір", ("telezip_query", "telezip_unique", "search_posts", "search_comments",
                  "drop_linked_comments", "min_channel_subscribers", "collect_chunk_days",
                  "languages")),
        ("Класифікація", ("classify_system_prompt", "tag_categories", "llm_model", "geo_enabled")),
        ("Дедуплікація", ("dedup_window_days", "dedup_pre_thresh", "dedup_cand_thresh",
                          "dedup_judge_prompt", "generic_sides")),
        ("Авто-аудит", ("review_enabled", "review_model", "review_prompt")),
        ("Агент-аудит", ("agent_review_prompt",)),
    ),
    "monitor": (
        ("Збір", ("telezip_query", "collect_chunk_days", "languages")),
        ("Фільтрація", ("mon_min_len", "mon_max_len")),
        ("Прескрін", ("prescreen_model", "prescreen_prompt")),
        ("Тегування", ("tag_categories", "tagger_prompt")),
    ),
    "research": (
        ("Збір", ("collect_chunk_days", "languages")),
        ("Класифікація", ("tagger_prompt",)),
        ("Групування дублів", ("dedup_group_days", "dedup_group_fuzz", "dedup_llm_cluster",
                               "dedup_cluster_prompt")),
        ("Агент-аудит", ("research_audit_enabled", "research_audit_prompt")),
    ),
    "infospace": (
        ("Джерела", ("info_max_age_days",)),
        ("Скрін", ("info_screen_model", "info_screen_prompt", "tag_categories",
                   "info_tagger_prompt", "geo_enabled")),
        ("Зіставлення", ("info_judge_prompt", "info_match_window_hours",
                         "info_update_summaries", "llm_model")),
        ("Ретеншн", ("info_retention_days",)),
    ),
    "tgsearch": (
        ("Стрім", ("stream_regex", "stream_interval_min", "stream_media_chat_id")),
        ("Пошук", ("search_terms", "search_days", "search_limit_per_term")),
        ("Фільтр", ("prescreen_enabled", "prescreen_model", "prescreen_prompt")),
        ("Тегування", ("tag_categories", "tagger_prompt", "llm_model")),
    ),
}
_LONG_FIELDS = {
    "description", "telezip_query", "classify_system_prompt", "dedup_judge_prompt",
    "review_prompt", "agent_review_prompt", "search_terms", "stream_regex",
    "prescreen_prompt", "tagger_prompt", "dedup_cluster_prompt", "research_audit_prompt",
    "info_screen_prompt", "info_tagger_prompt", "info_judge_prompt",
}
_HEAD_FIELDS = ("display_name", "name", "slug", "description", "pipeline", "is_active",
                "owner", "created_at", "updated_at")

# Поля форми, які task_update приймає ПІД ТИМ САМИМ ім'ям, що в картці [field].
# Старі аліаси (classify_prompt, unique, chunk_days, min_subscribers, мови,
# info_*_prompt, tag_categories) лишаються окремо.
_TEXT_FIELDS = (
    "dedup_judge_prompt", "review_model", "review_prompt", "agent_review_prompt",
    "prescreen_model", "prescreen_prompt", "tagger_prompt", "dedup_cluster_prompt",
    "research_audit_prompt", "search_terms", "stream_regex", "stream_media_chat_id",
    "info_screen_model",
)
_BOOL_FIELDS = (
    "drop_linked_comments", "dedup_llm_cluster", "research_audit_enabled",
    "info_update_summaries", "prescreen_enabled",
)
_INT_FIELDS = (
    "dedup_pre_thresh", "dedup_cand_thresh", "mon_min_len", "mon_max_len",
    "dedup_group_days", "dedup_group_fuzz", "info_max_age_days",
    "info_match_window_hours", "info_retention_days", "stream_interval_min",
    "search_days", "search_limit_per_term",
)
_JSON_LIST_FIELDS = ("generic_sides",)
_PIPE_FIELDS = {}
for _pipe, _groups in _TASK_GROUPS.items():
    for _title, _fields in _groups:
        for _name in _fields:
            _PIPE_FIELDS.setdefault(_name, set()).add(_pipe)


def _field_param_doc(name, kind):
    label = AnalysisTask._meta.get_field(name).verbose_name
    pipes = ", ".join(sorted(_PIPE_FIELDS.get(name, ())))
    if kind == "text":
        rule = "Порожньо = не змінювати; '-' = очистити."
    elif kind == "bool":
        rule = "Не передавати = не змінювати."
    elif kind == "int":
        rule = "Не передавати = не змінювати. 0 — валідне значення."
    else:
        rule = "Список через кому. Порожньо = не змінювати; '-' = очистити."
    return f"{label}. Лише конвеєри: {pipes}. {rule}"


_EXTRA_PARAM_DOCS = {}
_EXTRA_PARAM_DOCS.update({n: _field_param_doc(n, "text") for n in _TEXT_FIELDS})
_EXTRA_PARAM_DOCS.update({n: _field_param_doc(n, "bool") for n in _BOOL_FIELDS})
_EXTRA_PARAM_DOCS.update({n: _field_param_doc(n, "int") for n in _INT_FIELDS})
_EXTRA_PARAM_DOCS.update({n: _field_param_doc(n, "list") for n in _JSON_LIST_FIELDS})


def _guard_task_field(pipeline, name):
    pipes = _PIPE_FIELDS.get(name)
    if pipes and pipeline not in pipes:
        raise ToolError(
            f"конвеєр {pipeline}, поле {name} є лише в: {', '.join(sorted(pipes))}. "
            + _PIPELINE_HOW.get(pipeline, ""))


def _coerce_task_field(name, value):
    field = AnalysisTask._meta.get_field(name)
    kind = field.get_internal_type()
    if kind == "JSONField":
        if str(value).strip() == "-":
            return []
        parts = [x.strip() for x in str(value).replace("\n", ",").split(",") if x.strip()]
        return parts
    if kind == "BooleanField":
        return bool(value)
    if kind in ("PositiveSmallIntegerField", "PositiveIntegerField",
                "SmallIntegerField", "IntegerField"):
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ToolError(f"{name}: очікується ціле число, отримано «{value}»")
        if n < 0:
            raise ToolError(f"{name}: від'ємне значення")
        limit = 32767 if kind == "PositiveSmallIntegerField" else 2147483647
        if n > limit:
            raise ToolError(f"{name}: максимум {limit}")
        return n
    text = "" if str(value).strip() == "-" else str(value)
    max_length = getattr(field, "max_length", None)
    if max_length and len(text) > max_length:
        raise ToolError(f"{name}: довше за {max_length} символів")
    return text


def _collect_task_fields(**kwargs):
    """Лише передані значення: порожній рядок і None = параметр не чіпали."""
    out = {}
    for name in _TEXT_FIELDS + _JSON_LIST_FIELDS:
        if kwargs.get(name):
            out[name] = kwargs[name]
    for name in _BOOL_FIELDS + _INT_FIELDS:
        if kwargs.get(name) is not None:
            out[name] = kwargs[name]
    return out


def _apply_task_fields(task, extras, changed):
    for name, value in extras.items():
        _guard_task_field(task.pipeline, name)
        coerced = _coerce_task_field(name, value)
        setattr(task, name, coerced)
        if coerced == "" or coerced == []:
            changed.append(f"{name} очищено")
        elif isinstance(coerced, bool):
            changed.append(f"{name}={fmt.flag(coerced)}")
        else:
            shown = coerced if not isinstance(coerced, str) else f"({len(coerced)} симв)"
            changed.append(f"{name}={shown}" if not isinstance(coerced, str)
                           else f"{name} {shown}")


def _field_label(name):
    """Підпис із іменем поля: агент інакше не зіставляє «Скрін: системний промпт» з info_screen_prompt."""
    return f"{AnalysisTask._meta.get_field(name).verbose_name} [{name}]"


def _model_fallback(task, name):
    from django.conf import settings
    if name == "llm_model":
        return settings.LLM_MODEL
    if name in ("info_screen_model", "prescreen_model"):
        return task.llm_model or settings.LLM_MODEL
    if name == "review_model":
        return "anthropic/claude-sonnet-4.6"
    return ""


def _prompt_default(task, name):
    """Текст, який рантайм підставить, коли поле в базі порожнє. None — підстановки немає."""
    if name == "info_screen_prompt":
        from analysis.services.infospace.prompts import INFO_SCREEN_PROMPT
        return INFO_SCREEN_PROMPT
    if name == "info_judge_prompt":
        from analysis.services.infospace.prompts import INFO_JUDGE_PROMPT
        return INFO_JUDGE_PROMPT
    if name == "dedup_judge_prompt":
        from analysis.services.pipeline import _DEFAULT_JUDGE
        return _DEFAULT_JUDGE
    if name == "review_prompt":
        from analysis.services.review import _DEFAULT_REVIEW_SYS
        return _DEFAULT_REVIEW_SYS
    if name == "agent_review_prompt":
        from analysis.models import _default_agent_review_prompt
        return _default_agent_review_prompt()
    if name == "prescreen_prompt" and task.pipeline == AnalysisTask.PIPELINE_MONITOR:
        from analysis.pilot.prompts import PRESCREEN_SYSTEM_PROMPT_COMPACT
        return PRESCREEN_SYSTEM_PROMPT_COMPACT
    if name == "tagger_prompt" and task.pipeline == AnalysisTask.PIPELINE_MONITOR:
        from analysis.pilot.prompts import TAGGER_SYSTEM_PROMPT
        return TAGGER_SYSTEM_PROMPT
    if name == "dedup_cluster_prompt":
        from analysis.services.research_stages import CLUSTER_PROMPT
        return CLUSTER_PROMPT
    if name == "research_audit_prompt":
        from analysis.models import _default_research_audit_prompt
        return _default_research_audit_prompt()
    return None


def _short_value(task, name):
    if name == "owner":
        return task.owner.username if task.owner_id else "—"
    if name == "pipeline":
        return f"{task.pipeline} — {task.get_pipeline_display()}"
    if name in ("created_at", "updated_at"):
        dt = getattr(task, name)
        return f"{timezone.localtime(dt):%Y-%m-%d %H:%M} ({fmt.ago(dt)})" if dt else "—"
    if name == "tag_categories":
        return ", ".join(c.key for c in task.tag_categories.all()) or "—"
    if name == "languages":
        if task.languages:
            return ", ".join(task.languages)
        if task.pipeline == AnalysisTask.PIPELINE_MONITOR:
            return "порожньо → ru"
        return "порожньо (без фільтра)"
    if name == "generic_sides":
        if task.generic_sides:
            return ", ".join(task.generic_sides)
        from analysis.services.pipeline import GENERIC_SIDES
        return "порожньо → " + ", ".join(sorted(GENERIC_SIDES))
    if name in ("llm_model", "info_screen_model", "prescreen_model", "review_model"):
        raw = (getattr(task, name) or "").strip()
        return raw or ("порожньо → " + _model_fallback(task, name))
    val = getattr(task, name)
    if isinstance(val, bool):
        return fmt.flag(val)
    if val is None or val == "":
        return "—"
    return str(val)


def _long_block(task, name):
    raw = getattr(task, name)
    raw = raw.strip() if isinstance(raw, str) else ("" if raw is None else str(raw).strip())
    if raw:
        body = raw
    else:
        default = _prompt_default(task, name)
        if default and default.strip():
            body = ("порожньо в базі — нижче дефолт із коду (саме він іде в LLM)\n\n"
                    + default.strip())
        elif name == "classify_system_prompt":
            body = ("порожньо — доменних правил немає; до промпта доклеюється "
                    "лише JSON-схема з категорій тегів")
        elif name == "stream_regex":
            body = "порожньо (стрім вимкнено)"
        elif name == "info_tagger_prompt":
            body = "порожньо (лише підказки категорій тегів)"
        else:
            body = "порожньо"
    return f"### {_field_label(name)}\n{body}"


def _effective_llm_section(task):
    """Текст, який реально отримує модель, а не сире поле і не підказка категорії."""
    pipe = task.pipeline
    if pipe == AnalysisTask.PIPELINE_EVENTS:
        from analysis.services.stages import build_classify_prompt
        body = ("Це classify_prompt: поле classify_system_prompt плюс JSON-схема тегів. "
                "Саме цей текст іде в LLM на стадії classify.\n\n"
                + build_classify_prompt(task))
    elif pipe == AnalysisTask.PIPELINE_INFOSPACE:
        from analysis.services.infospace.prompts import INFO_JUDGE_PROMPT, build_screen_prompt
        judge = (task.info_judge_prompt or INFO_JUDGE_PROMPT).strip()
        tagger = (task.info_tagger_prompt or "").strip()
        if tagger and not task.tag_categories.exists():
            tagger_body = (tagger + "\n\nУ зібраний скрін цей текст не входить, "
                           "доки в задачі немає категорій тегів: "
                           "task_update tag_categories=ключ.")
        elif tagger:
            tagger_body = tagger
        else:
            tagger_body = ("порожньо — у зібраний скрін цей блок не додається, "
                           "лишаються лише підказки категорій.\n"
                           "Записати: task_update info_tagger_prompt=\"…\" "
                           "(\"-\" очистити).")
        body = ("Параметра classify_prompt у infospace немає — він лише для events. "
                "У LLM на скріні іде зібраний текст: info_screen_prompt + схема тегів "
                "+ підказки категорій + info_tagger_prompt.\n\n"
                "### Зібраний скрін-промпт\n" + build_screen_prompt(task)
                + "\n\n### info_tagger_prompt\n" + tagger_body
                + "\n\n### Суддя зіставлення [info_judge_prompt]\n" + judge)
    elif pipe == AnalysisTask.PIPELINE_MONITOR:
        from analysis.pilot.prompts import (PRESCREEN_SYSTEM_PROMPT_COMPACT,
                                            TAGGER_SYSTEM_PROMPT)
        pre = (task.prescreen_prompt or PRESCREEN_SYSTEM_PROMPT_COMPACT).strip()
        tag = (task.tagger_prompt or TAGGER_SYSTEM_PROMPT).strip()
        body = ("classify_prompt цей конвеєр не читає. У LLM ідуть два тексти.\n\n"
                "### prescreen_prompt\n" + pre + "\n\n### tagger_prompt\n" + tag)
    elif pipe == AnalysisTask.PIPELINE_RESEARCH:
        from analysis.services.research_stages import _agent_prompt
        body = ("classify_prompt цей конвеєр не читає. У агента іде tagger_prompt "
                "плюс рубрики.\n\n" + _agent_prompt(task))
    elif pipe == AnalysisTask.PIPELINE_TGSEARCH:
        pre = (task.prescreen_prompt or "").strip() or "порожньо (стадія не стартує)"
        tag = (task.tagger_prompt or "").strip() or "порожньо (стадія не стартує)"
        body = ("classify_prompt цей конвеєр не читає. У LLM ідуть тексти з полів задачі.\n\n"
                "### prescreen_prompt\n" + pre + "\n\n### tagger_prompt\n" + tag)
    else:
        return ""
    return fmt.section("Промпт, який іде в LLM", body)


def _task_config(task):
    groups = (("Задача", _HEAD_FIELDS),) + _TASK_GROUPS.get(
        task.pipeline, _TASK_GROUPS["events"])
    parts = []
    for i, (title, fields) in enumerate(groups):
        shorts, longs = [], []
        for name in fields:
            if name in _LONG_FIELDS:
                longs.append(_long_block(task, name))
            else:
                shorts.append((_field_label(name), _short_value(task, name)))
        lead = _PIPELINE_HOW.get(task.pipeline, task.pipeline) if i == 0 else ""
        body = fmt.joinsec(lead, fmt.kv(shorts), *longs)
        heading = f"Задача #{task.id} {task.slug} — {task.name}" if i == 0 else title
        parts.append(fmt.section(heading, body))
    return fmt.joinsec(*parts)


@tool("tasks_list", group="monitoring", params={
      "pipeline": 'Конвеєр: events | monitor | research | infospace | tgsearch. Порожньо = усі.',
      "active_only": "true — лише активні задачі."})
def tasks_list(pipeline: str = "", active_only: bool = False):
    """Список задач. Колонка «конвеєр» — точний ключ: events, monitor, research, infospace, tgsearch.

    Це п'ять різних конвеєрів, не різновиди monitor. events — події TeleZip,
    monitor — критика в чатах, research — тематичне, infospace — полінг джерел,
    tgsearch — пошук у чатах Telegram.
    """
    qs = common.scope_tasks(AnalysisTask.objects).order_by("id")
    if pipeline:
        qs = qs.filter(pipeline=pipeline)
    if active_only:
        qs = qs.filter(is_active=True)
    # агрегати — по одному запиту на таблицю (див. коментар у service_health:
    # «останній рядок задачі» в циклі на мільйонах постів коштує секунди)
    posts_agg = {r["task_id"]: r for r in Post.objects.order_by()
                 .values("task_id").annotate(n=Count("id"), last=Max("posted_at"))}
    events_agg = {r["task_id"]: r for r in Event.objects.order_by()
                  .values("task_id").annotate(n=Count("id"))}
    chats_agg = {r["task_id"]: r["n"] for r in
                 MonitorChat.objects.filter(is_active=True).order_by()
                 .values("task_id").annotate(n=Count("id"))}
    subs_agg = {r["task_id"]: r["n"] for r in
                SourceSubscription.objects.filter(is_active=True).order_by()
                .values("task_id").annotate(n=Count("id"))}
    rows = []
    for t in qs:
        pa = posts_agg.get(t.id, {})
        rows.append([
            f"#{t.id}", fmt.flag(t.is_active), t.slug,
            t.pipeline,
            fmt.trunc(t.name, 34),
            chats_agg.get(t.id, "") or "",
            subs_agg.get(t.id, "") or "",
            pa.get("n", 0),
            events_agg.get(t.id, {}).get("n", 0),
            fmt.ago(pa.get("last")),
        ])
    if not rows:
        return "задач за цим фільтром немає"
    return fmt.table(["id", "акт", "slug", "конвеєр", "назва", "чати", "джер",
                      "постів", "подій", "ост. матеріал"], rows)


def _task_gaps(task):
    """Чого бракує, щоб конвеєр цієї задачі взагалі мав що обробляти."""
    gaps = []
    pipe = task.pipeline
    if pipe in ("events", "monitor") and not (task.telezip_query or "").strip():
        gaps.append("немає telezip_query — task_update telezip_query=… "
                    "(діалект v3: пробіл = АБО, І — це +)")
    if pipe in _CHAT_PIPES and not task.monitor_chats.filter(is_active=True).exists():
        gaps.append("немає активних чатів — chat_add task=" + task.slug + " channel=@…")
    if pipe == AnalysisTask.PIPELINE_RESEARCH and not task.rubrics.filter(is_active=True).exists():
        gaps.append("немає рубрик — rubric_create task=" + task.slug
                    + " tag_category=… tag_name=… keywords=…")
    if pipe == AnalysisTask.PIPELINE_INFOSPACE:
        if not task.source_subscriptions.filter(is_active=True).exists():
            gaps.append("немає джерел — source_add url=… task=" + task.slug)
        if (task.info_tagger_prompt or "").strip() and not task.tag_categories.exists():
            gaps.append("info_tagger_prompt заданий, а категорій тегів немає — "
                        "task_update tag_categories=ключ (інакше правила в скрін не входять)")
    if pipe == AnalysisTask.PIPELINE_TGSEARCH:
        if not (task.search_terms or "").strip() and not (task.stream_regex or "").strip():
            gaps.append("немає ні search_terms, ні stream_regex — пошук і стрім вимкнені")
        if not (task.prescreen_prompt or "").strip():
            gaps.append("порожній prescreen_prompt — стадія фільтра не стартує")
        if not (task.tagger_prompt or "").strip():
            gaps.append("порожній tagger_prompt — стадія тегів не стартує")
    if not gaps:
        return fmt.section("Далі", _PIPELINE_HOW.get(pipe, pipe))
    return fmt.section("Щоб запустити, бракує", "\n".join(f"- {g}" for g in gaps))


@tool("task_show", group="monitoring", params={"ref": 'Задача: числовий id, slug або частина назви. Неоднозначність або чужа задача — відповість «не знайдено».'})
def task_show(ref: str):
    """Картка задачі. Перший блок — «Промпт, який іде в LLM»: зібраний текст моделі.

    classify_prompt є лише в events (поле classify_system_prompt плюс схема тегів).
    В infospace в модель іде зібраний info_screen_prompt; підказка категорії — одне
    речення всередині нього, її показує tag_category_show, це не весь промпт.
    """
    t = common.resolve_task(ref)
    parts = [_effective_llm_section(t), _task_config(t), _task_gaps(t)]

    stages = (Post.objects.filter(task=t).order_by().values("stage")
              .annotate(n=Count("id")))
    order = [s for s, _ in Post.STAGE_CHOICES]
    rows = sorted(((r["stage"], r["n"]) for r in stages),
                  key=lambda r: order.index(r[0]) if r[0] in order else 99)
    parts.append(fmt.section("Пости по стадіях",
                             ", ".join(f"{s}: {n}" for s, n in rows) or "постів немає"))

    ev = Event.objects.filter(task=t)
    rv = dict(ev.order_by().values_list("review_status").annotate(n=Count("id")))
    month = timezone.now().date() - timedelta(days=30)
    parts.append(fmt.section("Події", fmt.kv([
        ("усього", ev.count()),
        ("аудит", ", ".join(f"{k}: {v}" for k, v in sorted(rv.items())) or "—"),
        ("за 30 днів", ev.filter(event_date__gte=month).count()),
    ])))

    if t.pipeline in _CHAT_PIPES:
        chats = t.monitor_chats.all()
        parts.append(fmt.section("Чати", fmt.kv([
            ("активних", chats.filter(is_active=True).count()),
            ("усього", chats.count()),
            ("у стрімі", chats.filter(is_active=True, stream_enabled=True).count()),
            ("без акаунта", chats.filter(is_active=True, tg_account__isnull=True).count()),
        ])) + "\n(деталі: chats_list task=" + t.slug + "; додати: chat_add)")
    if t.pipeline == AnalysisTask.PIPELINE_RESEARCH:
        rubrics = list(t.rubrics.all())
        body = "\n".join(_rubric_line(r) for r in rubrics) if rubrics else "рубрик немає"
        parts.append(fmt.section("Рубрики", body))
    if t.pipeline == "infospace":
        subs = t.source_subscriptions.all()
        bad = subs.filter(is_active=True).filter(
            Q(source__quality_ok=False) | Q(source__consecutive_failures__gte=3)).count()
        parts.append(fmt.section("Джерела", fmt.kv([
            ("активних підписок", subs.filter(is_active=True).count()),
            ("усього", subs.count()),
            ("проблемних джерел", bad or "—"),
        ])) + "\n(деталі: sources_list task=" + t.slug + ")")

    runs = t.runs.order_by("-created_at")[:5]
    if runs:
        parts.append(fmt.section("Останні збори", fmt.table(
            ["run", "статус", "період", "постів", "подій", "створено"],
            [[f"#{r.id}", r.status, f"{r.date_from}…{r.date_to}", r.posts_collected,
              r.events_total, fmt.ago(r.created_at)] for r in runs])))
    return fmt.joinsec(*parts)


@tool("runs_list", group="monitoring", params={
      "task": 'Задача: числовий id, slug або частина назви. Неоднозначність або чужа задача — відповість «не знайдено».',
      "status": 'Статус збору: pending | collecting | collected | awaiting_agent | done | failed | cancelled. Порожньо = усі.',
      "limit": "Скільки зборів показати."})
def runs_list(task: str = "", status: str = "", limit: int = 15):
    """Збори (ResearchRun): статус, період, прогрес чанків."""
    qs = common.scope_by_task(
        ResearchRun.objects.select_related("task")).order_by("-created_at")
    if task:
        qs = qs.filter(task=common.resolve_task(task))
    if status:
        qs = qs.filter(status=status)
    rows = []
    for r in qs[:limit]:
        ch = dict(r.chunks.order_by().values_list("status").annotate(n=Count("id")))
        total = sum(ch.values())
        done = ch.get("done", 0) + ch.get("split", 0)
        rows.append([f"#{r.id}", r.task.slug, r.status, f"{r.date_from}…{r.date_to}",
                     f"{done}/{total} {fmt.pct(done, total)}" if total else "—",
                     ch.get("failed", 0) or "", r.posts_collected, r.events_total,
                     fmt.ago(r.created_at)])
    if not rows:
        return "зборів за цим фільтром немає"
    return fmt.table(["run", "задача", "статус", "період", "чанки", "збій",
                      "постів", "подій", "створено"], rows)


@tool("run_show", group="monitoring", params={
      "run_id": "Числовий id збору зі списку runs_list."})
def run_show(run_id: int):
    """Збір детально: чанки, рух постів по стадіях, події періоду (як екран «Збори → Статус»)."""
    r = (common.scope_by_task(ResearchRun.objects.select_related("task"))
         .filter(pk=common.as_int(run_id, "run_id")).first())
    if not r:
        raise ToolError(f"збору #{run_id} немає")
    now = timezone.now()
    ch = dict(r.chunks.order_by().values_list("status").annotate(n=Count("id")))
    total = sum(ch.values())
    done = ch.get("done", 0) + ch.get("split", 0)
    cooldown = r.chunks.filter(status="pending", next_retry_at__gt=now).count()
    parts = [fmt.section(f"Збір #{r.id} — {r.task.slug}", fmt.kv([
        ("статус", r.get_status_display()),
        ("період", f"{r.date_from} … {r.date_to} (чанк {r.chunk_days} дн)"),
        ("створено", fmt.ago(r.created_at)),
        ("почато", fmt.ago(r.started_at)),
        ("завершено", fmt.ago(r.finished_at)),
        ("чанки", (", ".join(f"{k}: {v}" for k, v in sorted(ch.items())) or "немає")
                  + (f" · у бекофі {cooldown}" if cooldown else "")
                  + (f" · готово {fmt.pct(done, total)}" if total else "")),
        ("лічильники", f"постів {r.posts_collected}, релевантних {r.posts_relevant}, "
                       f"подій {r.events_total}"),
        ("помилка", fmt.trunc(r.error, 300)),
    ]))]

    window = Post.objects.filter(task=r.task, posted_at__date__gte=r.date_from,
                                 posted_at__date__lte=r.date_to)
    counts = dict(window.order_by().values_list("stage").annotate(n=Count("id")))
    order = [s for s, _ in Post.STAGE_CHOICES]
    flow = [(s, counts[s]) for s in order if counts.get(s)]
    parts.append(fmt.section("Пости періоду по стадіях",
                             " → ".join(f"{s}:{n}" for s, n in flow) or "постів немає"))

    failed = r.chunks.filter(status="failed").order_by("date_from")[:5]
    if failed:
        parts.append(fmt.section("Чанки зі збоєм", fmt.table(
            ["період", "спроб", "помилка"],
            [[f"{c.date_from}…{c.date_to}", c.attempts, fmt.trunc(c.error, 140)]
             for c in failed])))

    ev = Event.objects.filter(task=r.task, event_date__gte=r.date_from,
                              event_date__lte=r.date_to)
    rv = dict(ev.order_by().values_list("review_status").annotate(n=Count("id")))
    parts.append(fmt.section("Події періоду",
                             f"усього {ev.count()}; "
                             + (", ".join(f"{k}: {v}" for k, v in sorted(rv.items())) or "—")))
    if r.status == "awaiting_agent":
        parts.append("⚠ Чекає АГЕНТА: батчі у backend/_dir/runs/run_%d/ — протегуй їх "
                     "(SYSTEM_PROMPT.md у теці), ранер сам підхопить *_done.json." % r.id)
    return fmt.joinsec(*parts)


@tool("run_create", group="monitoring", mutates=True, params={
      "task": 'Задача: числовий id, slug або частина назви. Неоднозначність або чужа задача — відповість «не знайдено».',
      "date_from": "Перший день періоду, формат YYYY-MM-DD (напр. 2026-09-01).",
      "date_to": "Останній день періоду, YYYY-MM-DD, ВКЛЮЧНО. Межа — 400 днів на один збір.",
      "chunk_days": "Скільки днів в одному чанку TeleZip. 0 = взяти з налаштувань задачі. ПРЯМИЙ МНОЖНИК ЦІНИ: 1 запит (~$0.10) на чанк, тож 30 днів по дню = $3.00, по 3 дні = $1.00 — але більший чанк ближчий до відлупу.",
      "title": "Необовʼязкова назва збору для списку."})
def run_create(task: str, date_from: str, date_to: str, chunk_days: int = 0,
               title: str = ""):
    """Збір TeleZip за період для конвеєрів events, monitor, research. Не для infospace і tgsearch.

    Створює ResearchRun і планує чанки (як «Збори → Додати»). Далі все ведуть
    воркери. Ідемпотентно за діапазонами: вже покриті чанки не дублюються.
    """
    from analysis.services import stages as _stages
    t = common.resolve_task(task)
    if t.pipeline not in _TELEZIP_RUNS:
        raise ToolError(
            f"{t.slug} — конвеєр {t.pipeline}, run_create збирає TeleZip і його не чіпає. "
            + _PIPELINE_HOW.get(t.pipeline, ""))
    d_from = common.parse_date(date_from, "date_from")
    d_to = common.parse_date(date_to, "date_to")
    if d_to < d_from:
        raise ToolError("date_to раніше за date_from")
    if (d_to - d_from).days > 400:
        raise ToolError("період >400 днів: збирай меншими шматками (TeleZip таймаутить)")
    run = ResearchRun.objects.create(
        task=t, title=title or "", date_from=d_from, date_to=d_to,
        chunk_days=chunk_days or t.collect_chunk_days or 3, status="pending")
    made = _stages.enqueue_collection(t, d_from, d_to, chunk_days=run.chunk_days, job=run)
    run.status, run.started_at = "collecting", timezone.now()
    run.save(update_fields=["status", "started_at"])
    # ціну показуємо ДО того, як воркери почнуть платити: 1 запит TeleZip на
    # чанк, а важкий чанк ще й ділиться навпіл (див. find_posts_range)
    from analysis.services.mcp_api.telezip import REQUEST_COST_USD
    return (f"Збір #{run.id} для {t.slug}: {d_from}…{d_to}, заплановано {made} чанків "
            f"(по {run.chunk_days} дн). Воркери підхоплять самі — прогрес: run_show "
            f"run_id={run.id}.\n"
            f"Ціна: ~${made * REQUEST_COST_USD:.2f} (1 запит TeleZip на чанк; важкий "
            f"чанк ділиться навпіл — тоді більше). Більший chunk_days = дешевше, "
            f"але вищий ризик відлупу."
            + ("\n⚠ 0 нових чанків: період уже покрито попередніми зборами." if not made else ""))


@tool("run_cancel", group="monitoring", mutates=True, params={
      "run_id": "Числовий id збору.",
      "drop_pending_chunks": "true — прибрати ще не взяті чанки з черги. Уже зібрані пости лишаються в конвеєрі в будь-якому разі."})
def run_cancel(run_id: int, drop_pending_chunks: bool = True):
    """Скасувати збір: статус `cancelled` + (опційно) прибрати його ще не взяті чанки."""
    r = common.scope_by_task(ResearchRun.objects).filter(
        pk=common.as_int(run_id, "run_id")).first()
    if not r:
        raise ToolError(f"збору #{run_id} немає")
    dropped = 0
    if drop_pending_chunks:
        dropped, _ = r.chunks.filter(status="pending").delete()
    r.status, r.finished_at = "cancelled", timezone.now()
    r.save(update_fields=["status", "finished_at"])
    return (f"Збір #{r.id} ({r.task.slug}) скасовано; прибрано {dropped} чанків у черзі. "
            "Уже зібрані пости лишились у конвеєрі.")


@tool("chats_list", group="monitoring", params={
      "task": 'Задача: числовий id, slug або частина назви. Неоднозначність або чужа задача — відповість «не знайдено».',
      "active": "true — лише активні рядки whitelist, false — лише вимкнені, не передавати — усі.",
      "stream_only": "true — лише чати в режимі стріму (полінг історії).",
      "problems_only": "true — лише проблемні: без акаунта збору або стрім не оновлювався понад добу.",
      "limit": "Скільки рядків показати."})
def chats_list(task: str = "", active: bool = None, stream_only: bool = False,
               problems_only: bool = False, limit: int = 60):
    """Whitelist чатів моніторингу: акаунт збору, режим (стрім/пошук), свіжість.

    problems_only — без акаунта, або стрім не оновлювався понад добу.
    """
    qs = common.scope_by_task(
        MonitorChat.objects.select_related("task", "channel", "tg_account")
    ).order_by("task__slug", "priority", "channel__username")
    if task:
        qs = qs.filter(task=common.resolve_task(task))
    if active is not None:
        qs = qs.filter(is_active=active)
    if stream_only:
        qs = qs.filter(stream_enabled=True)
    if problems_only:
        stale = timezone.now() - timedelta(days=1)
        qs = qs.filter(is_active=True).filter(
            Q(tg_account__isnull=True)
            | Q(stream_enabled=True, last_streamed_at__lt=stale)
            | Q(stream_enabled=True, last_streamed_at__isnull=True))
    rows = []
    for c in qs[:limit]:
        acc = f"#{c.tg_account_id} {fmt.trunc(c.tg_account.name, 14)}" if c.tg_account_id \
            else "НЕМА"
        rows.append([f"#{c.id}", c.task.slug, fmt.flag(c.is_active),
                     f"@{c.channel.username}" if c.channel.username
                     else fmt.trunc(c.channel.title, 22),
                     "стрім" if c.stream_enabled else "пошук", acc,
                     fmt.ago(c.last_streamed_at), fmt.ago(c.last_searched_at),
                     c.stream_last_msg_id or "", c.priority])
    if not rows:
        return "чатів за цим фільтром немає"
    return (fmt.table(["id", "задача", "акт", "чат", "режим", "акаунт", "ост. стрім",
                       "ост. пошук", "watermark", "пріор"], rows)
            + f"\n\nпоказано {len(rows)} із {qs.count()}")


@tool("chat_update", group="monitoring", mutates=True, params={
      "chat": "Рядок whitelist: числовий id із chats_list або @username чату.",
      "is_active": "Увімкнути/вимкнути чат для наступних зборів (історія лишається).",
      "stream_enabled": "true — читати чат суцільно (стрім), false — шукати за словами.",
      "account": "Telegram-акаунт збору: id/номер/назва, або '-' щоб відвʼязати.",
      "priority": "Менше число = вище в списку.",
      "forward_media": "Пересилати медіа цього чату в чат медіа задачі.",
      "is_critical_source": "Особливо важливий чат (пріоритет у звітах). Не передавати = не змінювати.",
      "notes": "Нотатка. Порожньо = не змінювати; '-' = очистити."})
def chat_update(chat: str, is_active: bool = None, stream_enabled: bool = None,
                account: str = "", priority: int = None, forward_media: bool = None,
                is_critical_source: bool = None, notes: str = ""):
    """Змінити рядок whitelist: активність, режим стріму, акаунт збору, пріоритет.

    chat — id рядка або @username (якщо однозначний). account — id/назва акаунта
    або `-` щоб відв'язати.
    """
    c = common.resolve_chat(chat)
    changed = []
    if is_active is not None:
        c.is_active = bool(is_active)
        changed.append(f"активний={fmt.flag(c.is_active)}")
    if stream_enabled is not None:
        c.stream_enabled = bool(stream_enabled)
        changed.append(f"стрім={fmt.flag(c.stream_enabled)}")
    if forward_media is not None:
        c.forward_media = bool(forward_media)
        changed.append(f"пересилання медіа={fmt.flag(c.forward_media)}")
    if is_critical_source is not None:
        c.is_critical_source = bool(is_critical_source)
        changed.append(f"критичне джерело={fmt.flag(c.is_critical_source)}")
    if notes:
        c.notes = "" if notes.strip() == "-" else notes
        changed.append("нотатка " + ("очищена" if not c.notes else f"({len(c.notes)} симв)"))
    if priority is not None:
        c.priority = int(priority)
        changed.append(f"пріоритет={c.priority}")
    if account:
        if account.strip() in ("-", "none", "нема"):
            c.tg_account = None
            changed.append("акаунт відв'язано")
        else:
            a = common.resolve_account(account)
            c.tg_account = a
            changed.append(f"акаунт=#{a.id} {a.name}")
    if not changed:
        return f"#{c.id} {c.task.slug}/@{c.channel.username}: нічого не змінено"
    c.save()
    return f"#{c.id} {c.task.slug}/@{c.channel.username}: " + "; ".join(changed)


@tool("chat_delete", group="monitoring", mutates=True, params={
      "chat": "Рядок whitelist: числовий id із chats_list або @username чату.",
      "confirm": "true — прибрати чат із задачі. Історія постів лишається. Без цього видалення не відбудеться."})
def chat_delete(chat: str, confirm: bool = False):
    """Прибрати чат із whitelist задачі. Пости, уже зібрані з нього, не чіпає."""
    c = common.resolve_chat(chat)
    user = f"@{c.channel.username}" if c.channel.username else f"#{c.channel_id}"
    label = f"#{c.id} {c.task.slug}/{user}"
    if not confirm:
        raise ToolError(f"{label} не прибрано. Повтори з confirm=true.")
    c.delete()
    return f"{label}: прибрано з whitelist"


def _whitelist_channel(ref: str):
    """Рядок довідника для whitelist. @username / посилання, якого ще нема, створюється."""
    ref = (ref or "").strip()
    if not ref:
        raise ToolError("дай channel: @username, посилання або id довідника")
    looks_new = ref.startswith("@") or "://" in ref or "t.me/" in ref
    if looks_new:
        try:
            return common.resolve_channel(ref)
        except ToolError:
            try:
                ch, _created = Channel.ensure(ref)
            except ValueError as e:
                raise ToolError(str(e))
            return ch
    return common.resolve_channel(ref)


@tool("chat_add", group="monitoring", mutates=True, scope=SCOPE_CREATE, params={
      "task": "Задача monitor, research або tgsearch: id, slug або частина назви.",
      "channel": "@username, посилання t.me або id рядка довідника. Невідомий @username створюється в довіднику.",
      "is_active": "Одразу збирати (дефолт true).",
      "stream_enabled": "true — стрім (полінг + регулярка задачі), false — пошук за словами.",
      "forward_media": "Пересилати медіа цього чату в чат медіа задачі.",
      "account": "Telegram-акаунт збору: id/номер/назва. Порожньо = не призначати.",
      "priority": "Менше число = вище в списку. Дефолт 100."})
def chat_add(task: str, channel: str, is_active: bool = True, stream_enabled: bool = False,
             forward_media: bool = False, account: str = "", priority: int = 100):
    """Додати чат у whitelist задачі (те, що в адмінці — вкладка «Чати»).

    Лише конвеєри monitor, research, tgsearch. Повтор того самого чату не дублює.
    Для infospace джерело підключається через source_add, не через чат.
    """
    t = common.resolve_task(task)
    if t.pipeline not in _CHAT_PIPES:
        raise ToolError(
            f"{t.slug} — конвеєр {t.pipeline}, whitelist чатів лише для "
            + ", ".join(sorted(_CHAT_PIPES)) + ". " + _PIPELINE_HOW.get(t.pipeline, ""))
    ch = _whitelist_channel(channel)
    acc = common.resolve_account(account) if account else None
    row, created = MonitorChat.objects.get_or_create(
        task=t, channel=ch,
        defaults={"is_active": bool(is_active), "stream_enabled": bool(stream_enabled),
                  "forward_media": bool(forward_media), "priority": int(priority)})
    if acc is not None and created:
        row.tg_account = acc
        row.save(update_fields=["tg_account"])
    user = f"@{ch.username}" if ch.username else ch.title or f"#{ch.id}"
    if not created:
        return (f"#{row.id} {t.slug}/{user}: уже в whitelist "
                "(правки: chat_update)")
    bits = [f"активний={fmt.flag(row.is_active)}",
            "стрім" if row.stream_enabled else "пошук"]
    if row.tg_account_id:
        bits.append(f"акаунт=#{row.tg_account_id}")
    return f"#{row.id} {t.slug}/{user}: додано, " + ", ".join(bits)


def _rubric_keywords(spec: str) -> list:
    import json
    spec = (spec or "").strip()
    if not spec:
        raise ToolError("keywords порожні: по одному регулярному виразу в рядок "
                        "(усі мають збігтися) або JSON-список")
    if spec.startswith("["):
        try:
            data = json.loads(spec)
        except json.JSONDecodeError as e:
            raise ToolError(f"keywords: битий JSON ({e})")
        if not isinstance(data, list) or not all(isinstance(x, str) and x.strip() for x in data):
            raise ToolError("keywords: JSON-список непорожніх рядків")
        return [x.strip() for x in data]
    lines = [ln.strip() for ln in spec.splitlines() if ln.strip()]
    if not lines:
        raise ToolError("keywords порожні")
    return lines


def _ensure_rubric_tag(category: str, name: str) -> tuple[str, str]:
    from analysis.models import Tag, TagCategory
    key = (category or "").strip()
    c = TagCategory.objects.filter(key=key).first()
    if not c:
        raise ToolError(f"категорії тегів «{key}» немає (спершу tag_category_create)")
    name = (name or "").strip()
    if not name:
        raise ToolError("tag_name порожній")
    if len(name) > 80:
        raise ToolError("tag_name довше за 80 символів")
    folded = name.casefold()
    for existing in Tag.objects.filter(category=c.key):
        if existing.name.casefold() == folded:
            return c.key, existing.name
    Tag.objects.create(category=c.key, name=name)
    return c.key, name


def _resolve_rubric(ref: str) -> ResearchRubric:
    ref = str(ref).strip().lstrip("#")
    if not ref.isdigit():
        raise ToolError("рубрика: числовий id із rubrics_list")
    row = common.scope_by_task(
        ResearchRubric.objects.select_related("task")).filter(pk=int(ref)).first()
    if not row:
        raise ToolError(f"рубрики #{ref} немає")
    return row


def _rubric_line(row: ResearchRubric) -> str:
    keys = " | ".join(row.keywords or []) or "—"
    return (f"#{row.id} {row.task.slug} {row.tag_category}:{row.tag_name} "
            f"активна={fmt.flag(row.is_active)} порядок={row.order}\n"
            f"ключі (усі мають збігтися): {keys}"
            + (f"\nдоповнення промпту: {row.extra_prompt}" if row.extra_prompt else ""))


@tool("rubrics_list", group="monitoring", params={
      "task": "Задача research: id, slug або частина назви."})
def rubrics_list(task: str):
    """Рубрики тематичного дослідження: тег події і ключові слова (усі мають збігтися)."""
    t = common.resolve_task(task)
    rows = list(t.rubrics.all())
    if not rows:
        return f"{t.slug}: рубрик немає (rubric_create)"
    return "\n\n".join(_rubric_line(r) for r in rows)


@tool("rubric_create", group="monitoring", mutates=True, scope=SCOPE_CREATE, params={
      "task": "Задача research: id, slug або частина назви.",
      "tag_category": "Ключ категорії тегів, яка вже існує (tag_category_create).",
      "tag_name": "Канонічний тег події цієї рубрики (до 80 символів). Якщо тега ще нема — створюється.",
      "keywords": "Регулярки: по одній в рядок (кандидат, якщо КОЖНА збіглась) або JSON-список рядків.",
      "extra_prompt": "Додаткові правила рубрики в промпт агента. Порожньо = без доповнення.",
      "is_active": "Одразу активна (дефолт true).",
      "order": "Порядок у списку. Менше — вище."})
def rubric_create(task: str, tag_category: str, tag_name: str, keywords: str,
                  extra_prompt: str = "", is_active: bool = True, order: int = 0):
    """Додати рубрику research-задачі: що шукаємо в потоці чатів.

    Лише конвеєр research. Категорія тегів має вже існувати — інакше фасет подій не реєструється.
    """
    t = common.resolve_task(task)
    if t.pipeline != AnalysisTask.PIPELINE_RESEARCH:
        raise ToolError(
            f"{t.slug} — конвеєр {t.pipeline}, рубрики лише для research. "
            + _PIPELINE_HOW.get(t.pipeline, ""))
    cat, tag = _ensure_rubric_tag(tag_category, tag_name)
    words = _rubric_keywords(keywords)
    row = ResearchRubric(task=t, tag_category=cat, tag_name=tag, keywords=words,
                         extra_prompt=extra_prompt or "", is_active=bool(is_active),
                         order=max(0, int(order)))
    row.save()
    return "Рубрику додано\n" + _rubric_line(row)


@tool("rubric_update", group="monitoring", mutates=True, params={
      "ref": "Числовий id рубрики з rubrics_list.",
      "tag_category": "Інша категорія тегів. Порожньо = не змінювати.",
      "tag_name": "Інший тег події. Порожньо = не змінювати.",
      "keywords": "Новий список регулярок (рядки або JSON). Порожньо = не змінювати.",
      "extra_prompt": "Доповнення промпту. Порожньо = не змінювати; '-' = очистити.",
      "is_active": "Увімкнути/вимкнути. Не передавати = не змінювати.",
      "order": "Порядок. Не передавати = не змінювати."})
def rubric_update(ref: str, tag_category: str = "", tag_name: str = "", keywords: str = "",
                  extra_prompt: str = "", is_active: bool = None, order: int = None):
    """Змінити рубрику research-задачі."""
    row = _resolve_rubric(ref)
    changed = []
    cat = tag_category or row.tag_category
    tag = tag_name or row.tag_name
    if tag_category or tag_name:
        cat, tag = _ensure_rubric_tag(cat, tag)
        if cat != row.tag_category:
            row.tag_category = cat
            changed.append(f"категорія={cat}")
        if tag != row.tag_name:
            row.tag_name = tag
            changed.append(f"тег={tag}")
    if keywords:
        row.keywords = _rubric_keywords(keywords)
        changed.append(f"ключів={len(row.keywords)}")
    if extra_prompt:
        row.extra_prompt = "" if extra_prompt.strip() == "-" else extra_prompt
        changed.append("доповнення промпту " + ("очищено" if not row.extra_prompt
                                                else f"({len(row.extra_prompt)} симв)"))
    if is_active is not None:
        row.is_active = bool(is_active)
        changed.append(f"активна={fmt.flag(row.is_active)}")
    if order is not None:
        row.order = max(0, int(order))
        changed.append(f"порядок={row.order}")
    if not changed:
        return f"#{row.id}: нічого не змінено"
    row.save()
    return _rubric_line(row) + "\n" + "; ".join(changed)


@tool("rubric_delete", group="monitoring", mutates=True, params={
      "ref": "Числовий id рубрики з rubrics_list.",
      "confirm": "true — справді видалити. Без цього видалення не відбудеться."})
def rubric_delete(ref: str, confirm: bool = False):
    """Видалити рубрику. Тег події в довіднику лишається."""
    row = _resolve_rubric(ref)
    if not confirm:
        raise ToolError(f"#{row.id} {row.tag_category}:{row.tag_name} не видалено. "
                        "Повтори з confirm=true.")
    label = f"#{row.id} {row.task.slug} {row.tag_category}:{row.tag_name}"
    row.delete()
    return f"{label}: видалено"


@tool("sources_list", group="monitoring", params={
      "task": 'Задача: числовий id, slug або частина назви. Неоднозначність або чужа задача — відповість «не знайдено».',
      "kind": "Тип джерела: telegram | rss | web | vk. Порожньо = усі.",
      "problems_only": "true — лише проблемні: збої, підозра на якість або прострочений полінг (рахується тільки серед тих, кого воркер реально бере).",
      "limit": "Скільки джерел показати."})
def sources_list(task: str = "", kind: str = "", problems_only: bool = False,
                 limit: int = 60):
    """Джерела інформпростору: розклад полінгу, health, якість, до яких задач підключені."""
    qs = (Source.objects.select_related("channel", "channel__region_subject", "tg_account")
          .annotate(n_subs=Count("subscriptions", filter=Q(subscriptions__is_active=True)))
          .order_by("kind", "channel__title"))
    # джерело саме по собі нічиє, тож видимість успадковується від підписок:
    # не-суперюзер бачить лише ті, що живлять ЙОГО задачі
    if not registry.actor().is_superuser:
        qs = qs.filter(id__in=common.scope_by_task(
            SourceSubscription.objects.filter(is_active=True)).values("source_id"))
    if task:
        t = common.resolve_task(task)
        qs = qs.filter(subscriptions__task=t, subscriptions__is_active=True).distinct()
    if kind:
        qs = qs.filter(kind=kind)
    if problems_only:
        # лише серед тих, кого info_collect реально бере (див. pollable_source_ids)
        qs = qs.filter(is_active=True, id__in=common.pollable_source_ids()).filter(
            Q(quality_ok=False) | Q(consecutive_failures__gte=1)
            | Q(next_poll_at__lt=timezone.now() - timedelta(minutes=30)))
    rows = []
    for s in qs[:limit]:
        due = (s.next_poll_at - timezone.now()).total_seconds() if s.next_poll_at else None
        rows.append([f"#{s.id}", s.kind, fmt.flag(s.is_active), fmt.trunc(s.name, 30),
                     fmt.trunc(s.url, 40),
                     s.region_subject.name if s.region_subject_id else "—",
                     f"{s.poll_interval_sec // 60}хв",
                     "зараз" if due is not None and due <= 0 else
                     (f"через {int(due // 60)}хв" if due is not None else "—"),
                     fmt.ago(s.last_ok_at), s.consecutive_failures or "",
                     "" if s.quality_ok else f"🟡 {fmt.trunc(s.quality_note, 28)}",
                     s.n_subs or "нічиє"])
    if not rows:
        return "джерел за цим фільтром немає"
    parts = [fmt.table(["id", "тип", "акт", "назва", "url", "регіон", "інтервал",
                        "полінг", "ост. успіх", "збоїв", "якість", "задач"], rows)]
    errs = [s for s in qs[:limit] if s.last_error and s.consecutive_failures]
    if errs:
        parts.append(fmt.section("Останні помилки", "\n".join(
            f"#{s.id} {fmt.trunc(s.name, 24)}: {fmt.trunc(s.last_error, 160)}"
            for s in errs[:5])))
    return fmt.joinsec(*parts)


@tool("source_update", group="monitoring", mutates=True, params={
      "ref": "Джерело: числовий id, частина URL або назви.",
      "is_active": "Увімкнути/вимкнути опитування джерела.",
      "poll_interval_sec": "Інтервал полінгу в секундах (нижня межа 60).",
      "poll_now": "true — поставити джерело в чергу негайно.",
      "reset_cursor": "true — забути, докуди вже прочитано, і перечитати заново. Може дати вал постів.",
      "account": "Telegram-акаунт для полінгу (для kind=telegram): id/номер/назва або '-'."})
def source_update(ref: str, is_active: bool = None, poll_interval_sec: int = None,
                  poll_now: bool = False, reset_cursor: bool = False,
                  account: str = ""):
    """Змінити джерело: активність, інтервал, «опитати зараз», скидання курсора (backfill)."""
    s = common.resolve_source(ref)
    if not registry.actor().is_superuser:
        mine = common.scope_by_task(
            SourceSubscription.objects.filter(is_active=True, source=s)).exists()
        if not mine:
            raise ToolError(f"джерело #{s.id} не підключене до жодної твоєї задачі — "
                            "правити його може лише власник або адмін")
    changed = []
    if is_active is not None:
        s.is_active = bool(is_active)
        changed.append(f"активне={fmt.flag(s.is_active)}")
    if poll_interval_sec is not None:
        s.poll_interval_sec = max(60, int(poll_interval_sec))
        changed.append(f"інтервал={s.poll_interval_sec}с")
    if poll_now:
        s.next_poll_at = timezone.now()
        s.locked_at = None
        changed.append("полінг «зараз»")
    if reset_cursor:
        s.poll_cursor = {}
        changed.append("курсор скинуто (перечитає заново — може дати вал постів)")
    if account:
        if account.strip() in ("-", "none", "нема"):
            s.tg_account = None
            changed.append("акаунт відв'язано")
        else:
            a = common.resolve_account(account)
            s.tg_account = a
            changed.append(f"акаунт=#{a.id} {a.name}")
    if not changed:
        return f"#{s.id} {s.name}: нічого не змінено"
    s.save()
    return f"#{s.id} {s.name}: " + "; ".join(changed)


@tool("events_stats", group="monitoring", params={
      "task": 'Задача: числовий id, slug або частина назви. Неоднозначність або чужа задача — відповість «не знайдено».',
      "days": "Скільки останніх діб узяти.",
      "group_by": "Розріз: day | week | month | region | task | tag:<ключ категорії> (напр. tag:importance). Ключі — tag_categories.",
      "region": "Назва субʼєкта РФ або її частина (напр. Дагестан).",
      "limit": "Скільки рядків показати.",
      "review_status": "Статус аудиту подій: approved (дефолт) | pending | rejected | all."})
def events_stats(task: str = "", days: int = 14, group_by: str = "day",
                 region: str = "", limit: int = 20, review_status: str = "approved"):
    """Зріз подій: по днях/тижнях/місяцях, регіонах, тегах або задачах.

    group_by: day|week|month|region|tag:<категорія>|task.
    Рахує через services/metrics.py — ті самі формули, що й графіки адмінки.
    """
    from analysis.services.metrics import EventSource
    qs = common.scope_by_task(Event.objects.all())
    if task:
        qs = qs.filter(task=common.resolve_task(task))
    if review_status and review_status != "all":
        qs = qs.filter(review_status=review_status)
    since = timezone.now().date() - timedelta(days=max(1, int(days)))
    qs = qs.filter(event_date__gte=since)
    if region:
        from analysis.models import Region
        r = Region.objects.filter(name__icontains=region).first()
        if not r:
            raise ToolError(f"регіону «{region}» немає в довіднику")
        qs = qs.filter(region_subject=r)

    head = f"події за {days} дн (з {since}" + (f", аудит: {review_status}" if review_status else "") + ")"
    if group_by == "task":
        rows = [[f"#{r['task_id']}", r["task__slug"], r["n"]] for r in
                qs.order_by().values("task_id", "task__slug")
                  .annotate(n=Count("id")).order_by("-n")[:limit]]
        return fmt.section(head, fmt.table(["id", "задача", "подій"], rows))
    if group_by == "region":
        rows = [[r["name"], r["count"], r["per_100k"], r["reach"]]
                for r in EventSource(qs).by_region()[:limit]]
        return fmt.section(head, fmt.table(["регіон", "подій", "на 100к", "охоплення"], rows))
    if group_by.startswith("tag"):
        category = group_by.split(":", 1)[1] if ":" in group_by else ""
        if not category:
            raise ToolError("вкажи категорію: group_by=tag:<ключ категорії> "
                            "(список: TagCategory в адмінці)")
        rows = [[r["name"], r["count"]] for r in EventSource(qs).by_tag(category, top=limit)]
        return fmt.section(f"{head}, теги «{category}»",
                           fmt.table(["тег", "подій"], rows) if rows else "нічого")
    gran = {"day": "day", "week": "week", "month": "month"}.get(group_by)
    if not gran:
        raise ToolError(f"невідомий group_by: {group_by} "
                        "(day|week|month|region|tag:<категорія>|task)")
    rows = [[str(r["bucket"])[:10], r["count"], r["posts"] or 0, r["reach"] or 0]
            for r in EventSource(qs, gran).timeseries()][-limit:]
    return fmt.section(head, fmt.table(["період", "подій", "постів", "охоплення"], rows))


# --------------------------------------------------------------------------- події
EVENT_FILTER_DOCS = {
    "task": "Задача (id/slug/назва — лише своя). Порожньо = усі свої.",
    "days": "Останні N днів за датою події (як «Свіжість» в адмінці). Ігнорується, якщо задано date_from/date_to.",
    "date_from": "Дата події від, YYYY-MM-DD (як фільтр «Період»).",
    "date_to": "Дата події до, YYYY-MM-DD.",
    "review_status": "approved (дефолт, як в адмінці) | pending | rejected | all.",
    "region": "Субʼєкт РФ (канонічна назва/аліас) — фільтр «Субʼєкт РФ».",
    "settlement": "Населений пункт (частина назви).",
    "tag": "Теги: `категорія:тег` або просто `тег`; кілька через кому = ВСІ мають бути (як фасети в адмінці). Категорії: tag_categories або task_show.",
    "query": "Текст в описі події (icontains).",
    "channel": "@username каналу/джерела, який писав про подію (фільтр «Канал/Джерело»).",
    "min_channels": "Мінімум унікальних каналів події (фільтр «Кількість каналів»). 0 = без обмеження.",
    "min_reach": "Мінімальне охоплення. 0 = без обмеження.",
}


def _event_filters(task="", days=0, date_from="", date_to="", review_status="approved",
                   region="", settlement="", tag="", query="", channel="",
                   min_channels=0, min_reach=0):
    """Спільний набір фільтрів для events_list/events_stats — дзеркало
    list_filter адмінки подій (Період, Свіжість, Дослідження, Статус аудиту,
    Субʼєкт РФ, фасети тегів, Канал/Джерело, Кількість каналів, Охоплення)."""
    qs = common.scope_by_task(Event.objects.all())
    desc = []
    if task:
        t = common.resolve_task(task)
        qs, desc = qs.filter(task=t), desc + [f"задача {t.slug}"]
    if review_status and review_status != "all":
        if review_status not in dict(Event.REVIEW_CHOICES):
            raise ToolError("review_status: approved | pending | rejected | all")
        qs, desc = qs.filter(review_status=review_status), desc + [f"аудит {review_status}"]
    if date_from or date_to:
        if date_from:
            qs = qs.filter(event_date__gte=common.parse_date(date_from, "date_from"))
        if date_to:
            qs = qs.filter(event_date__lte=common.parse_date(date_to, "date_to"))
        desc.append(f"період {date_from or '…'} … {date_to or '…'}")
    elif days:
        since = timezone.now().date() - timedelta(days=max(1, int(days)))
        qs, desc = qs.filter(event_date__gte=since), desc + [f"за {days} дн (з {since})"]
    if region:
        r = common.resolve_region(region)
        qs, desc = qs.filter(region_subject=r), desc + [f"регіон {r.name}"]
    if settlement:
        qs, desc = qs.filter(settlement__icontains=settlement), desc + [f"нас. пункт ~{settlement}"]
    for spec in _split_csv(tag):
        cat, _, name = spec.rpartition(":")
        tq = Q(tags__name__iexact=name.strip())
        if cat:
            tq &= Q(tags__category=cat.strip())
        qs = qs.filter(tq)
        desc.append(f"тег {spec}")
    if query:
        qs, desc = qs.filter(summary__icontains=query), desc + [f"текст ~{query}"]
    if channel:
        qs = qs.filter(posts__channel__username__iexact=channel.lstrip("@"))
        desc.append(f"канал @{channel.lstrip('@')}")
    if min_channels:
        qs, desc = qs.filter(channel_count__gte=int(min_channels)), desc + [f"каналів ≥{min_channels}"]
    if min_reach:
        qs, desc = qs.filter(reach__gte=int(min_reach)), desc + [f"охоплення ≥{min_reach}"]
    return qs.distinct(), desc


def _tags_short(ev, n=6):
    return ", ".join(f"{t.category}:{t.name}" for t in ev.tags.all()[:n])


@tool("events_list", group="monitoring", params={**EVENT_FILTER_DOCS,
      "order": "newest (дефолт) | oldest | reach | channels.",
      "limit": "Скільки подій показати."})
def events_list(task: str = "", days: int = 30, date_from: str = "", date_to: str = "",
                review_status: str = "approved", region: str = "", settlement: str = "",
                tag: str = "", query: str = "", channel: str = "", min_channels: int = 0,
                min_reach: int = 0, order: str = "newest", limit: int = 30):
    """Список подій із фільтрами адмінки: період/свіжість, задача, статус аудиту,
    регіон, теги (фасети), канал, кількість каналів, охоплення.

    Дефолт — як у списку адмінки: лише «Схвалено» за останні 30 днів;
    `review_status=pending` — черга на аудит (далі `event_update` схвалює/відхиляє).
    Категорії тегів для фільтра `tag` — `tag_categories`.
    Колонка id — подія (`event_show` / `event_update`). Колонка пост — id
    найранішого поста цієї події (`prompt_try` / `posts_retag`, параметр posts).
    """
    from django.db.models import OuterRef, Subquery
    qs, desc = _event_filters(task, days, date_from, date_to, review_status, region,
                              settlement, tag, query, channel, min_channels, min_reach)
    ordering = {"newest": ("-event_date", "-id"), "oldest": ("event_date", "id"),
                "reach": ("-reach", "-id"), "channels": ("-channel_count", "-id")}.get(order)
    if not ordering:
        raise ToolError("order: newest | oldest | reach | channels")
    head_post = (Post.objects.filter(event_id=OuterRef("pk")).exclude(text="")
                 .order_by("posted_at", "id").values("id")[:1])
    qs = qs.annotate(head_post_id=Subquery(head_post))
    total = qs.count()
    show_task = not task
    headers = ["id", "пост", "дата"] + (["задача"] if show_task else []) + [
        "аудит", "регіон", "нас. пункт", "кан.", "охопл.", "теги", "опис"]
    rows = []
    for e in (qs.select_related("task", "region_subject").prefetch_related("tags")
              .order_by(*ordering)[:limit]):
        row = [f"#{e.id}", f"#{e.head_post_id}" if e.head_post_id else "—",
               str(e.event_date or "—")]
        if show_task:
            row.append(e.task.slug)
        row += [
            {"approved": "✓", "pending": "?", "rejected": "✗"}.get(e.review_status, e.review_status),
            fmt.trunc(e.region_subject.name if e.region_subject_id else (e.region or "—"), 18),
            fmt.trunc(e.settlement, 14), e.channel_count, e.reach,
            fmt.trunc(_tags_short(e), 40), fmt.trunc(e.summary, 90)]
        rows.append(row)
    return fmt.joinsec(
        fmt.section(f"Події: {total} (показано {len(rows)})", "; ".join(desc) or "без фільтрів"),
        fmt.table(headers, rows) if rows else "нічого не знайдено",
        "Фільтри: days/date_from/date_to, review_status, region, settlement, tag (кат:тег, кома = І), "
        "query, channel, min_channels, min_reach; order=newest|oldest|reach|channels.")


@tool("event_show", group="monitoring", params={"ref": "id події (з events_list)."})
def event_show(ref: str):
    """Картка події: id події, опис, регіон, усі теги, аудит, пости-джерела.

    У таблиці постів колонка id — id поста. Його передають у `prompt_try` і
    `posts_retag` (параметр posts).
    """
    ev = _resolve_event(ref)
    posts = list(ev.posts.select_related("channel").order_by("posted_at", "id")[:15])
    n_posts = ev.posts.count()
    post_note = f"показано {len(posts)} з {n_posts}" if n_posts > len(posts) else ""
    return fmt.joinsec(
        fmt.section(f"Подія #{ev.id} · {ev.event_date} · {ev.task.slug}", fmt.kv([
            ("id", f"#{ev.id}"),
            ("аудит", f"{ev.review_status}" + (f" ({fmt.trunc(ev.review_notes, 80)})" if ev.review_notes else "")
                      + (f", {fmt.ago(ev.reviewed_at)}" if ev.reviewed_at else "")),
            ("регіон", (ev.region_subject.name if ev.region_subject_id else "—")
                       + (f" (сирий: {ev.region})" if ev.region else "")),
            ("нас. пункт", ev.settlement or "—"),
            ("теги", ", ".join(f"{t.category}:{t.name}" for t in ev.tags.all()) or "—"),
            ("постів/каналів/охоплення", f"{ev.post_count}/{ev.channel_count}/{ev.reach}"),
            ("опис", ev.summary or "—"),
            ("правити", f"/admin/analysis/event/{ev.id}/change/?task={ev.task_id}"),
        ])),
        fmt.section("Пости" + (f" ({post_note})" if post_note else ""), fmt.table(
            ["id", "коли", "канал", "посилання"],
            [[f"#{p.id}", str(p.posted_at)[:16],
              fmt.trunc(p.channel_name or (p.channel.title if p.channel_id else ""), 24),
              p.url] for p in posts])) if posts else "")


def _resolve_event(ref):
    if not str(ref).strip().lstrip("#").isdigit():
        raise ToolError("подія — за числовим id (з events_list)")
    ev = common.scope_by_task(Event.objects.select_related("task", "region_subject")) \
        .filter(pk=common.as_int(ref, "ref")).first()
    if not ev:
        raise ToolError(f"події #{ref} немає")
    return ev


@tool("event_update", group="monitoring", mutates=True, params={
      "ref": "id події (з events_list).",
      "review": "approve — схвалити; reject — відхилити (буде видалено чисткою); pending — повернути в чергу аудиту. Порожньо = не чіпати.",
      "notes": "Нотатка аудиту (чому). Пишеться разом із review або окремо.",
      "add_tags": "Теги додати: `категорія:тег` через кому (напр. `topic:мігранти, attacker_nationality:узбек`). Категорія обовʼязкова; у закритій категорії тег має бути зі словника, у відкритій — канонізується (можливий виклик LLM) або створюється.",
      "remove_tags": "Теги прибрати: `категорія:тег` або `тег` через кому.",
      "region": "Субʼєкт РФ (канонічна назва/аліас). Порожньо = не змінювати; '-' = очистити.",
      "settlement": "Населений пункт. Порожньо = не змінювати; '-' = очистити.",
      "event_date": "Дата події YYYY-MM-DD. Порожньо = не змінювати.",
      "summary": "Новий опис події. Порожньо = не змінювати."})
def event_update(ref: str, review: str = "", notes: str = "", add_tags: str = "",
                 remove_tags: str = "", region: str = "", settlement: str = "",
                 event_date: str = "", summary: str = ""):
    """Схвалити/відхилити/повернути в чергу, тегувати, поправити регіон/дату/опис події.

    Схвалення/відхилення — те саме, що дії «✅ Схвалити» / «🚫 Відхилити» в
    адмінці (нотатка `manual: … by <user>`, claim-lock аудиту знімається).
    Відхилена подія зникає з дефолтних списків і графіків і згодом видаляється
    чисткою — це не «м'яке приховування».
    """
    from analysis.models import Tag
    from analysis.services import tags as tag_svc
    ev = _resolve_event(ref)
    who = registry.actor()
    changed, fields = [], []
    if review:
        status = {"approve": Event.REVIEW_APPROVED, "approved": Event.REVIEW_APPROVED,
                  "reject": Event.REVIEW_REJECTED, "rejected": Event.REVIEW_REJECTED,
                  "pending": Event.REVIEW_PENDING}.get(review.strip().lower())
        if not status:
            raise ToolError("review: approve | reject | pending")
        ev.review_status = status
        ev.review_notes = notes or f"manual: {status} by {who}"
        ev.reviewed_at = timezone.now() if status != Event.REVIEW_PENDING else None
        ev.review_locked_at = None
        fields += ["review_status", "review_notes", "reviewed_at", "review_locked_at"]
        changed.append({"approved": "✅ схвалено", "rejected": "🚫 відхилено",
                        "pending": "↩ у чергу аудиту"}[status])
    elif notes:
        ev.review_notes, fields = notes, fields + ["review_notes"]
        changed.append("нотатка аудиту оновлена")
    if region:
        ev.region_subject = None if region.strip() == "-" else common.resolve_region(region)
        fields.append("region_subject")
        changed.append(f"регіон={ev.region_subject.name if ev.region_subject_id else '—'}")
    if settlement:
        ev.settlement = "" if settlement.strip() == "-" else settlement.strip()[:160]
        fields.append("settlement")
        changed.append(f"нас. пункт={ev.settlement or '—'}")
    if event_date:
        ev.event_date, fields = common.parse_date(event_date, "event_date"), fields + ["event_date"]
        changed.append(f"дата={ev.event_date}")
    if summary:
        ev.summary, fields = summary.strip(), fields + ["summary"]
        changed.append(f"опис ({len(ev.summary)} симв)")
    added, dropped, skipped = [], [], []
    for spec in _split_csv(add_tags):
        cat, _, name = spec.rpartition(":")
        if not cat or not name.strip():
            raise ToolError(f"add_tags: «{spec}» — потрібно `категорія:тег`")
        cat, name = cat.strip(), name.strip()
        t = Tag.objects.filter(category=cat, name__iexact=name).first() \
            or tag_svc.resolve(cat, name)
        if t is None:
            skipped.append(spec)
            continue
        if not ev.tags.filter(pk=t.pk).exists():
            ev.tags.add(t)
            added.append(f"{t.category}:{t.name}")
    for spec in _split_csv(remove_tags):
        cat, _, name = spec.rpartition(":")
        qs = ev.tags.filter(name__iexact=name.strip())
        if cat:
            qs = qs.filter(category=cat.strip())
        for t in list(qs):
            ev.tags.remove(t)
            dropped.append(f"{t.category}:{t.name}")
    if added:
        changed.append("теги +" + ", ".join(added))
    if dropped:
        changed.append("теги −" + ", ".join(dropped))
    if skipped:
        changed.append("НЕ додано (закрита категорія, немає у словнику або категорії не існує): "
                       + ", ".join(skipped))
    if not fields and not added and not dropped and not skipped:
        return f"подія #{ev.id}: нічого не змінено (жоден параметр не передано)"
    if fields:
        ev.save(update_fields=fields)
    return fmt.section(f"Подія #{ev.id} · {ev.event_date} · {ev.task.slug}",
                       "\n".join(changed) + f"\nтеги тепер: {_tags_short(ev, 20) or '—'}")


@tool("tag_categories", group="monitoring", params={
      "task": "Задача — показати лише її категорії (порожньо = усі) і по 12 найчастіших тегів кожної."})
def tag_categories(task: str = ""):
    """Категорії тегів (`TagCategory`) і приклади тегів — щоб правильно писати
    `tag=` у events_list і `add_tags=` в event_update. Закрита категорія =
    лише словникові теги."""
    from analysis.models import Tag, TagCategory
    cats = TagCategory.objects.order_by("order", "key")
    t = common.resolve_task(task) if task else None
    if t is not None and t.tag_categories.exists():
        cats = cats.filter(pk__in=t.tag_categories.values("pk"))
    rows = []
    for c in cats:
        top = (Tag.objects.filter(category=c.key).order_by()
               .annotate(n=Count("events", filter=Q(events__task=t) if t else None))
               .order_by("-n", "name")[:12])
        rows.append([c.key, fmt.trunc(c.label, 22), "закрита" if c.closed else "відкрита",
                     fmt.trunc(", ".join(f"{x.name}({x.n})" if x.n else x.name for x in top), 90)])
    return fmt.table(["ключ", "назва", "тип", "теги (події)"], rows) if rows else "категорій немає"


@tool("channels_find", group="monitoring", params={
      "query": "@username або частина назви каналу в НАШОМУ довіднику. Пошук у базі TeleZip — це tz_channels.",
      "limit": "Скільки каналів показати."})
def channels_find(query: str, limit: int = 20):
    """Знайти канал/чат у нашому довіднику (`Channel`) за username/назвою.

    Колонка «id» — це id рядка довідника, і він НЕ підходить ні для
    `chat_update` (там id рядка whitelist із `chats_list`), ні для
    `tz_find(channel=…)` (там TelegramID або @username). Із цієї таблиці для
    інших інструментів бери @username, а рядок whitelist шукай у `chats_list`.
    """
    qs = (Channel.objects.filter(Q(username__icontains=query.lstrip("@"))
                                 | Q(title__icontains=query))
          .select_related("region_subject").order_by("-subscribers")[:limit])
    rows = [[f"#{c.id}", f"@{c.username}" if c.username else "—", fmt.trunc(c.title, 36),
             c.subscribers or "", c.region_subject.name if c.region_subject_id else "—",
             c.enrolled_in.count()] for c in qs]
    return fmt.table(["id", "username", "назва", "підписників", "регіон", "у моніторингах"],
                     rows) if rows else f"каналів за «{query}» немає"


@tool("channel_add", group="monitoring", mutates=True, scope=SCOPE_CREATE, params={
      "url": "Посилання або @username: https://t.me/name, @name, https://vk.com/club…, https://site/розділ. Ключ довідника — нормалізоване посилання, дубль не створиться.",
      "title": "Назва (заповнює лише порожню).",
      "region": "Субʼєкт РФ канонічною назвою або аліасом із довідника регіонів (заповнює лише порожній).",
      "topics": "Теми/теги через кому (новини, етнічне, політика, локал-чат…). Додаються до наявних.",
      "chat_type": "channel | chat | discussion | unknown. Порожньо = за платформою.",
      "language": "Код мови (ru, uk…)."})
def channel_add(url: str, title: str = "", region: str = "", topics: str = "",
                chat_type: str = "", language: str = ""):
    """Додати канал/чат/сайт у довідник (`Channel`) або дозаповнити наявний.

    Довідник спільний: рядок ідентифікує посилання, тож повторний виклик не
    дублює, а віддає той самий рядок і дописує порожні поля й теми. Щоб канал
    ще й ОПИТУВАВСЯ як джерело infospace: `source_add`. У whitelist чатів
    monitor/research/tgsearch — `chat_add`.
    """
    reg = common.resolve_region(region) if region else None
    try:
        ch, created = Channel.ensure(url, name=title, region=reg, language=language)
    except ValueError as e:
        raise ToolError(str(e))
    changed = []
    if chat_type:
        if chat_type not in dict(Channel.CHAT_TYPE_CHOICES):
            raise ToolError(f"chat_type: очікується одне з "
                            f"{', '.join(dict(Channel.CHAT_TYPE_CHOICES))}")
        ch.chat_type, changed = chat_type, changed + ["chat_type"]
    added = _merge_topics(ch, topics, "")
    if added:
        changed.append("topics")
    if changed:
        ch.save(update_fields=changed)
    return fmt.section(
        f"Довідник: #{ch.id} {'створено' if created else 'уже був'}",
        _channel_card(ch) + (f"\nдодано теми: {', '.join(added)}" if added else ""))


@tool("channel_update", group="monitoring", mutates=True, params={
      "ref": "Канал довідника: id рядка (#123 із channels_find), @username, посилання або частина назви (має бути однозначною).",
      "add_topics": "Теми/теги додати (через кому).",
      "remove_topics": "Теми/теги прибрати (через кому).",
      "title": "Нова назва. Порожньо = не змінювати.",
      "region": "Субʼєкт РФ (канонічна назва/аліас). Порожньо = не змінювати; '-' = очистити.",
      "settlement": "Населений пункт. Порожньо = не змінювати; '-' = очистити.",
      "chat_type": "channel | chat | discussion | unknown. Порожньо = не змінювати.",
      "focus": "Фокус каналу одним реченням. Порожньо = не змінювати; '-' = очистити.",
      "discusses_problems": "Чи обговорює суспільні проблеми РФ. Не передавати = не змінювати."})
def channel_update(ref: str, add_topics: str = "", remove_topics: str = "", title: str = "",
                   region: str = "", settlement: str = "", chat_type: str = "",
                   focus: str = "", discusses_problems: bool = None):
    """Змінити рядок довідника: теми (теги), назву, регіон, населений пункт, тип, фокус.

    Теми — це поле `Channel.topics` (список), те саме, що править агент
    класифікації директорії; підписники/активність/доступ пише лише код.
    """
    ch = common.resolve_channel(ref)
    changed, notes = [], []
    if title:
        ch.title, changed = title.strip()[:512], changed + ["title"]
        notes.append(f"назва={ch.title}")
    if region:
        ch.region_subject = None if region.strip() == "-" else common.resolve_region(region)
        changed.append("region_subject")
        notes.append(f"регіон={ch.region_subject.name if ch.region_subject_id else '—'}")
    if settlement:
        ch.settlement = "" if settlement.strip() == "-" else settlement.strip()[:160]
        changed.append("settlement")
        notes.append(f"нас. пункт={ch.settlement or '—'}")
    if chat_type:
        if chat_type not in dict(Channel.CHAT_TYPE_CHOICES):
            raise ToolError(f"chat_type: очікується одне з "
                            f"{', '.join(dict(Channel.CHAT_TYPE_CHOICES))}")
        ch.chat_type, changed = chat_type, changed + ["chat_type"]
        notes.append(f"тип={chat_type}")
    if focus:
        ch.directory_focus = "" if focus.strip() == "-" else focus.strip()[:300]
        changed.append("directory_focus")
        notes.append("фокус оновлено")
    if discusses_problems is not None:
        ch.discusses_problems = bool(discusses_problems)
        changed.append("discusses_problems")
        notes.append(f"обговорює проблеми={fmt.flag(ch.discusses_problems)}")
    added = _merge_topics(ch, add_topics, remove_topics)
    if add_topics or remove_topics:
        changed.append("topics")
        notes.append(f"теми={', '.join(ch.topics) or '—'}")
    if not changed:
        return f"#{ch.id} {ch}: нічого не змінено (жоден параметр не передано)"
    ch.save(update_fields=changed)
    return fmt.section(f"Довідник: #{ch.id} {ch}", "\n".join(notes) + "\n\n" + _channel_card(ch))


def _split_csv(spec: str) -> list[str]:
    return [x.strip() for x in (spec or "").split(",") if x.strip()]


def _merge_topics(ch, add: str, remove: str) -> list[str]:
    """Оновити `Channel.topics` на місці (без збереження) → список доданих."""
    topics = [t for t in (ch.topics or []) if isinstance(t, str)]
    low = {t.lower() for t in topics}
    added = []
    for t in _split_csv(add):
        if t.lower() not in low:
            topics.append(t)
            low.add(t.lower())
            added.append(t)
    drop = {t.lower() for t in _split_csv(remove)}
    ch.topics = [t for t in topics if t.lower() not in drop]
    return added


def _channel_card(ch) -> str:
    return fmt.kv([
        ("посилання", ch.url or "—"),
        ("username", f"@{ch.username}" if ch.username else "—"),
        ("назва", ch.title or "—"),
        ("платформа/тип", f"{ch.platform}/{ch.chat_type or '—'}"),
        ("регіон", ch.region_subject.name if ch.region_subject_id else "—"),
        ("нас. пункт", ch.settlement or "—"),
        ("теми", ", ".join(ch.topics or []) or "—"),
        ("підписників", ch.subscribers or "—"),
        ("джерело", f"#{ch.source.id}" if hasattr(ch, "source") else "нема (source_add)"),
    ])


@tool("event_add", group="monitoring", mutates=True, params={
      "task": "Задача (дослідження): id, slug або частина назви — лише своя.",
      "url": "Посилання на пост Telegram (https://t.me/name/123, відкритий канал) або статтю на сайті."})
def event_add(task: str, url: str):
    """Додати подію за посиланням — як «Додати подію» в списку подій адмінки.

    Текст поста/статті → скрін-промпт дослідження (relevant/summary/region/tags)
    → Post(done) → Event(approved). Це виклик LLM і fetch сторінки; закритий
    канал або нерелевантний текст → зрозуміла помилка. Якщо пост із таким URL
    у дослідженні вже має подію — повернеться вона, дубля не буде.
    """
    from analysis.services.event_by_link import LinkError, create_event
    t = common.resolve_task(task)
    url = (url or "").strip()
    if not url:
        raise ToolError("дай посилання на пост або статтю")
    try:
        ev, created = create_event(t, url, registry.actor().user)
    except LinkError as e:
        raise ToolError(str(e))
    except Exception as e:  # noqa: BLE001 — модель має бачити причину
        raise ToolError(f"не вдалося створити подію: {type(e).__name__}: {e}")
    tags = ", ".join(f"{x.category}:{x.name}" for x in ev.tags.all()[:8])
    return fmt.section(
        f"Подія #{ev.id} {'створена' if created else 'уже була — повернуто наявну'}",
        fmt.kv([("задача", t.slug), ("дата", ev.event_date),
                ("регіон", ev.region_subject.name if ev.region_subject_id else ev.region or "—"),
                ("нас. пункт", ev.settlement or "—"), ("теги", tags or "—"),
                ("статус", ev.review_status),
                ("опис", fmt.trunc(ev.summary, 300)),
                ("правити", f"/admin/analysis/event/{ev.id}/change/?task={t.id}")]))


@tool("source_add", group="monitoring", mutates=True, scope=SCOPE_CREATE, params={
      "url": "Посилання: https://t.me/name або @name (Telegram), https://site/section або RSS-адреса (сайт/RSS), https://vk.com/… (VK).",
      "kind": "telegram | rss | web | vk. Порожньо = за посиланням (t.me → telegram, vk.com → vk, інакше web; '.xml'/'/rss'/'/feed' → rss).",
      "task": "Одразу підписати задачу (id/slug/назва — лише свою). Порожньо = джерело без підписок (воркер його не опитуватиме).",
      "name": "Назва (заповнює лише порожню в довіднику).",
      "region": "Субʼєкт РФ канонічною назвою/аліасом.",
      "language": "Код мови (ru, uk…).",
      "poll_interval_sec": "Інтервал полінгу в секундах. 0 = дефолт моделі.",
      "account": "Telegram-акаунт для читання (id/номер/назва) — лише для kind=telegram."})
def source_add(url: str, kind: str = "", task: str = "", name: str = "", region: str = "",
               language: str = "", poll_interval_sec: int = 0, account: str = ""):
    """Створити джерело інформпростору (`Source`) і, за потреби, підписати на нього задачу.

    Джерело = рядок довідника (`Channel`, створюється/знаходиться за посиланням)
    + розклад/курсор/акаунт. Без підписки активної infospace-задачі
    `info_collect` його НЕ бере в роботу — тож зазвичай передавай `task`.
    Повторний виклик з тим самим посиланням віддає наявне джерело.
    """
    url = (url or "").strip()
    if not url:
        raise ToolError("дай посилання")
    kind = (kind or _guess_kind(url)).strip().lower()
    if kind not in dict(Source.KIND_CHOICES):
        raise ToolError(f"kind: очікується одне з {', '.join(dict(Source.KIND_CHOICES))}")
    reg = common.resolve_region(region) if region else None
    t = common.resolve_task(task) if task else None
    if t is not None and t.pipeline != AnalysisTask.PIPELINE_INFOSPACE:
        raise ToolError(
            f"{t.slug} — конвеєр {t.pipeline}, джерела підключаються лише до infospace. "
            + _PIPELINE_HOW.get(t.pipeline, ""))
    acc = common.resolve_account(account) if account else None
    if acc is not None and kind != Source.KIND_TELEGRAM:
        raise ToolError("акаунт має сенс лише для kind=telegram")
    extra = {}
    if poll_interval_sec:
        extra["poll_interval_sec"] = max(60, int(poll_interval_sec))
    if acc is not None:
        extra["tg_account"] = acc
    try:
        src, created = Source.ensure(kind, url, name=name, region=reg, language=language,
                                     **extra)
    except ValueError as e:
        raise ToolError(str(e))
    notes = [f"джерело #{src.id} {'створено' if created else 'уже було'}: {src.name} ({src.url}), "
             f"тип {src.kind}, інтервал {src.poll_interval_sec} с"]
    if not created and extra:
        for k, v in extra.items():
            setattr(src, k, v)
        src.save(update_fields=list(extra))
        notes.append("оновлено: " + ", ".join(extra))
    if t is not None:
        sub, sub_created = SourceSubscription.objects.get_or_create(
            task=t, source=src, defaults={"is_active": True})
        if not sub_created and not sub.is_active:
            sub.is_active = True
            sub.save(update_fields=["is_active"])
            sub_created = True
        notes.append(f"підписка {t.slug}: {'додано' if sub_created else 'уже є'}")
    else:
        notes.append("підписок нема — воркер не опитуватиме; підписати: source_subscribe")
    return fmt.section("Джерело", "\n".join(notes))


def _guess_kind(url: str) -> str:
    u = url.strip().lower()
    if u.startswith("@") or "t.me/" in u:
        return Source.KIND_TELEGRAM
    if "vk.com/" in u:
        return Source.KIND_VK
    if u.endswith((".xml", ".rss", "/rss", "/feed", "/rss/", "/feed/")) or "rss" in u.rsplit("/", 1)[-1]:
        return Source.KIND_RSS
    return Source.KIND_WEB


@tool("source_subscribe", group="monitoring", mutates=True, params={
      "ref": "Джерело: id, посилання або частина назви.",
      "task": "Задача (id/slug/назва — лише своя).",
      "active": "true — підписати/увімкнути; false — вимкнути підписку (історія лишається).",
      "priority": "Пріоритет підписки (менше = вище). 0 = не змінювати."})
def source_subscribe(ref: str, task: str, active: bool = True, priority: int = 0):
    """Підписати infospace-задачу на джерело (або вимкнути підписку).

    Лише конвеєр infospace. Підписка робить джерело «робочим» для info_collect.
    Вимкнення не видаляє ні джерело, ні зібране.
    """
    src = common.resolve_source(ref)
    t = common.resolve_task(task)
    if t.pipeline != AnalysisTask.PIPELINE_INFOSPACE:
        raise ToolError(
            f"{t.slug} — конвеєр {t.pipeline}, підписка на джерело лише для infospace. "
            + _PIPELINE_HOW.get(t.pipeline, ""))
    sub, created = SourceSubscription.objects.get_or_create(
        task=t, source=src, defaults={"is_active": bool(active)})
    changed = []
    if not created and sub.is_active != bool(active):
        sub.is_active = bool(active)
        changed.append("is_active")
    if priority:
        sub.priority = int(priority)
        changed.append("priority")
    if changed:
        sub.save(update_fields=changed)
    state = "активна" if sub.is_active else "вимкнена"
    return (f"підписка {t.slug} → #{src.id} {src.name}: "
            f"{'створено' if created else ('оновлено' if changed else 'без змін')}, "
            f"{state}, пріоритет {sub.priority}")


@tool("task_create", group="monitoring", mutates=True, scope=SCOPE_CREATE, params={
      "slug": "Ідентифікатор латиницею (a-z, 0-9, дефіс), унікальний: `migrants-kavkaz-2026`.",
      "name": "Назва дослідження.",
      "pipeline": "events (події з TeleZip-збору) | infospace (полінг джерел → живі події) | monitor (критика в чатах) | research (тематичне) | tgsearch (пошук у чатах Telegram). Дефолт events.",
      "description": "Опис (для людей).",
      "telezip_query": "Запит TeleZip для збору (діалект v3: пробіл = АБО, І — `+`). Для infospace не потрібен.",
      "languages": "Мови через кому (ru, uk…).",
      "tag_categories": "Ключі категорій тегів через кому (див. tag_categories) — які теги класифікатор збирає з поста.",
      "display_name": "Людська назва для простого інтерфейсу (/app/).",
      "is_active": "Створити активною (дефолт true).",
      "info_screen_prompt": "Лише infospace: системний промпт скріну. Порожньо = дефолт моделі (копіюється в поле при збереженні форми; тут лишається порожнім, доки не задаси).",
      "info_tagger_prompt": "Лише infospace: додаткові правила тегів, доклеюються до скрін-промпта. Порожньо = лише підказки категорій.",
      "info_judge_prompt": "Лише infospace: промпт судді зіставлення. Порожньо = дефолт із коду.",
      **_EXTRA_PARAM_DOCS})
def task_create(slug: str, name: str, pipeline: str = "events", description: str = "",
                telezip_query: str = "", languages: str = "", tag_categories: str = "",
                display_name: str = "", is_active: bool = True,
                info_screen_prompt: str = "", info_tagger_prompt: str = "",
                info_judge_prompt: str = "",
                drop_linked_comments: bool = None,
                dedup_pre_thresh: int = None, dedup_cand_thresh: int = None,
                dedup_judge_prompt: str = "", generic_sides: str = "",
                review_model: str = "", review_prompt: str = "",
                agent_review_prompt: str = "",
                mon_min_len: int = None, mon_max_len: int = None,
                prescreen_model: str = "", prescreen_prompt: str = "",
                prescreen_enabled: bool = None, tagger_prompt: str = "",
                dedup_group_days: int = None, dedup_group_fuzz: int = None,
                dedup_llm_cluster: bool = None, dedup_cluster_prompt: str = "",
                research_audit_enabled: bool = None, research_audit_prompt: str = "",
                info_screen_model: str = "", info_max_age_days: int = None,
                info_match_window_hours: int = None, info_update_summaries: bool = None,
                info_retention_days: int = None,
                stream_regex: str = "", stream_interval_min: int = None,
                stream_media_chat_id: str = "",
                search_terms: str = "", search_days: int = None,
                search_limit_per_term: int = None):
    """Створити задачу (`AnalysisTask`) і одразу поля її етапів. Власник = ти; видно лише тобі і суперюзерам.

    pipeline обовʼязково розрізняй: events (події TeleZip), monitor (критика
    в чатах), research (тематичне), infospace (полінг джерел), tgsearch
    (пошук у чатах). Дефолт events — це не інформпростір. Відповідь каже,
    чим цей тип збирається і яке в нього поле промпта.
    """
    import re as _re
    from analysis.models import TagCategory
    slug = (slug or "").strip().lower()
    if not _re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", slug):
        raise ToolError("slug: латиниця, цифри й дефіс, 2–64 символи")
    if AnalysisTask.objects.filter(slug=slug).exists():
        raise ToolError(f"задача зі slug «{slug}» уже є (можливо, чужа)")
    if not (name or "").strip():
        raise ToolError("дай назву")
    if pipeline not in dict(AnalysisTask.PIPELINE_CHOICES):
        raise ToolError(f"pipeline: одне з {', '.join(dict(AnalysisTask.PIPELINE_CHOICES))}")
    if any((info_screen_prompt, info_tagger_prompt, info_judge_prompt)) \
            and pipeline != AnalysisTask.PIPELINE_INFOSPACE:
        raise ToolError(
            "info_screen_prompt, info_tagger_prompt, info_judge_prompt пишуться лише "
            "в конвеєр infospace")
    extras = _collect_task_fields(
        drop_linked_comments=drop_linked_comments, dedup_pre_thresh=dedup_pre_thresh,
        dedup_cand_thresh=dedup_cand_thresh, dedup_judge_prompt=dedup_judge_prompt,
        generic_sides=generic_sides, review_model=review_model, review_prompt=review_prompt,
        agent_review_prompt=agent_review_prompt, mon_min_len=mon_min_len,
        mon_max_len=mon_max_len, prescreen_model=prescreen_model,
        prescreen_prompt=prescreen_prompt, prescreen_enabled=prescreen_enabled,
        tagger_prompt=tagger_prompt, dedup_group_days=dedup_group_days,
        dedup_group_fuzz=dedup_group_fuzz, dedup_llm_cluster=dedup_llm_cluster,
        dedup_cluster_prompt=dedup_cluster_prompt,
        research_audit_enabled=research_audit_enabled,
        research_audit_prompt=research_audit_prompt, info_screen_model=info_screen_model,
        info_max_age_days=info_max_age_days, info_match_window_hours=info_match_window_hours,
        info_update_summaries=info_update_summaries, info_retention_days=info_retention_days,
        stream_regex=stream_regex, stream_interval_min=stream_interval_min,
        stream_media_chat_id=stream_media_chat_id, search_terms=search_terms,
        search_days=search_days, search_limit_per_term=search_limit_per_term)
    for field_name, field_value in extras.items():
        _guard_task_field(pipeline, field_name)
        _coerce_task_field(field_name, field_value)
    cats = []
    for key in _split_csv(tag_categories):
        c = TagCategory.objects.filter(key=key).first()
        if not c:
            raise ToolError(f"категорії тегів «{key}» немає (список: tag_categories)")
        cats.append(c)
    who = registry.actor()
    t = AnalysisTask.objects.create(
        slug=slug, name=name.strip()[:200], pipeline=pipeline, description=description.strip(),
        telezip_query=telezip_query, display_name=(display_name or "").strip()[:120],
        languages=[x.strip() for x in languages.replace(",", " ").split() if x.strip()],
        is_active=bool(is_active), owner=who.user,
        info_screen_prompt=info_screen_prompt, info_tagger_prompt=info_tagger_prompt,
        info_judge_prompt=info_judge_prompt)
    if cats:
        t.tag_categories.set(cats)
    if extras:
        applied = []
        _apply_task_fields(t, extras, applied)
        t.save()
    return fmt.joinsec(
        fmt.section(f"Задача #{t.id} {t.slug} створена", fmt.kv([
            ("назва", t.name), ("конвеєр", f"{t.pipeline} — {t.get_pipeline_display()}"),
            ("власник", who.user.username if who.user else "— (спільна)"),
            ("мови", ", ".join(t.languages) or "—"),
            ("категорії тегів", ", ".join(c.key for c in cats) or "—"),
            ("запит", fmt.trunc(t.telezip_query, 160) or "—"),
            ("активна", fmt.flag(t.is_active)),
        ])),
        _PIPELINE_HOW.get(t.pipeline, ""),
        f"/admin/analysis/analysistask/{t.id}/change/")


@tool("task_update", group="monitoring", mutates=True, params={
      "ref": 'Задача: числовий id, slug або частина назви. Неоднозначність або чужа задача — відповість «не знайдено».',
      "telezip_query": "Новий пошуковий запит задачі. Порожньо = не змінювати; щоб ОЧИСТИТИ запит, передай '-'. УВАГА, ДІАЛЕКТ ІНШИЙ, НІЖ У tz_find: збір ходить у v3, де ПРОБІЛ = АБО, а І — це `+` перед групою (`тема +(дія) -(шум)`). У tz_find (v4) навпаки. Запит, перенесений звідси в tz_find без переписування, тихо дасть 0 збігів, і навпаки.",
      "languages": "Мови через кому (ru, uk…). Порожньо = не змінювати; очистити список звідси не можна — це робиться в адмінці задачі.",
      "unique": "Згортати репости при зборі. Не передавати = не змінювати.",
      "chunk_days": "Розмір чанка збору в днях. 0 = не змінювати.",
      "is_active": "Увімкнути/вимкнути задачу. Не передавати = не змінювати.",
      "min_subscribers": "Відсівати канали, менші за це число підписників. -1 = не змінювати, 0 = вимкнути фільтр.",
      "llm_model": "Перевизначити модель LLM для задачі. Порожньо = не змінювати; повернути дефолт із коду звідси не можна — це робиться в адмінці задачі.",
      "display_name": "Людська назва секції для простого інтерфейсу (/app/), напр. «Інформпростір регіонів». Порожньо = не змінювати; '-' = очистити (показуватиметься технічна назва).",
      "name": "Технічна назва задачі. Порожньо = не змінювати.",
      "description": "Опис задачі (для людей). Порожньо = не змінювати; '-' = очистити.",
      "search_posts": "Збирати пости каналів. Не передавати = не змінювати.",
      "search_comments": "Збирати коментарі/повідомлення чатів. Не передавати = не змінювати.",
      "geo_enabled": "Визначати регіон подій (гео-стадія). Не передавати = не змінювати.",
      "review_enabled": "Авто-аудит подій LLM після створення. Не передавати = не змінювати.",
      "dedup_window_days": "Вікно дедупу подій у днях. 0 = не змінювати.",
      "classify_prompt": "Лише конвеєр events: system-промпт класифікації (поле classify_system_prompt). Порожньо = не змінювати; '-' = очистити поле. Для infospace це інше поле — info_screen_prompt / info_tagger_prompt. Перед зміною подивись task_show.",
      "info_screen_prompt": "Лише infospace: системний промпт скріну. Порожньо = не змінювати; '-' = очистити (тоді береться дефолт із коду). Поточний зібраний текст — task_show.",
      "info_tagger_prompt": "Лише infospace: додаткові правила тегів, доклеюються до скрін-промпта. Порожньо = не змінювати; '-' = очистити (лишаться підказки категорій). Це НЕ підказка категорії (hint).",
      "info_judge_prompt": "Лише infospace: промпт судді зіставлення. Порожньо = не змінювати; '-' = очистити (дефолт із коду).",
      "tag_categories": "Ключі категорій тегів через кому — замінити набір задачі. Порожньо = не змінювати; '-' = відв'язати всі.",
      **_EXTRA_PARAM_DOCS})
def task_update(ref: str, telezip_query: str = "", languages: str = "",
                unique: bool = None, chunk_days: int = 0, is_active: bool = None,
                min_subscribers: int = -1, llm_model: str = "", display_name: str = "",
                name: str = "", description: str = "", search_posts: bool = None,
                search_comments: bool = None, geo_enabled: bool = None,
                review_enabled: bool = None, dedup_window_days: int = 0,
                classify_prompt: str = "", info_screen_prompt: str = "",
                info_tagger_prompt: str = "", info_judge_prompt: str = "",
                tag_categories: str = "",
                drop_linked_comments: bool = None,
                dedup_pre_thresh: int = None, dedup_cand_thresh: int = None,
                dedup_judge_prompt: str = "", generic_sides: str = "",
                review_model: str = "", review_prompt: str = "",
                agent_review_prompt: str = "",
                mon_min_len: int = None, mon_max_len: int = None,
                prescreen_model: str = "", prescreen_prompt: str = "",
                prescreen_enabled: bool = None, tagger_prompt: str = "",
                dedup_group_days: int = None, dedup_group_fuzz: int = None,
                dedup_llm_cluster: bool = None, dedup_cluster_prompt: str = "",
                research_audit_enabled: bool = None, research_audit_prompt: str = "",
                info_screen_model: str = "", info_max_age_days: int = None,
                info_match_window_hours: int = None, info_update_summaries: bool = None,
                info_retention_days: int = None,
                stream_regex: str = "", stream_interval_min: int = None,
                stream_media_chat_id: str = "",
                search_terms: str = "", search_days: int = None,
                search_limit_per_term: int = None):
    """Змінити задачу: кожне поле картки task_show (ім'я в дужках) — параметр тут, для свого конвеєра.

    classify_prompt — лише events; info_screen_prompt, info_tagger_prompt, info_judge_prompt — лише infospace. Решта полів етапів називаються як у моделі.

    Який тип у задачі — перший рядок task_show. Запит TeleZip (events/monitor):
    `tz_find(stats=true)` показав обсяг → фіксуємо тут → `run_create`. Старий
    запит друкується у відповіді — щоб було куди відкотитись.
    """
    t = common.resolve_task(ref)
    if classify_prompt and t.pipeline != AnalysisTask.PIPELINE_EVENTS:
        raise ToolError(
            f"{t.slug} — конвеєр {t.pipeline}, classify_prompt пише лише events. "
            + _PIPELINE_HOW.get(t.pipeline, ""))
    info_prompts = (
        ("info_screen_prompt", info_screen_prompt),
        ("info_tagger_prompt", info_tagger_prompt),
        ("info_judge_prompt", info_judge_prompt),
    )
    for arg_name, value in info_prompts:
        if value and t.pipeline != AnalysisTask.PIPELINE_INFOSPACE:
            raise ToolError(
                f"{t.slug} — конвеєр {t.pipeline}, {arg_name} пише лише infospace. "
                + _PIPELINE_HOW.get(t.pipeline, ""))
    extras = _collect_task_fields(
        drop_linked_comments=drop_linked_comments, dedup_pre_thresh=dedup_pre_thresh,
        dedup_cand_thresh=dedup_cand_thresh, dedup_judge_prompt=dedup_judge_prompt,
        generic_sides=generic_sides, review_model=review_model, review_prompt=review_prompt,
        agent_review_prompt=agent_review_prompt, mon_min_len=mon_min_len,
        mon_max_len=mon_max_len, prescreen_model=prescreen_model,
        prescreen_prompt=prescreen_prompt, prescreen_enabled=prescreen_enabled,
        tagger_prompt=tagger_prompt, dedup_group_days=dedup_group_days,
        dedup_group_fuzz=dedup_group_fuzz, dedup_llm_cluster=dedup_llm_cluster,
        dedup_cluster_prompt=dedup_cluster_prompt,
        research_audit_enabled=research_audit_enabled,
        research_audit_prompt=research_audit_prompt, info_screen_model=info_screen_model,
        info_max_age_days=info_max_age_days, info_match_window_hours=info_match_window_hours,
        info_update_summaries=info_update_summaries, info_retention_days=info_retention_days,
        stream_regex=stream_regex, stream_interval_min=stream_interval_min,
        stream_media_chat_id=stream_media_chat_id, search_terms=search_terms,
        search_days=search_days, search_limit_per_term=search_limit_per_term)
    for field_name, field_value in extras.items():
        _guard_task_field(t.pipeline, field_name)
        _coerce_task_field(field_name, field_value)
    changed = []
    if telezip_query:
        old = t.telezip_query
        # порожній рядок означає «не чіпати», тож для очищення потрібен явний
        # маркер — інакше запит задачі неможливо стерти взагалі
        t.telezip_query = "" if telezip_query.strip() == "-" else telezip_query
        changed.append(f"запит: було «{fmt.trunc(old, 160)}»"
                       + ("; стало ПОРОЖНЬО" if not t.telezip_query else ""))
    if languages:
        t.languages = [x.strip() for x in languages.replace(",", " ").split() if x.strip()]
        changed.append(f"мови={t.languages}")
    if unique is not None:
        t.telezip_unique = bool(unique)
        changed.append(f"unique={fmt.flag(t.telezip_unique)} "
                       f"({'репости згортаються' if t.telezip_unique else 'повне охоплення'})")
    if chunk_days:
        t.collect_chunk_days = max(1, int(chunk_days))
        changed.append(f"чанк={t.collect_chunk_days} дн")
    if is_active is not None:
        t.is_active = bool(is_active)
        changed.append(f"активна={fmt.flag(t.is_active)}")
    if min_subscribers >= 0:
        t.min_channel_subscribers = int(min_subscribers)
        changed.append(f"мін. підписників={t.min_channel_subscribers}")
    if llm_model:
        t.llm_model = llm_model
        changed.append(f"модель={llm_model}")
    if display_name:
        t.display_name = "" if display_name.strip() == "-" else display_name.strip()[:120]
        changed.append(f"назва для користувача: «{t.human_name}»")
    if name:
        t.name = name.strip()[:200]
        changed.append(f"назва={t.name}")
    if description:
        t.description = "" if description.strip() == "-" else description.strip()
        changed.append("опис " + ("очищено" if not t.description else f"({len(t.description)} симв)"))
    for flag_name, value, label in (("search_posts", search_posts, "пости"),
                                    ("search_comments", search_comments, "коментарі"),
                                    ("geo_enabled", geo_enabled, "гео"),
                                    ("review_enabled", review_enabled, "аудит LLM")):
        if value is not None:
            setattr(t, flag_name, bool(value))
            changed.append(f"{label}={fmt.flag(bool(value))}")
    if dedup_window_days:
        t.dedup_window_days = max(1, int(dedup_window_days))
        changed.append(f"вікно дедупу={t.dedup_window_days} дн")
    if classify_prompt:
        t.classify_system_prompt = "" if classify_prompt.strip() == "-" else classify_prompt
        changed.append("промпт класифікації " + ("→ дефолт із коду" if not t.classify_system_prompt
                                                 else f"({len(t.classify_system_prompt)} симв)"))
    for field, value in info_prompts:
        if not value:
            continue
        text = "" if value.strip() == "-" else value
        setattr(t, field, text)
        changed.append(f"{field} " + ("очищено" if not text else f"({len(text)} симв)"))
    if extras:
        _apply_task_fields(t, extras, changed)
    if tag_categories:
        from analysis.models import TagCategory
        if tag_categories.strip() == "-":
            t.tag_categories.clear()
            changed.append("категорії тегів очищено")
        else:
            cats = []
            for key in _split_csv(tag_categories):
                c = TagCategory.objects.filter(key=key).first()
                if not c:
                    raise ToolError(f"категорії тегів «{key}» немає (список: tag_categories)")
                cats.append(c)
            t.tag_categories.set(cats)
            changed.append("категорії тегів=" + ", ".join(c.key for c in cats))
    if not changed:
        return f"#{t.id} {t.slug}: нічого не змінено (жоден параметр не передано)"
    t.save()
    follow = ""
    if telezip_query and t.pipeline in _TELEZIP_RUNS:
        follow = "Зібрати ним період: run_create task=" + t.slug
    elif telezip_query:
        follow = _PIPELINE_HOW.get(t.pipeline, "")
    return fmt.joinsec(
        fmt.section(f"Задача #{t.id} {t.slug} · {t.pipeline}", "\n".join(changed)),
        f"новий запит: {fmt.trunc(t.telezip_query, 300)}" if telezip_query else "",
        follow)
