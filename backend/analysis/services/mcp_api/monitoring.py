"""Моніторинги: задачі, збори (runs), whitelist чатів, джерела інформпростору, зрізи подій."""
from datetime import timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone

from analysis.models import (AnalysisTask, Channel, Event, MonitorChat, Post,
                             ResearchRun, Source, SourceSubscription)
from analysis.services.mcp_api import common, fmt, registry
from analysis.services.mcp_api.registry import ToolError, tool

PIPE_SHORT = {"events": "події", "monitor": "критика", "research": "дослідж.",
              "infospace": "інформпр.", "tgsearch": "TG-пошук"}


@tool("tasks_list", group="monitoring")
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


@tool("task_show", group="monitoring")
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


@tool("runs_list", group="monitoring")
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


@tool("run_show", group="monitoring")
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


@tool("run_create", group="monitoring", mutates=True)
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


@tool("run_cancel", group="monitoring", mutates=True)
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


@tool("chats_list", group="monitoring")
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


@tool("chat_update", group="monitoring", mutates=True)
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


@tool("sources_list", group="monitoring")
def sources_list(task: str = "", kind: str = "", problems_only: bool = False,
                 limit: int = 60):
    """Джерела інформпростору: розклад полінгу, health, якість, до яких задач підключені."""
    qs = (Source.objects.select_related("region_subject", "tg_account")
          .annotate(n_subs=Count("subscriptions", filter=Q(subscriptions__is_active=True)))
          .order_by("kind", "name"))
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


@tool("source_update", group="monitoring", mutates=True)
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


@tool("events_stats", group="monitoring")
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


@tool("channels_find", group="monitoring")
def channels_find(query: str, limit: int = 20):
    """Знайти канал/чат у довіднику (`Channel`) за username/назвою — id для інших дій."""
    qs = (Channel.objects.filter(Q(username__icontains=query.lstrip("@"))
                                 | Q(title__icontains=query))
          .select_related("region_subject").order_by("-subscribers")[:limit])
    rows = [[f"#{c.id}", f"@{c.username}" if c.username else "—", fmt.trunc(c.title, 36),
             c.subscribers or "", c.region_subject.name if c.region_subject_id else "—",
             c.enrolled_in.count()] for c in qs]
    return fmt.table(["id", "username", "назва", "підписників", "регіон", "у моніторингах"],
                     rows) if rows else f"каналів за «{query}» немає"


@tool("task_update", group="monitoring", mutates=True)
def task_update(ref: str, telezip_query: str = "", languages: str = "",
                unique: bool = None, chunk_days: int = 0, is_active: bool = None,
                min_subscribers: int = -1, llm_model: str = ""):
    """Змінити параметри збору задачі: запит TeleZip, мови, unique, розмір чанка.

    Замикає маршрут розвідки: `tz_stats` показав, що обсяг здоровий →
    фіксуємо його в задачі → `run_create` збирає ним period. Старий запит
    друкується у відповіді — щоб було куди відкотитись.
    """
    t = common.resolve_task(ref)
    changed = []
    if telezip_query:
        old = t.telezip_query
        t.telezip_query = telezip_query
        changed.append(f"запит: було «{fmt.trunc(old, 160)}»")
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
    if not changed:
        return f"#{t.id} {t.slug}: нічого не змінено (жоден параметр не передано)"
    t.save()
    return fmt.joinsec(
        fmt.section(f"Задача #{t.id} {t.slug}", "\n".join(changed)),
        f"новий запит: {fmt.trunc(t.telezip_query, 300)}" if telezip_query else "",
        "Зібрати ним період: run_create task=" + t.slug)
