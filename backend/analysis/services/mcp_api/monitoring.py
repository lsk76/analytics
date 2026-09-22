"""Моніторинги: задачі, збори (runs), whitelist чатів, джерела інформпростору, зрізи подій."""
from datetime import timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone

from analysis.models import (AnalysisTask, Channel, Event, MonitorChat, Post,
                             ResearchRun, Source, SourceSubscription)
from analysis.services.mcp_api import common, fmt, registry
from analysis.services.mcp_api.registry import SCOPE_CREATE, ToolError, tool

PIPE_SHORT = {"events": "події", "monitor": "критика", "research": "дослідж.",
              "infospace": "інформпр.", "tgsearch": "TG-пошук"}


@tool("tasks_list", group="monitoring", params={
      "pipeline": 'Конвеєр: events | monitor | research | infospace | tgsearch. Порожньо = усі.',
      "active_only": "true — лише активні задачі."})
def tasks_list(pipeline: str = "", active_only: bool = False):
    """Задачі аналізу (моніторинги): конвеєр, обсяги, що до них підключено."""
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
            PIPE_SHORT.get(t.pipeline, t.pipeline),
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


@tool("task_show", group="monitoring", params={"ref": 'Задача: числовий id, slug або частина назви. Неоднозначність або чужа задача — відповість «не знайдено».'})
def task_show(ref: str):
    """Картка моніторингу: конфіг конвеєра, підключення, черги, події, останні збори."""
    t = common.resolve_task(ref)
    cfg = [("конвеєр", t.get_pipeline_display()), ("активна", fmt.flag(t.is_active)),
           ("власник", t.owner.username if t.owner_id else "—"),
           ("опис", fmt.trunc(t.description, 200))]
    if t.pipeline in ("events", "monitor", "research"):
        cfg += [("запит TeleZip", fmt.trunc(t.telezip_query, 200)),
                ("мови", t.languages or "—"),
                ("пости/коментарі", f"{fmt.flag(t.search_posts)}/{fmt.flag(t.search_comments)}"),
                ("чанк збору", f"{t.collect_chunk_days} дн"),
                ("мін. підписників", t.min_channel_subscribers or "—")]
    if t.pipeline == "events":
        cfg += [("дедуп", f"вікно {t.dedup_window_days} дн, pre {t.dedup_pre_thresh}%, "
                          f"cand {t.dedup_cand_thresh}%"),
                ("авто-аудит", fmt.flag(t.review_enabled))]
    if t.pipeline == "infospace":
        cfg += [("свіжість", f"{t.info_max_age_days} дн"),
                ("вікно збігу", f"{t.info_match_window_hours} год"),
                ("жива подія", fmt.flag(t.info_update_summaries)),
                ("ретеншн сирих", f"{t.info_retention_days} дн")]
    cfg += [("категорії тегів", ", ".join(c.key for c in t.tag_categories.all()) or "—"),
            ("модель LLM", t.llm_model or "дефолт")]
    parts = [fmt.section(f"Задача #{t.id} {t.slug} — {t.name}", fmt.kv(cfg))]

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

    if t.pipeline in ("monitor", "tgsearch", "research"):
        chats = t.monitor_chats.all()
        parts.append(fmt.section("Чати", fmt.kv([
            ("активних", chats.filter(is_active=True).count()),
            ("усього", chats.count()),
            ("у стрімі", chats.filter(is_active=True, stream_enabled=True).count()),
            ("без акаунта", chats.filter(is_active=True, tg_account__isnull=True).count()),
        ])) + "\n(деталі: chats_list task=" + t.slug + ")")
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
    """Запустити збір за період — створює ResearchRun і планує чанки (як «Збори → Додати»).

    Далі все ведуть воркери. Ідемпотентно за діапазонами: вже покриті чанки не дублюються.
    """
    from analysis.services import stages as _stages
    t = common.resolve_task(task)
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
      "forward_media": "Пересилати медіа цього чату в чат медіа задачі."})
def chat_update(chat: str, is_active: bool = None, stream_enabled: bool = None,
                account: str = "", priority: int = None, forward_media: bool = None):
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
      "group_by": "Розріз: day | week | month | region | task | tag:<ключ категорії> (напр. tag:importance). Ключі категорій окремим інструментом не віддаються — їх видно в адмінці, /admin/analysis/tagcategory/.",
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
    Категорії тегів для фільтра `tag` — `tag_categories`; id події — для
    `event_show`/`event_update`.
    """
    qs, desc = _event_filters(task, days, date_from, date_to, review_status, region,
                              settlement, tag, query, channel, min_channels, min_reach)
    ordering = {"newest": ("-event_date", "-id"), "oldest": ("event_date", "id"),
                "reach": ("-reach", "-id"), "channels": ("-channel_count", "-id")}.get(order)
    if not ordering:
        raise ToolError("order: newest | oldest | reach | channels")
    total = qs.count()
    rows = [[f"#{e.id}", str(e.event_date or "—"), e.task.slug if not task else "",
             {"approved": "✓", "pending": "?", "rejected": "✗"}.get(e.review_status, e.review_status),
             fmt.trunc(e.region_subject.name if e.region_subject_id else (e.region or "—"), 18),
             fmt.trunc(e.settlement, 14), e.channel_count, e.reach,
             fmt.trunc(_tags_short(e), 40), fmt.trunc(e.summary, 90)]
            for e in qs.select_related("task", "region_subject")
                        .prefetch_related("tags").order_by(*ordering)[:limit]]
    headers = ["id", "дата", "задача", "аудит", "регіон", "нас. пункт", "кан.", "охопл.", "теги", "опис"]
    if task:
        rows = [r[:2] + r[3:] for r in rows]
        headers = headers[:2] + headers[3:]
    return fmt.joinsec(
        fmt.section(f"Події: {total} (показано {len(rows)})", "; ".join(desc) or "без фільтрів"),
        fmt.table(headers, rows) if rows else "нічого не знайдено",
        "Фільтри: days/date_from/date_to, review_status, region, settlement, tag (кат:тег, кома = І), "
        "query, channel, min_channels, min_reach; order=newest|oldest|reach|channels.")


@tool("event_show", group="monitoring", params={"ref": "id події (з events_list)."})
def event_show(ref: str):
    """Картка події: опис, регіон, усі теги, аудит, пости-джерела з посиланнями."""
    ev = _resolve_event(ref)
    posts = ev.posts.select_related("channel").order_by("posted_at")[:15]
    return fmt.joinsec(
        fmt.section(f"Подія #{ev.id} · {ev.event_date} · {ev.task.slug}", fmt.kv([
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
        fmt.section("Пости", fmt.table(
            ["коли", "канал", "посилання"],
            [[str(p.posted_at)[:16], fmt.trunc(p.channel_name or (p.channel.title if p.channel_id else ""), 24),
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
    ще й ОПИТУВАВСЯ, потрібне джерело: `source_add` (воно саме створить рядок
    довідника, якщо його нема). У whitelist моніторингу — через адмінку.
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
    """Підписати задачу на джерело (або вимкнути підписку) — `SourceSubscription`.

    Саме підписка робить джерело «робочим» для `info_collect`. Вимкнення не
    видаляє ні джерело, ні зібране — лише виключає з наступних зборів.
    """
    src = common.resolve_source(ref)
    t = common.resolve_task(task)
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
      "is_active": "Створити активною (дефолт true)."})
def task_create(slug: str, name: str, pipeline: str = "events", description: str = "",
                telezip_query: str = "", languages: str = "", tag_categories: str = "",
                display_name: str = "", is_active: bool = True):
    """Створити дослідження (`AnalysisTask`) — власник = ти, далі воно видиме
    лише тобі (і суперюзерам), як в адмінці.

    Мінімум — slug, назва і конвеєр; решту (запит, промпти, стадії) правиш
    `task_update`, канали/джерела підключаєш `source_add`/`source_subscribe`,
    збір запускаєш `run_create`. Промпти класифікації за замовчуванням — із коду.
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
        is_active=bool(is_active), owner=who.user)
    if cats:
        t.tag_categories.set(cats)
    return fmt.joinsec(
        fmt.section(f"Задача #{t.id} {t.slug} створена", fmt.kv([
            ("назва", t.name), ("конвеєр", PIPE_SHORT.get(t.pipeline, t.pipeline)),
            ("власник", who.user.username if who.user else "— (спільна)"),
            ("мови", ", ".join(t.languages) or "—"),
            ("категорії тегів", ", ".join(c.key for c in cats) or "—"),
            ("запит", fmt.trunc(t.telezip_query, 160) or "—"),
            ("активна", fmt.flag(t.is_active)),
        ])),
        "Далі: task_update (промпти/стадії), source_add/source_subscribe (джерела), "
        f"run_create task={t.slug} (збір), /admin/analysis/analysistask/{t.id}/change/")


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
      "classify_prompt": "System-промпт класифікації постів (повний текст). Порожньо = не змінювати; '-' = повернути дефолт із коду. Перед зміною подивись поточний: task_show."})
def task_update(ref: str, telezip_query: str = "", languages: str = "",
                unique: bool = None, chunk_days: int = 0, is_active: bool = None,
                min_subscribers: int = -1, llm_model: str = "", display_name: str = "",
                name: str = "", description: str = "", search_posts: bool = None,
                search_comments: bool = None, geo_enabled: bool = None,
                review_enabled: bool = None, dedup_window_days: int = 0,
                classify_prompt: str = ""):
    """Змінити параметри задачі: збір (запит TeleZip, мови, unique, чанк), назву/опис,
    прапорці стадій (пости/коментарі, гео, аудит), вікно дедупу, промпт класифікації.

    Замикає маршрут розвідки: `tz_find(stats=true)` показав, що обсяг здоровий →
    фіксуємо його в задачі → `run_create` збирає ним period. Старий запит
    друкується у відповіді — щоб було куди відкотитись.
    """
    t = common.resolve_task(ref)
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
    if not changed:
        return f"#{t.id} {t.slug}: нічого не змінено (жоден параметр не передано)"
    t.save()
    return fmt.joinsec(
        fmt.section(f"Задача #{t.id} {t.slug}", "\n".join(changed)),
        f"новий запит: {fmt.trunc(t.telezip_query, 300)}" if telezip_query else "",
        "Зібрати ним період: run_create task=" + t.slug)
