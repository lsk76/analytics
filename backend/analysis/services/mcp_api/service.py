"""Стан сервісу цілком: черги конвеєрів, свіжість даних, налаштування, публікація."""
from datetime import timedelta

from django.db.models import Count, Max, Min
from django.utils import timezone

from analysis.models import (AnalysisTask, CollectChunk, Event, Post, PublishConfig,
                             PublishedEvent, ResearchRun, Setting, Source)
from analysis.services.mcp_api import common, fmt, registry
from analysis.services.mcp_api.registry import SCOPE_ADMIN, tool

TERMINAL = (Post.STAGE_DONE, Post.STAGE_FAILED)
NON_TERMINAL = [s for s, _ in Post.STAGE_CHOICES if s not in TERMINAL]
STAGE_LABEL = dict(Post.STAGE_CHOICES)

# Стадія постів -> воркер, який її розгрібає (для «черга росте — кого рестартити»)
STAGE_WORKER = {
    Post.STAGE_COLLECTED: "worker-enrich",
    Post.STAGE_ENRICHED: "worker-precluster",
    Post.STAGE_PRECLUSTERED: "worker-classify",
    Post.STAGE_CLASSIFIED: "worker-dedup",
    Post.STAGE_DEDUPED: "worker-review",
    Post.STAGE_MON_COLLECTED: "worker-mon-filter",
    Post.STAGE_MON_FILTERED: "worker-mon-prescreen",
    Post.STAGE_MON_PRESCREENED: "worker-mon-runs (агенти)",
    Post.STAGE_TGS_COLLECTED: "worker-tgs-screen",
    Post.STAGE_TGS_SCREENED: "worker-tgs-tag",
    Post.STAGE_TGS_TAGGED: "worker-tgs-tag",
    Post.STAGE_INFO_COLLECTED: "worker-info-screen",
    Post.STAGE_INFO_SCREENED: "worker-info-event",
}


@tool("service_health", group="service")
def service_health():
    """Загальний стан сервісу одним екраном: черги, збори, акаунти, джерела, публікація.

    Перше, що варто викликати: показує ЩО стоїть і ХТО це розгрібає.
    """
    now = timezone.now()
    parts = []

    # --- задачі -------------------------------------------------------------
    # зведення показує рівно те, що користувачу видно в адмінці: свої задачі
    # (суперюзер — усі), тож числа збігаються з тим, що він бачить у списках
    tasks = list(common.scope_tasks(AnalysisTask.objects).order_by("id"))
    active = [t for t in tasks if t.is_active]
    by_pipe = {}
    for t in active:
        by_pipe.setdefault(t.pipeline, []).append(t)
    parts.append(fmt.section(
        f"Задачі: {len(active)} активних із {len(tasks)}",
        "\n".join(f"{p:<10} {', '.join(f'#{t.id} {t.slug}' for t in ts)}"
                  for p, ts in sorted(by_pipe.items())) or "активних немає"))

    # --- черги постів -------------------------------------------------------
    rows = []
    q = (common.scope_by_task(Post.objects.filter(stage__in=NON_TERMINAL)).order_by()
         .values("stage").annotate(n=Count("id"), oldest=Min("created_at")))
    for r in sorted(q, key=lambda r: -r["n"]):
        rows.append([r["stage"], STAGE_LABEL.get(r["stage"], r["stage"]), r["n"],
                     fmt.ago(r["oldest"]), STAGE_WORKER.get(r["stage"], "—")])
    failed = common.scope_by_task(
        Post.objects.filter(stage=Post.STAGE_FAILED)).count()
    body = fmt.table(["стадія", "що це", "постів", "найстаріший", "хто розгрібає"], rows) \
        if rows else "черги порожні"
    parts.append(fmt.section(f"Черги постів (у роботі; failed: {failed})", body))

    # --- збір (чанки TeleZip) ----------------------------------------------
    chunks = common.scope_by_task(CollectChunk.objects)
    ch = dict(chunks.order_by().values_list("status").annotate(n=Count("id")))
    cooldown = chunks.filter(status="pending", next_retry_at__gt=now).count()
    parts.append(fmt.section(
        "Чанки збору",
        ", ".join(f"{k}: {v}" for k, v in sorted(ch.items())) +
        (f" (з них у бекофі: {cooldown})" if cooldown else "") or "немає"))

    # --- запуски ------------------------------------------------------------
    runs = (common.scope_by_task(ResearchRun.objects)
            .exclude(status__in=["done", "cancelled"])
            .select_related("task").order_by("-created_at")[:12])
    rows = [[f"#{r.id}", r.task.slug, r.status, f"{r.date_from}…{r.date_to}",
             r.posts_collected, fmt.ago(r.created_at)] for r in runs]
    waiting = [r for r in runs if r.status == "awaiting_agent"]
    body = fmt.table(["run", "задача", "статус", "період", "постів", "створено"], rows) \
        if rows else "активних зборів немає"
    if waiting:
        body += ("\n\n⚠ чекають АГЕНТА (тегування/аудит): "
                 + ", ".join(f"#{r.id}" for r in waiting)
                 + " — батчі в backend/_dir/runs/run_<id>/")
    parts.append(fmt.section("Збори (незавершені)", body))

    # --- свіжість даних по активних задачах ---------------------------------
    # ОДНИМ згрупованим запитом на всю таблицю, а не «останній рядок задачі» в
    # циклі: на кількох мільйонах постів зворотний скан pk-індексу під фільтром
    # задачі коштував ~3с НА ЗАДАЧУ (45с на екран). Max(posted_at) лягає на
    # індекс (task, stage, posted_at) — 1.3с на всі задачі разом.
    posts_agg = {r["task_id"]: r for r in common.scope_by_task(Post.objects).order_by()
                 .values("task_id").annotate(n=Count("id"), last=Max("posted_at"))}
    events_agg = {r["task_id"]: r for r in common.scope_by_task(Event.objects).order_by()
                  .values("task_id").annotate(n=Count("id"), last=Max("created_at"))}
    rows = []
    for t in active:
        pa, ea = posts_agg.get(t.id, {}), events_agg.get(t.id, {})
        rows.append([f"#{t.id}", t.slug, t.pipeline, pa.get("n", 0),
                     fmt.ago(pa.get("last")), ea.get("n", 0), fmt.ago(ea.get("last"))])
    parts.append(fmt.section(
        "Свіжість (дата останнього матеріалу / коли створено останню подію)",
        fmt.table(["id", "задача", "конвеєр", "постів", "ост. матеріал",
                   "подій", "ост. подія"], rows)))

    # --- акаунти й проксі ---------------------------------------------------
    from accounts.models import Proxy, TelegramAccount
    acc = common.scope_accounts(TelegramAccount.objects)
    a_total, a_active = acc.count(), acc.filter(is_active=True).count()
    a_auth = acc.filter(is_active=True, is_authenticated=True).count()
    spam = dict(acc.filter(is_active=True).order_by().values_list("spam_status")
                .annotate(n=Count("id")))
    px = Proxy.objects.filter(is_active=True)
    parts.append(fmt.section("Telegram-акаунти", fmt.kv([
        ("акаунтів", f"{a_active} активних із {a_total}, авторизованих {a_auth}"),
        ("SpamBot", ", ".join(f"{k}: {v}" for k, v in sorted(spam.items())) or "—"),
        ("проксі", f"{px.filter(is_working=True).count()} робочих із {px.count()} активних"),
    ])))

    # --- джерела інформпростору --------------------------------------------
    # «Прострочений полінг» рахуємо ЛИШЕ по тих, кого воркер реально бере: у
    # джерела без активної підписки next_poll_at лежить у минулому вічно.
    src = Source.objects.filter(is_active=True)
    polled = src.filter(id__in=common.pollable_source_ids())
    overdue = polled.filter(next_poll_at__lt=now - timedelta(minutes=30)).count()
    bad_q = polled.filter(quality_ok=False).count()
    failing = polled.filter(consecutive_failures__gte=3).count()
    n_polled = polled.count()
    parts.append(fmt.section("Джерела (інформпростір)", fmt.kv([
        ("активних", f"{src.count()} (опитуються {n_polled}, "
                     f"без підписок {src.count() - n_polled})"),
        ("прострочений полінг (>30хв)", overdue or "—"),
        ("🟡 підозра на якість", bad_q or "—"),
        ("збоїть (3+ поспіль)", failing or "—"),
    ])))

    # --- публікація ---------------------------------------------------------
    pub_cfgs = PublishConfig.objects.filter(is_active=True)
    if not registry.actor().is_superuser:
        pub_cfgs = pub_cfgs.filter(owner=registry.actor().user)
    cfgs = pub_cfgs.count()
    day = now - timedelta(days=1)
    pub = dict(PublishedEvent.objects.filter(created_at__gte=day,
                                             config__in=pub_cfgs).order_by()
               .values_list("status").annotate(n=Count("id")))
    parts.append(fmt.section("Публікація", fmt.kv([
        ("активних профілів", cfgs),
        ("за добу", ", ".join(f"{k}: {v}" for k, v in sorted(pub.items())) or "нічого"),
    ])))
    return fmt.joinsec(*parts)


@tool("service_queues", group="service", params={
      "task": "Задача: id, slug або частина назви. Порожньо = усі видимі.",
      "stage": "Стадія конвеєра: collected | enriched | preclustered | classified | deduped | mon_collected | mon_filtered | mon_prescreened | tgs_collected | tgs_screened | tgs_tagged | info_collected | info_screened | done | failed. Порожньо = усі непорожні.",
      "errors": "Скільки прикладів свіжих помилок показати (0 — без них)."})
def service_queues(task: str = "", stage: str = "", errors: int = 3):
    """Черги конвеєра детально: стадії×задачі, застряглі claim'и, свіжі помилки.

    task — id/slug/назва (порожньо = всі); stage — конкретна стадія;
    errors — скільки прикладів помилок показати (0 = без них).
    """
    qs = common.scope_by_task(Post.objects.all())
    if task:
        qs = qs.filter(task=common.resolve_task(task))
    if stage:
        qs = qs.filter(stage=stage)
    rows = []
    agg = (qs.filter(stage__in=NON_TERMINAL + [Post.STAGE_FAILED]).order_by()
           .values("task_id", "task__slug", "stage")
           .annotate(n=Count("id"), oldest=Min("created_at"),
                     locked=Count("stage_locked_at")))
    for r in sorted(agg, key=lambda r: (r["task__slug"], -r["n"])):
        rows.append([f"#{r['task_id']}", r["task__slug"], r["stage"], r["n"],
                     r["locked"] or "", fmt.ago(r["oldest"])])
    body = fmt.table(["id", "задача", "стадія", "постів", "захоплено", "найстаріший"],
                     rows) if rows else "черги порожні"
    parts = [fmt.section("Пости в роботі", body)]

    stuck = qs.filter(stage_locked_at__lt=timezone.now() - timedelta(hours=2)) \
              .exclude(stage__in=TERMINAL).count()
    if stuck:
        parts.append(f"⚠ {stuck} постів захоплено воркером >2 год тому — "
                     "ймовірно, воркер помер під час обробки (рестарт звільнить).")

    if errors:
        errs = (qs.filter(stage=Post.STAGE_FAILED).exclude(stage_error="")
                .order_by("-id").values("id", "task__slug", "stage_error")[:errors])
        if errs:
            parts.append(fmt.section("Свіжі помилки", "\n".join(
                f"#{e['id']} {e['task__slug']}: {fmt.trunc(e['stage_error'], 220)}"
                for e in errs)))
    return fmt.joinsec(*parts)


@tool("settings_list", group="service", params={
      "prefix": "Показати лише ключі, що містять цей текст."})
def settings_list(prefix: str = ""):
    """Key-value налаштування (`Setting`): промпти, тексти, прапорці без деплою."""
    qs = Setting.objects.all()
    if prefix:
        qs = qs.filter(key__icontains=prefix)
    rows = [[s.key, fmt.trunc(s.description, 60), len(s.value or ""),
             fmt.trunc(s.value, 80), fmt.ago(s.updated_at)]
            for s in qs.order_by("key")]
    return fmt.table(["ключ", "опис", "симв.", "значення", "оновлено"], rows) \
        if rows else "налаштувань немає"


@tool("setting_set", mutates=True, group="service", scope=SCOPE_ADMIN, params={
      "key": "Ключ налаштування (напр. digest_report_prompt). Неіснуючий буде створено.",
      "value": "Нове значення. ПОРОЖНЄ = повернутись до дефолту з коду.",
      "description": "Опис для адмінки. Порожньо = не чіпати наявний."})
def setting_set(key: str, value: str, description: str = ""):
    """Записати налаштування (`Setting`). Порожнє значення = дефолт із коду.

    Ключі не валідуються: одруківка не впаде з помилкою, а створить НОВИЙ
    непотрібний рядок, і код далі читатиме старий ключ. Тому спершу звір ім'я
    через `settings_list`, а у відповіді дивись позначку «створено/оновлено».
    """
    obj, created = Setting.objects.get_or_create(key=key.strip())
    old = obj.value
    obj.value = value
    if description:
        obj.description = description
    obj.save()
    return (f"{'створено' if created else 'оновлено'} `{obj.key}` "
            f"({len(old or '')} → {len(value or '')} симв.)\n"
            f"було: {fmt.trunc(old, 200) or '(порожньо)'}\n"
            f"стало: {fmt.trunc(value, 200) or '(порожньо — дефолт із коду)'}")


@tool("publish_status", group="service", params={
      "limit": "Скільки останніх публікацій показати."})
def publish_status(limit: int = 10):
    """Профілі публікації + останні публікації подій у Telegram."""
    rows = []
    cfg_qs = PublishConfig.objects.order_by("-is_active", "id")
    if not registry.actor().is_superuser:
        cfg_qs = cfg_qs.filter(owner=registry.actor().user)
    for c in cfg_qs:
        pub = c.published.order_by()
        rows.append([f"#{c.id}", fmt.flag(c.is_active), fmt.trunc(c.name, 28),
                     c.task.slug if c.task_id else "всі",
                     pub.filter(status=PublishedEvent.STATUS_PUBLISHED).count(),
                     pub.filter(status=PublishedEvent.STATUS_SKIPPED).count(),
                     pub.filter(status=PublishedEvent.STATUS_FAILED).count(),
                     fmt.ago(pub.aggregate(m=Max("published_at"))["m"])])
    parts = [fmt.section("Профілі", fmt.table(
        ["id", "акт", "назва", "задача", "опубл.", "відсіяно", "збій", "остання"], rows)
        if rows else "профілів немає")]

    last = (PublishedEvent.objects.filter(config__in=cfg_qs)
            .select_related("config", "event").order_by("-created_at")[:limit])
    rows = [[f"#{p.id}", p.config.name[:18], p.status,
             fmt.trunc(p.event.summary if p.event_id else "", 46),
             fmt.trunc(p.ai_reason or p.error, 40), fmt.ago(p.created_at)]
            for p in last]
    parts.append(fmt.section("Останні публікації", fmt.table(
        ["id", "профіль", "статус", "подія", "причина/помилка", "коли"], rows)
        if rows else "публікацій немає"))
    return fmt.joinsec(*parts)


@tool("tools_manifest", group="service")
def tools_manifest():
    """Список інструментів MCP-шару (назва, група, чи змінює стан, параметри)."""
    from analysis.services.mcp_api.registry import manifest
    # «платне» окремою колонкою: інакше tz_find виглядає як звичайне читання,
    # хоч кожен його виклик коштує грошей (див. docs/telezip-api.md §4)
    paid = {"tz_find", "tz_channels", "tz_users"}
    rows = []
    for m in manifest():
        mode = "змінює" if m["mutates"] else "читає"
        if m["scope"] == "mcp:admin":
            mode += " (адмін)"
        rows.append([m["name"], m["group"], mode,
                     "$0.10" if m["name"] in paid else "",
                     ", ".join(p["name"] for p in m["params"]) or "—",
                     fmt.trunc(m.get("summary") or m["doc"], 90)])
    return (fmt.table(["інструмент", "група", "режим", "ціна виклику",
                       "параметри", "що робить"], rows)
            + "\n\nПовний опис інструмента з усіма застереженнями — у його схемі "
              "(поле description), тут лише перший рядок.")
