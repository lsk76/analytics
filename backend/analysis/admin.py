import datetime
import json
from collections import OrderedDict

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import AdminDateWidget
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, Sum, F
from django.db.models.functions import TruncDate, TruncWeek, TruncMonth, Coalesce
from django.shortcuts import render
from django.urls import path
from django.utils.functional import cached_property
from django.utils import timezone as djtz
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from rangefilter.filters import DateRangeFilterBuilder, DateRangeFilter


# --- date range filter that speaks YYYY-MM-DD (not the uk-locale DD.MM.YYYY) ---
# The whole app standardises on ISO dates: chart drill-down URLs build
# ?posted_at__range__gte=YYYY-MM-DD, and the manual picker must parse/display the
# same. localize=False + explicit ISO input_formats/widget format bypass the locale.
_ISO_FMT = "%Y-%m-%d"


class ISODateRangeFilter(DateRangeFilter):
    def _get_form_fields(self):
        def field(initial):
            return forms.DateField(
                label="", required=False, initial=initial, localize=False,
                input_formats=[_ISO_FMT, "%d.%m.%Y"],   # ISO first; accept legacy too
                widget=AdminDateWidget(format=_ISO_FMT, attrs={"placeholder": "YYYY-MM-DD"}),
            )
        return OrderedDict((
            (self.lookup_kwarg_gte, field(self.default_gte)),
            (self.lookup_kwarg_lte, field(self.default_lte)),
        ))


def ISODateRangeFilterBuilder(title=None, default_start=None, default_end=None):
    return type(str("ISODateRangeFilter"), (ISODateRangeFilter,), {
        "__from_builder": True, "default_title": title,
        "default_start": default_start, "default_end": default_end,
    })

from .services import stages

from .models import (
    AnalysisTask, Channel, Tag, TagAlias, TagCategory,
    Post, Event, ResearchRun, CollectChunk,
    Region, RegionAlias, MonitorChat, ChannelDailyStat,
)
from .multiselect_filter import (
    multiselect_filter, autocomplete_filter, MultiSelectFilter,
)

# --- reusable filter widgets (pso-style) -----------------------------------
TaskFilter = multiselect_filter(AnalysisTask, "Задача", "task", ordering="name")


class TaskSingleFilter(admin.SimpleListFilter):
    """Задача: ОДИНОЧНИЙ вибір (стокові лінки — клік на іншу задачу замінює
    вибір). Параметр лишається ?task=<id>, тож усі наявні посилання
    (матриця, графіки, закладки) працюють без змін."""
    title = "Задача"
    parameter_name = "task"

    def lookups(self, request, model_admin):
        return [(str(t.id), t.name) for t in AnalysisTask.objects.order_by("name")]

    def queryset(self, request, queryset):
        v = self.value()
        return queryset.filter(task_id=v) if v else queryset


def facet_base(changelist, request, exclude_spec):
    """Queryset with every OTHER filter (and search) applied, but not exclude_spec —
    the base for computing faceted option counts."""
    qs = changelist.root_queryset
    for spec in getattr(changelist, "filter_specs", []) or []:
        if spec is exclude_spec:
            continue
        try:
            res = spec.queryset(request, qs)
            if res is not None:
                qs = res
        except Exception:  # noqa: BLE001
            pass
    if getattr(changelist, "query", ""):
        qs, _se = changelist.model_admin.get_search_results(request, qs, changelist.query)
    return qs


class TagCategoryMultiSelectFilter(MultiSelectFilter):
    """
    Faceted per-tag include/exclude widget for ONE category.

    Each tag has TWO checkboxes side-by-side:
      ✓ — include this tag (events that HAVE it)
      ✗ — exclude this tag (events that DON'T have it)
    Different tags in the same category can be marked differently, e.g. tag A
    included while tag B excluded. The two checkboxes for the same tag are
    mutually exclusive (the JS enforces this).

    URL representation:
      ?tag_<cat>=<id>      — include (multi-value; OR-ed within category)
      ?tag_<cat>_excl=<id> — exclude (multi-value)
    Combined: queryset .filter(include).exclude(exclude). Across categories
    it's still AND.
    """
    category = None
    template = "admin/filters/multi_select_with_exclude.html"

    @property
    def exclude_param(self):
        return f"{self.parameter_name}_excl"

    def __init__(self, request, params, model, model_admin):
        super().__init__(request, params, model, model_admin)
        # SimpleListFilter pops only `parameter_name` from lookup_params; the
        # exclude companion would otherwise leak into queryset.filter() and
        # crash. Pop it here so Django stops treating it as a field lookup.
        params.pop(self.exclude_param, None)

    def expected_parameters(self):
        return [self.parameter_name, self.exclude_param]

    def queryset(self, request, queryset):
        # We override queryset (instead of filter_queryset) because we have
        # TWO param families to apply, not one.
        inc = self.request.GET.getlist(self.parameter_name)
        exc = self.request.GET.getlist(self.exclude_param)
        if inc:
            queryset = queryset.filter(tags__id__in=inc).distinct()
        if exc:
            queryset = queryset.exclude(tags__id__in=exc).distinct()
        return queryset

    def filter_queryset(self, queryset, values):  # kept for base-class contract
        return queryset.filter(tags__id__in=values)

    def lookups(self, request, model_admin):  # ensures has_output()
        return [(str(t.id), t.name)
                for t in Tag.objects.filter(category=self.category).order_by("name")]

    def _facet_base(self, changelist):
        return facet_base(changelist, self.request, self)

    def choices(self, changelist):
        """Yield one item per tag with `included` AND `excluded` flags.

        The first item is the standard "All" reset link that clears BOTH
        include and exclude sets for this category.
        """
        included = self.request.GET.getlist(self.parameter_name)
        excluded = self.request.GET.getlist(self.exclude_param)
        yield {
            "selected": not (included or excluded),
            "query_string": changelist.get_query_string(
                remove=[self.parameter_name, self.exclude_param]),
            "display": _("All"),
            "value": "__all__",
        }
        base = self._facet_base(changelist)
        ids = list(base.values_list("pk", flat=True).distinct())
        # model-agnostic facet counts: works for Event AND Post (both have `tags`)
        model = changelist.model
        rows = (model._default_manager.filter(pk__in=ids, tags__category=self.category)
                .values("tags__id", "tags__name")
                .annotate(n=Count("pk", distinct=True)))
        present = {str(r["tags__id"]): (r["tags__name"], r["n"]) for r in rows}
        # Keep currently-selected (include OR exclude) tags visible even if
        # they yield 0 in the current facet — else the user can't untick them.
        for tid in set(included) | set(excluded):
            if tid not in present:
                t = Tag.objects.filter(id=tid).first()
                if t:
                    present[tid] = (t.name, 0)
        for tid, (name, n) in sorted(present.items(), key=lambda kv: kv[1][0]):
            yield {
                "included": tid in included,
                "excluded": tid in excluded,
                "display": f"{name} ({n})",
                "value": tid,
            }


def tag_category_filter(category: str, label: str):
    """Dynamic 'category -> its tags' faceted multiselect (one per category)."""
    return type(
        f"Tag{category.capitalize()}Filter",
        (TagCategoryMultiSelectFilter,),
        {"title": label, "parameter_name": f"tag_{category}", "category": category},
    )

class SubjectFilter(MultiSelectFilter):
    """Faceted select2 multi-select for RF subject: only subjects present in the
    current selection (other filters applied), each with its event count.
    Options are rendered server-side; select2 just adds search + multi-tag UI."""
    title = "Суб'єкт РФ"
    parameter_name = "region_id"
    template = "admin/filters/facet_autocomplete.html"
    placeholder = "Пошук суб'єкта…"

    def filter_queryset(self, queryset, values):
        return queryset.filter(region_subject_id__in=values)

    def lookups(self, request, model_admin):  # only for has_output(); choices() facets
        return [(str(r.id), r.name) for r in
                Region.objects.filter(events__isnull=False).distinct().order_by("name")]

    def choices(self, changelist):
        selected = self.request.GET.getlist(self.parameter_name)
        yield {
            "selected": len(selected) == 0,
            "query_string": changelist.get_query_string(remove=[self.parameter_name]),
            "display": _("All"),
            "value": "__all__",
        }
        base = facet_base(changelist, self.request, self)
        ids = list(base.values_list("pk", flat=True).distinct())
        rows = (Event.objects.filter(pk__in=ids, region_subject__isnull=False)
                .values("region_subject__id", "region_subject__name")
                .annotate(n=Count("pk")))
        present = {str(r["region_subject__id"]): (r["region_subject__name"], r["n"])
                   for r in rows}
        for rid in selected:  # keep current picks visible even if 0 now
            if rid not in present:
                r = Region.objects.filter(id=rid).first()
                if r:
                    present[rid] = (r.name, 0)
        for rid, (name, n) in sorted(present.items(), key=lambda kv: kv[1][0]):
            yield {
                "selected": rid in selected,
                "query_string": "",
                "display": f"{name} ({n})",
                "value": rid,
            }

ChannelFilter = autocomplete_filter(
    title="Канал", parameter_name="channel_id",
    filter_field="posts__channel__id__in",
    selected_lookup=lambda req, ids: Channel.objects.filter(id__in=ids),
    autocomplete_url_name="channel-autocomplete", placeholder="Пошук каналу…")


class RegionAliasInline(admin.TabularInline):
    model = RegionAlias
    extra = 0


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("name", "kind")
    list_filter = ("kind",)
    search_fields = ("name", "aliases__raw")
    inlines = [RegionAliasInline]


@admin.action(description="▶ Поставити збір у чергу (за період job'а)")
def enqueue_job_action(modeladmin, request, queryset):
    for run in queryset:
        n = stages.enqueue_collection(run.task, run.date_from, run.date_to,
                                      chunk_days=run.chunk_days, job=run)
        run.status = "collecting"
        run.save(update_fields=["status"])
        messages.success(
            request,
            f"#{run.id} «{run.task.name}» {run.date_from}…{run.date_to}: +{n} чанків у черзі. "
            f"Воркери самі доведуть до подій.")


@admin.action(description="↻ Перепрогнати пайплайн (без TeleZip)")
def reprocess_period_action(modeladmin, request, queryset):
    for run in queryset:
        n_ev, n_posts = stages.reprocess_period(run.task, run.date_from, run.date_to)
        messages.success(
            request,
            f"#{run.id} «{run.task.name}» {run.date_from}…{run.date_to}: -{n_ev} подій, "
            f"скинуто {n_posts} постів → collected. Воркери доведуть назад до подій.")


@admin.action(description="⟳ Перезібрати з нуля (TeleZip)")
def recollect_fresh_action(modeladmin, request, queryset):
    for run in queryset:
        n_ev, n_posts, n_chunks = stages.recollect_fresh(
            run.task, run.date_from, run.date_to, job=run)
        run.status = "collecting"
        run.save(update_fields=["status"])
        messages.success(
            request,
            f"#{run.id} «{run.task.name}» {run.date_from}…{run.date_to}: -{n_ev} подій, "
            f"-{n_posts} постів, +{n_chunks} чанків у черзі.")


@admin.register(ResearchRun)
class ResearchRunAdmin(admin.ModelAdmin):
    list_display = ("__str__", "task", "date_from", "date_to", "status",
                    "chunk_progress", "stage_progress", "posts_collected", "created_at")
    list_filter = ("task", "status")
    search_fields = ("title", "task__name")
    actions = [enqueue_job_action, reprocess_period_action, recollect_fresh_action]
    readonly_fields = ("started_at", "finished_at", "stage_progress", "params", "stats",
                       "posts_collected", "posts_relevant", "events_total",
                       "events_corroborated", "created_at")
    change_list_template = "admin/analysis/researchrun/change_list.html"

    def save_model(self, request, obj, form, change):
        """«Новий запуск» одним кроком: створення запису в адмінці одразу планує
        чанки збору — далі все ведуть воркери (mon-collect/filter/prescreen +
        mon-runs для гібридного тегування; events-задачі — свої стадії)."""
        super().save_model(request, obj, form, change)
        if change or obj.status not in ("pending", "collecting"):
            return
        if obj.chunks.exists():
            return
        from .services import stages as _stages
        made = _stages.enqueue_collection(obj.task, obj.date_from, obj.date_to,
                                          chunk_days=obj.chunk_days, job=obj)
        obj.status = "collecting"
        obj.started_at = djtz.now()
        obj.save(update_fields=["status", "started_at"])
        messages.success(request, f"Запуск #{obj.id}: заплановано {made} чанків — "
                                  f"воркери підхоплять самі. Прогрес: Збори → Статус.")

    # ---------- status dashboard --------------------------------------------

    def get_urls(self):
        custom = [
            path("status/", self.admin_site.admin_view(self.status_view),
                 name="analysis_researchrun_status"),
        ]
        return custom + super().get_urls()

    def status_view(self, request):
        """Live dashboard for all active collection jobs. Auto-refresh every 30s."""
        now = djtz.now()
        # active = anything not terminally done; we still show recent done ones below
        active = list(ResearchRun.objects.select_related("task")
                      .exclude(status__in=["done", "cancelled"])
                      .order_by("-created_at"))
        recent_done = list(ResearchRun.objects.select_related("task")
                           .filter(status__in=["collected", "done"])
                           .order_by("-finished_at", "-created_at")[:5])

        def _job_card(j):
            chs = list(CollectChunk.objects.filter(job=j))
            c_total = len(chs)
            c_by = {"done": 0, "running": 0, "pending": 0, "failed": 0, "split": 0}
            cooldown = 0
            for ch in chs:
                c_by[ch.status] = c_by.get(ch.status, 0) + 1
                if ch.status == "pending" and ch.next_retry_at and ch.next_retry_at > now:
                    cooldown += 1
            # `split` chunks are terminal (delegated to children that ended up done),
            # so count them as finished progress.
            chunks_finished = c_by["done"] + c_by["split"]
            chunk_pct = round(100 * chunks_finished / c_total) if c_total else 0

            # post-stage breakdown for the job's date range (posts belong to task, not job)
            W = Post.objects.filter(task=j.task,
                                    posted_at__date__gte=j.date_from,
                                    posted_at__date__lte=j.date_to)
            pcounts = dict(W.values_list("stage").annotate(n=Count("id")))
            p_total = sum(pcounts.values())
            p_terminal = pcounts.get(Post.STAGE_DONE, 0) + pcounts.get(Post.STAGE_FAILED, 0)
            p_pct = round(100 * p_terminal / p_total) if p_total else 0

            # event + review breakdown for the period
            evs = Event.objects.filter(task=j.task,
                                       event_date__gte=j.date_from,
                                       event_date__lte=j.date_to)
            e_total = evs.count()
            r_by = dict(evs.values_list("review_status").annotate(n=Count("id")))
            r_approved = r_by.get(Event.REVIEW_APPROVED, 0)
            r_pct = round(100 * r_approved / e_total) if e_total else 0

            # an in-flight chunk to surface most recent activity
            last_run = (CollectChunk.objects.filter(job=j)
                        .exclude(locked_at__isnull=True)
                        .order_by("-locked_at").first())
            return {
                "job": j,
                "chunks_total": c_total, "chunks": c_by, "chunks_pct": chunk_pct,
                "chunks_cooldown": cooldown,
                "posts_total": p_total, "posts_pct": p_pct,
                "flow": self._stage_flow(j, W, pcounts),
                "posts_failed": pcounts.get(Post.STAGE_FAILED, 0),
                "events_total": e_total, "events_review": r_by, "events_pct": r_pct,
                "last_running": last_run,
            }

        ctx = {
            **self.admin_site.each_context(request),
            "title": "Статус збору",
            "opts": self.model._meta,
            "has_view_permission": True,
            "now": now,
            "active_cards": [_job_card(j) for j in active],
            "recent_done": [_job_card(j) for j in recent_done],
        }
        return render(request, "admin/analysis/researchrun/status.html", ctx)

    def changelist_view(self, request, extra_context=None):
        extra_context = dict(extra_context or {})
        return super().changelist_view(request, extra_context=extra_context)

    # пайплайн-стадії в порядку проходження (термінальні: done/failed).
    # Events- і monitor-стадії в одному списку: пости конкретної задачі живуть
    # лише на стадіях «свого» конвеєра, тож чужі рядки мають 0 і не показуються.
    _STAGE_SEQ = [
        (Post.STAGE_COLLECTED, "збір"),
        (Post.STAGE_ENRICHED, "збагач."),
        (Post.STAGE_PRECLUSTERED, "преклас."),
        (Post.STAGE_CLASSIFIED, "класиф."),
        (Post.STAGE_DEDUPED, "дедуп"),
        (Post.STAGE_MON_COLLECTED, "збір(м)"),
        (Post.STAGE_MON_FILTERED, "фільтр"),
        (Post.STAGE_MON_PRESCREENED, "прескрін+"),
        (Post.STAGE_DONE, "готово"),
    ]

    # ---- потік етапів для статус-картки --------------------------------------
    # «Черга етапу X» = пости, що стоять на ВХІДНІЙ стадії X (claim-модель:
    # захоплені воркером = «в роботі»); «помилки етапу» — по префіксу stage_error.
    _FLOW_MONITOR = [
        ("Фільтр",   Post.STAGE_MON_COLLECTED,   "mon_filter"),
        ("Прескрін", Post.STAGE_MON_FILTERED,    "mon_prescreen"),
        ("Тегування (агент)", Post.STAGE_MON_PRESCREENED, "mon_tag"),
    ]
    _FLOW_EVENTS = [
        ("Збагачення",   Post.STAGE_COLLECTED,    "enrich"),
        ("Прекластер",   Post.STAGE_ENRICHED,     "precluster"),
        ("Класифікація", Post.STAGE_PRECLUSTERED, "classify"),
        ("Дедуп",        Post.STAGE_CLASSIFIED,   "dedup"),
        ("Подієзбірка",  Post.STAGE_DEDUPED,      ""),
    ]

    def _stage_flow(self, j, W, pcounts):
        from django.db.models import Value
        from django.db.models.functions import StrIndex, Substr
        is_mon = j.task.pipeline == AnalysisTask.PIPELINE_MONITOR
        steps_cfg = self._FLOW_MONITOR if is_mon else self._FLOW_EVENTS
        locked = dict(W.filter(stage_locked_at__isnull=False)
                      .values_list("stage").annotate(n=Count("id")))
        # помилки по етапах: префікс stage_error до двокрапки
        failed_by = {}
        for pref, n in (W.filter(stage=Post.STAGE_FAILED)
                        .annotate(_p=Substr("stage_error", 1,
                                            StrIndex("stage_error", Value(":")) - 1))
                        .values_list("_p").annotate(n=Count("id"))):
            failed_by[pref or "інше"] = n
        steps = []
        for label, in_stage, err_pref in steps_cfg:
            queue = pcounts.get(in_stage, 0)
            working = locked.get(in_stage, 0)
            step = {"label": label,
                    "queue": max(queue - working, 0),
                    "working": working,
                    "failed": failed_by.pop(err_pref, 0) if err_pref else 0}
            if is_mon and in_stage == Post.STAGE_MON_PRESCREENED:
                # агентний крок: черга = лише prescreen-ПОЗИТИВНІ ще без вердикту
                step["queue"] = W.filter(
                    stage=Post.STAGE_MON_PRESCREENED, is_classified=False,
                    classification___prescreen__could_be_criticism=True).count()
                st = j.stats or {}
                if st.get("batches"):
                    step["batches"] = st.get("batches")
                    step["batches_done"] = st.get("batches_done", 0)
            steps.append(step)
        # хвіст помилок без відомого префікса — окремим бакетом
        other_failed = sum(failed_by.values())
        # термінал «Готово» з чесною розбивкою
        done_n = pcounts.get(Post.STAGE_DONE, 0)
        breakdown = []
        if is_mon and done_n:
            noise = W.filter(stage=Post.STAGE_DONE,
                             classification__is_filtered=True).count()
            crit = W.filter(stage=Post.STAGE_DONE, is_relevant=True).count()
            pre_neg = W.filter(
                stage=Post.STAGE_DONE,
                classification___prescreen__could_be_criticism=False).count()
            rest = max(done_n - noise - crit - pre_neg, 0)
            breakdown = [("відсіяно фільтром", noise),
                         ("відсіяно прескріном", pre_neg),
                         ("не критика (агент)", rest),
                         ("критика → події", crit)]
        return {"steps": steps, "done": done_n,
                "done_breakdown": [(l, n) for l, n in breakdown if n],
                "other_failed": other_failed}

    @admin.display(description="Чанки")
    def chunk_progress(self, obj):
        total = obj.chunks.count()
        # `split` is a terminal state: the parent delegated to 1-day children (which
        # themselves end up `done`). It is NOT outstanding work — count it as finished.
        done = obj.chunks.filter(status__in=["done", "split"]).count()
        pct = round(100 * done / total) if total else 0
        return format_html("{}/{} <small>({}%)</small>", done, total, pct) if total else "—"

    @admin.display(description="Стадії постів")
    def stage_progress(self, obj):
        """Розподіл постів задачі за період job'а по стадіях + % завершення.
        Пости не належать job'у напряму — рахуємо за task і period [date_from, date_to]."""
        counts = dict(
            Post.objects.filter(task=obj.task,
                                 posted_at__date__gte=obj.date_from,
                                 posted_at__date__lte=obj.date_to)
            .values_list("stage").annotate(n=Count("id"))
        )
        total = sum(counts.values())
        if not total:
            return "—"
        terminal = counts.get(Post.STAGE_DONE, 0) + counts.get(Post.STAGE_FAILED, 0)
        pct = round(100 * terminal / total)
        parts = [f"{lbl} {counts[st]}" for st, lbl in self._STAGE_SEQ if counts.get(st)]
        if counts.get(Post.STAGE_FAILED):
            parts.append(f"помилка {counts[Post.STAGE_FAILED]}")
        fill = pct // 10
        bar = "█" * fill + "░" * (10 - fill)
        return format_html("<b>{}%</b> <span style='font-family:monospace'>{}</span>"
                           "<br><small>{} / всього {}</small>",
                           pct, bar, " · ".join(parts), total)


@admin.register(CollectChunk)
class CollectChunkAdmin(admin.ModelAdmin):
    list_display = ("task", "date_from", "date_to", "status", "attempts",
                    "posts_collected", "finished_at")
    list_filter = ("task", "status")
    date_hierarchy = "date_from"
    readonly_fields = ("locked_at", "attempts", "posts_collected", "error",
                       "created_at", "finished_at")


@admin.register(TagCategory)
class TagCategoryAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "closed", "hint", "order")
    list_editable = ("label", "closed", "hint", "order")
    ordering = ("order", "key")


class MonitorChatInline(admin.TabularInline):
    """Whitelist чатів для opinion-моніторингу. Видно одразу на сторінці Task."""
    model = MonitorChat
    extra = 0
    autocomplete_fields = ("channel",)
    fields = ("channel", "is_active", "is_critical_source", "priority",
              "added_by", "notes")
    ordering = ("priority", "channel__username")


@admin.register(AnalysisTask)
class AnalysisTaskAdmin(admin.ModelAdmin):
    """Форма задачі = дві реюзабельні «рецептури», згруповані ПО ЕТАПАХ конвеєра:
      📰 Пошук подій: Збір → Класифікація → Дедуплікація → Аудит/резонансність
      💬 Моніторинг коментарів: Чати → Фільтрація → Прескрін → Тегування агентами
    JS у change_form ховає етапи чужого конвеєра (перемикач «Конвеєр» угорі)."""
    list_display = ("name", "slug", "pipeline", "geo_enabled",
                    "is_active", "drop_linked_comments", "monitor_chats_count")
    list_filter = ("pipeline", "is_active", "drop_linked_comments")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug")
    filter_horizontal = ("tag_categories",)
    inlines = [MonitorChatInline]
    change_form_template = "admin/analysis/analysistask/change_form.html"

    # мови, які реально трапляються в наших джерелах TeleZip
    LANG_CHOICES = [
        ("ru", "російська (ru)"), ("uk", "українська (uk)"),
        ("be", "білоруська (be)"), ("en", "англійська (en)"),
        ("kk", "казахська (kk)"), ("uz", "узбецька (uz)"),
        ("az", "азербайджанська (az)"), ("hy", "вірменська (hy)"),
        ("ka", "грузинська (ka)"), ("tg", "таджицька (tg)"),
    ]

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # «Мови» — мультиселект замість сирого JSON (зберігається як список)
        if db_field.name == "languages":
            return forms.MultipleChoiceField(
                choices=self.LANG_CHOICES, required=False,
                widget=forms.SelectMultiple(attrs={"size": 5, "style": "width:14em"}),
                label=db_field.verbose_name,
                help_text="Порожньо = без фільтра. Ctrl/⌘ — вибрати кілька.")
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    # спільні для обох конвеєрів розділи
    _FS_HEAD = (
        ("Задача", {
            "fields": ("name", "slug", "description", "pipeline", "is_active"),
        }),
        ("Спільне: категорії тегів", {
            "fields": ("tag_categories",),
        }),
    )
    # 📰 ПОШУК ПОДІЙ: етапи
    _FS_EVENTS = (
        ("📰 Етап 1 — Збір (TeleZip)", {
            "description": "Що і як тягнемо з пошуку TeleZip.",
            "fields": ("telezip_query", "telezip_unique", "search_posts",
                       "search_comments", "drop_linked_comments",
                       "min_channel_subscribers", "collect_chunk_days",
                       "languages"),
        }),
        ("📰 Етап 2 — Класифікація (LLM)", {
            "description": "Як LLM вирішує релевантність і витягує поля події.",
            "fields": ("classify_system_prompt", "llm_model", "geo_enabled"),
        }),
        ("📰 Етап 3 — Дедуплікація", {
            "classes": ("collapse",),
            "description": "N постів → 1 подія: пороги схожості та LLM-суддя.",
            "fields": ("dedup_window_days", "dedup_pre_thresh",
                       "dedup_cand_thresh", "dedup_judge_prompt", "generic_sides"),
        }),
        ("📰 Етап 4 — Авто-аудит: перший прохід (дешева LLM)", {
            "classes": ("collapse",),
            "description": "Воркер грубо відсіює хибнопозитиви дешевою моделлю "
                           "(gemini-flash тощо). Резонансність (охоплення/канали) "
                           "рахується автоматично.",
            "fields": ("review_enabled", "review_model", "review_prompt"),
        }),
        ("📰 Етап 5 — Агент-аудит (гібрид)", {
            "description": "Якісний ярус: запуск збору готує батчі approved-подій і стає "
                           "в «Чекає агента»; Claude-агенти виносять keep/reject + правки "
                           "(регіон, теги), ранер застосовує вердикти сам.",
            "fields": ("agent_review_prompt",),
        }),
    )
    # 💬 МОНІТОРИНГ КОМЕНТАРІВ: етапи (Етап 1 — збір + канали, канали переставляє JS)
    _FS_MONITOR = (
        ("💬 Етап 1 — Збір (TeleZip + канали)", {
            "classes": ("mon-collect-fs",),
            "description": "Збір коментарів із обраних Telegram-каналів (нижче). "
                           "Репости завжди згортаються (унікальні). "
                           "Запит '*' = усі повідомлення каналу за період.",
            "fields": ("telezip_query", "collect_chunk_days", "languages"),
        }),
        ("💬 Етап 2 — Фільтрація", {
            "description": "Дешевий відсів шуму без LLM (надто короткі/довгі повідомлення).",
            "fields": ("mon_min_len", "mon_max_len"),
        }),
        ("💬 Етап 3 — Прескрін (дешеве так/ні)", {
            "description": "OpenRouter-модель відсіює ~90-95% некритики перед агентами.",
            "fields": ("prescreen_model", "prescreen_prompt"),
        }),
        ("💬 Етап 4 — Тегування агентами", {
            "description": "Промпт, який отримують Claude-агенти в SYSTEM_PROMPT.md "
                           "батчів («чекає агента» на дашборді запусків).",
            "fields": ("tagger_prompt",),
        }),
    )

    def get_fieldsets(self, request, obj=None):
        # розділи залежать від конвеєра: спільне поле збору (telezip_query,
        # languages, collect_chunk_days) живе лише в «своєму» розділі — тож
        # жодного дублювання поля між fieldset-ами.
        pipeline = getattr(obj, "pipeline", None) or AnalysisTask.PIPELINE_EVENTS
        stages = (self._FS_MONITOR if pipeline == AnalysisTask.PIPELINE_MONITOR
                  else self._FS_EVENTS)
        return self._FS_HEAD + stages

    @admin.display(description="Чати моніт.")
    def monitor_chats_count(self, obj):
        n_total = obj.monitor_chats.count()
        n_active = obj.monitor_chats.filter(is_active=True).count()
        return f"{n_active}/{n_total}" if n_total else "—"


@admin.register(MonitorChat)
class MonitorChatAdmin(admin.ModelAdmin):
    """Standalone-сторінка чатів моніторингу (для bulk-операцій)."""
    list_display = ("task", "channel", "is_active", "is_critical_source",
                    "priority", "added_by", "created_at")
    list_filter = ("task", "is_active", "is_critical_source")
    search_fields = ("channel__username", "channel__title", "notes")
    autocomplete_fields = ("task", "channel")
    list_editable = ("is_active", "is_critical_source", "priority")


from rangefilter.filters import NumericRangeFilterBuilder as _NRFB
SubscribersRangeFilter = _NRFB(title="Підписники")


class ChannelTopicFilter(admin.SimpleListFilter):
    """Filter by a value inside the JSON `topics` list (Postgres @> contains)."""
    title = "Тема"
    parameter_name = "topic"
    TOPICS = ["новини", "локал-чат", "барахолка/оголошення", "авто/ДТП",
              "етнічне/міжнаціональне", "політика/опозиція", "влада/силовики",
              "релігія", "кримінал/ЧП", "знайомства/дозвілля", "інше"]

    def lookups(self, request, model_admin):
        return [(t, t) for t in self.TOPICS]

    def queryset(self, request, qs):
        return qs.filter(topics__contains=[self.value()]) if self.value() else qs


class ClassifiedFilter(admin.SimpleListFilter):
    """Has the directory classifier processed this channel yet?"""
    title = "Класифіковано (директорія)"
    parameter_name = "classified"

    def lookups(self, request, model_admin):
        return [("yes", "Так"), ("no", "Ні")]

    def queryset(self, request, qs):
        if self.value() == "yes":
            return qs.filter(directory_classified_at__isnull=False)
        if self.value() == "no":
            return qs.filter(directory_classified_at__isnull=True)
        return qs


class ChannelSubjectFilter(SubjectFilter):
    """Same select2 faceted RF-subject filter as EventAdmin, but faceted by CHANNEL
    counts (aggregated directly on the filtered set — no pk__in over 108k rows)."""

    def lookups(self, request, model_admin):
        return [(str(r.id), r.name) for r in
                Region.objects.filter(channels__isnull=False).distinct().order_by("name")]

    def choices(self, changelist):
        selected = self.request.GET.getlist(self.parameter_name)
        yield {
            "selected": len(selected) == 0,
            "query_string": changelist.get_query_string(remove=[self.parameter_name]),
            "display": _("All"),
            "value": "__all__",
        }
        base = facet_base(changelist, self.request, self)
        rows = (base.filter(region_subject__isnull=False)
                .values("region_subject__id", "region_subject__name")
                .annotate(n=Count("pk")).order_by())
        present = {str(r["region_subject__id"]): (r["region_subject__name"], r["n"])
                   for r in rows}
        for rid in selected:
            if rid not in present:
                r = Region.objects.filter(id=rid).first()
                if r:
                    present[rid] = (r.name, 0)
        for rid, (name, n) in sorted(present.items(), key=lambda kv: kv[1][0]):
            yield {"selected": rid in selected, "query_string": "",
                   "display": f"{name} ({n})", "value": rid}


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ("username", "title", "subscribers", "region_subject", "chat_type",
                    "discusses_problems", "topics_display", "directory_focus")
    list_filter = (ChannelSubjectFilter, "chat_type", "discusses_problems", ChannelTopicFilter,
                   ("subscribers", SubscribersRangeFilter), ClassifiedFilter,
                   "enriched", "is_channel", "language")
    search_fields = ("username", "title", "description")
    ordering = ("-subscribers",)
    list_select_related = ("region_subject",)
    list_per_page = 50
    show_full_result_count = False          # 108k rows — skip the slow full COUNT(*)
    autocomplete_fields = ("region_subject",)

    @admin.display(description="Теми")
    def topics_display(self, obj):
        return ", ".join(obj.topics) if obj.topics else "—"


class TagAliasInline(admin.TabularInline):
    model = TagAlias
    extra = 0


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "created_at")
    list_filter = ("category",)
    search_fields = ("name", "aliases__raw")
    inlines = [TagAliasInline]


# Numeric «from … to …» range filters for Event admin.
# Either bound may be left empty; the field is a plain DB column on Event.
from rangefilter.filters import NumericRangeFilterBuilder

ChannelCountFilter = NumericRangeFilterBuilder(title="К-сть каналів")
ReachFilter = NumericRangeFilterBuilder(title="Охоплення")


class InterEthnicFilter(admin.SimpleListFilter):
    """
    Slice events by how many distinct nationality tags they carry.

    Definitions:
      * inter  — ≥2 distinct tags with category='nationality' (the loose
        "inter-ethnic" heuristic — two ethnicities tagged on the same event)
      * mono   — exactly 1 nationality tag (intra-ethnic OR one side untagged)
      * audit  — event has tags in BOTH closed categories attacker_nationality
        and victim_nationality (hand-audited sides; strictest signal of
        inter-ethnic framing because sides are explicit, not co-occurrence)
      * none   — no nationality tag at all (review/enrich gap)
    """
    title = "Етнічність сторін"
    parameter_name = "ethnicity"

    def lookups(self, request, model_admin):
        return [
            ("inter", "Міжетнічні (≥2 нац. теги)"),
            ("mono",  "Моноетнічні (1 нац. тег)"),
            ("audit", "Аудит: сторони визначені"),
            ("none",  "Без нац. тегів"),
        ]

    def queryset(self, request, queryset):
        from django.db.models import Q
        v = self.value()
        if v == "inter":
            return (queryset
                    .annotate(_n_nat=Count("tags",
                                            filter=Q(tags__category="nationality"),
                                            distinct=True))
                    .filter(_n_nat__gte=2))
        if v == "mono":
            return (queryset
                    .annotate(_n_nat=Count("tags",
                                            filter=Q(tags__category="nationality"),
                                            distinct=True))
                    .filter(_n_nat=1))
        if v == "none":
            return (queryset
                    .annotate(_n_nat=Count("tags",
                                            filter=Q(tags__category="nationality"),
                                            distinct=True))
                    .filter(_n_nat=0))
        if v == "audit":
            # сторони живуть у звичайних tags під закритими категоріями
            # attacker_nationality / victim_nationality (окремі M2M-поля знято)
            return (queryset
                    .annotate(_n_att=Count("tags", distinct=True,
                                           filter=Q(tags__category="attacker_nationality")),
                              _n_vic=Count("tags", distinct=True,
                                           filter=Q(tags__category="victim_nationality")))
                    .filter(_n_att__gte=1, _n_vic__gte=1))
        return queryset


class RosMinorityClashFilter(admin.SimpleListFilter):
    """Events where росіянин is one side and ≥1 non-Russian nationality the other —
    the «росіяни ↔ нацменшини» slice the indicator matrix counts (task=1)."""
    title = "Сутички рос ↔ меншина"
    parameter_name = "ros_clash"
    NAT_CATS = ("attacker_nationality", "victim_nationality", "nationality")

    def lookups(self, request, model_admin):
        return [("1", "росіянин ↔ нацменшина")]

    def queryset(self, request, queryset):
        from django.db.models import Q
        if self.value() != "1":
            return queryset
        ros = list(Tag.objects.filter(category__in=self.NAT_CATS, name="росіянин")
                   .values_list("id", flat=True))
        # two conditional Counts over the SAME (unfiltered) tags join — filtering the
        # join with .filter(tags__in=ros) would collapse the other-nationality count to 0.
        return (queryset.annotate(
                    _has_ros=Count("tags", distinct=True, filter=Q(tags__id__in=ros)),
                    _other_nat=Count("tags", distinct=True,
                                     filter=Q(tags__category__in=self.NAT_CATS) & ~Q(tags__name="росіянин")))
                .filter(_has_ros__gte=1, _other_nat__gte=1))


class ReviewStatusDefaultFilter(admin.SimpleListFilter):
    """Статус аудиту з дефолтом «Схвалено»: без параметра список показує лише
    approved-події; «Всі» — явний вибір (?review_status=all)."""
    title = "Статус аудиту"
    parameter_name = "review_status"

    def lookups(self, request, model_admin):
        return [("all", "Всі")] + list(Event.REVIEW_CHOICES)

    def queryset(self, request, queryset):
        v = self.value() or Event.REVIEW_APPROVED   # дефолт = схвалено
        if v == "all":
            return queryset
        return queryset.filter(review_status=v)

    def choices(self, changelist):
        # без стокового «All» першим рядком: дефолтний вибір = «Схвалено»
        current = self.value() or Event.REVIEW_APPROVED
        for lookup, title in self.lookup_choices:
            yield {
                "selected": current == str(lookup),
                "query_string": changelist.get_query_string({self.parameter_name: lookup}),
                "display": title,
            }


class ReviewSourceFilter(admin.SimpleListFilter):
    """Хто виніс вердикт: агент-рев'ю / ручне Claude-рев'ю / відновлені / аудит."""
    title = "Джерело рев'ю"
    parameter_name = "review_source"

    def lookups(self, request, model_admin):
        return [
            ("agent", "Агент-рев'ю"),
            ("manual", "Ручне (Claude)"),
            ("restored", "Відновлені"),
            ("audit", "Аудит (CSV)"),
            ("none", "Без нотатки"),
        ]

    def queryset(self, request, queryset):
        v = self.value()
        if v == "agent":
            return queryset.filter(review_notes__icontains="agent review")
        if v == "manual":
            return queryset.filter(review_notes__icontains="manual Claude review")
        if v == "restored":
            return queryset.filter(review_notes__icontains="[restored]")
        if v == "audit":
            return queryset.filter(review_notes__icontains="audit")
        if v == "none":
            return queryset.filter(review_notes="")
        return queryset


class JobPeriodFilter(admin.SimpleListFilter):
    """Filter posts by a collect job — posts of that job's task within its period."""
    title = "Збір (job)"
    parameter_name = "job"

    def lookups(self, request, model_admin):
        return [(r.id, f"#{r.id} {r.task.slug} {r.date_from}…{r.date_to}")
                for r in ResearchRun.objects.select_related("task").order_by("-created_at")[:50]]

    def queryset(self, request, queryset):
        if self.value():
            r = ResearchRun.objects.filter(id=self.value()).first()
            if r:
                return queryset.filter(task=r.task,
                                       posted_at__date__gte=r.date_from,
                                       posted_at__date__lte=r.date_to)
        return queryset


class PrescreenFilter(admin.SimpleListFilter):
    title = "Prescreen"
    parameter_name = "prescreen"

    def lookups(self, request, model_admin):
        return [
            ("pos", "could_be_criticism = true"),
            ("neg", "could_be_criticism = false"),
            ("any", "будь-який вердикт"),
            ("none", "ще не пройшов"),
        ]

    def queryset(self, request, qs):
        v = self.value()
        if v == "pos":
            return qs.filter(classification___prescreen__could_be_criticism=True)
        if v == "neg":
            return qs.filter(classification___prescreen__could_be_criticism=False)
        if v == "any":
            return qs.filter(classification__has_key="_prescreen")
        if v == "none":
            return qs.exclude(classification__has_key="_prescreen")
        return qs


def _region_population_map():
    """{normalized inferred_region string -> (canonical_name, population)}.
    No LLM: matches via RegionAlias (raw) + direct Region name, both normalized.
    Used by Post charts to compute per-100k for comment subjects."""
    def norm(s):
        return (s or "").strip().lower().replace("республика", "").replace(
            "республіка", "").replace("ё", "е").strip()
    out = {}
    for r in Region.objects.exclude(population__isnull=True).values("name", "population"):
        out[norm(r["name"])] = (r["name"], r["population"])
    # aliases (raw RU text -> Region) override/extend
    for a in (RegionAlias.objects.filter(region__population__isnull=False)
              .select_related("region").values("raw", "region__name", "region__population")):
        out[norm(a["raw"])] = (a["region__name"], a["region__population"])
    return out, norm


class EstimatedCountPaginator(Paginator):
    """Pagination COUNT(*) over the multi-million-row Post table takes seconds.
    When the changelist is UNFILTERED, use Postgres' `reltuples` planner estimate
    (instant) instead of a real COUNT. Filtered queries still get an exact count —
    they're fast because the filtered columns (event_id, task, stage) are indexed."""

    @cached_property
    def count(self):
        qs = self.object_list
        try:
            has_filter = bool(qs.query.where)
        except Exception:
            has_filter = True
        if not has_filter:
            with connection.cursor() as cur:
                cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = %s",
                            [qs.model._meta.db_table])
                row = cur.fetchone()
            if row and row[0] and row[0] > 0:
                return int(row[0])
        return super().count


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    # Default model ordering is `posted_at` (unindexed) → full-sort of millions of
    # rows on every unfiltered changelist. Order by the PK (indexed) instead for an
    # instant default load; column headers still let you sort by posted_at on demand.
    ordering = ("-id",)
    paginator = EstimatedCountPaginator
    list_display = ("posted_at", "channel_name", "criticism_targets",
                    "topics", "opinions", "text_preview", "tg_link",
                    "is_relevant")
    list_filter = (
        "task", JobPeriodFilter,
        ("posted_at", ISODateRangeFilterBuilder()),
        "stage", "is_relevant",
        tag_category_filter("criticism_target", "Об'єкт критики"),
        tag_category_filter("topic", "Тема"),
        tag_category_filter("opinion", "Тип думки"),
        PrescreenFilter,
    )
    search_fields = ("url", "text")
    readonly_fields = ("stage_locked_at", "stage_attempts", "stage_error", "created_at")
    list_per_page = 100
    # The Post table is huge (300k+). Django's "X of N total" header runs an
    # unfiltered COUNT(*) — measured ~11.5s — on EVERY filtered changelist (e.g.
    # opening one event's posts). The filtered query itself is 9ms (event_id is
    # indexed), so the whole wait was that header count. Skip it.
    show_full_result_count = False
    change_list_template = "admin/analysis/post/change_list.html"

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related("channel")
                .prefetch_related("tags"))

    def lookup_allowed(self, lookup, value, *args, **kwargs):
        # Allow deep-links from charts/events: ?event__id__exact, region drill-down
        # (channel.region_subject), and the posted_at range (chart point -> posts).
        if lookup in ("event__id__exact", "event__id", "event",
                      "channel__region_subject__name__exact",
                      "channel__region_subject__name",
                      "channel__region_subject__id__exact",
                      "posted_at__range__gte", "posted_at__range__lte",
                      "posted_at__gte", "posted_at__lt", "posted_at__lte"):
            return True
        return super().lookup_allowed(lookup, value, *args, **kwargs)

    # ---------- charts -------------------------------------------------------
    _CHART_PARAMS = ("gran", "tag_top", "region_top", "_fragment", "_ts")

    def get_urls(self):
        custom = [
            path("charts/", self.admin_site.admin_view(self.charts_view),
                 name="analysis_post_charts"),
        ]
        return custom + super().get_urls()

    def charts_view(self, request):
        """Графіки критики: коментарі в часі по республіках (region_subject),
        % критики (÷ усі повідомлення з ChannelDailyStat), розподіл по об'єктах
        критики й темах. Гранулярність (день/тиждень/місяць) і вмикання/вимикання
        республік — на клієнті. Фільтри беруться з changelist."""
        from django.http import QueryDict
        is_fragment = request.GET.get("_fragment") == "1"  # до стрипу _CHART_PARAMS

        # apply changelist filters (strip our own params first)
        get_clean = QueryDict(request.GET.urlencode(), mutable=True)
        for own in self._CHART_PARAMS:
            get_clean.pop(own, None)
        request.GET = get_clean
        cl = self.get_changelist_instance(request)
        qs = cl.queryset

        from django.urls import reverse
        cl_base = reverse("admin:analysis_post_changelist")
        base_q = get_clean.urlencode()

        def link(extra):
            qd = QueryDict(base_q, mutable=True)
            for k, v in extra.items():
                qd.setlist(k, v if isinstance(v, (list, tuple)) else [str(v)])
            return f"{cl_base}?{qd.urlencode()}" if qd else cl_base

        # Республіка = денормалізований Post.region_subject (Ф2 unify: пряме поле
        # без JOIN, той самий контракт, що в подій). Лише МОНІТОРИНГ-критика
        # (events-пости мають свої графіки в EventAdmin). PostSource додає краї:
        # is_relevant=True + exclude(is_channel_repost) — авто-форвард каналу
        # більше не рахується як «думка людини».
        from .services.metrics import PostSource
        REL = PostSource(qs.filter(task__pipeline=AnalysisTask.PIPELINE_MONITOR)
                         .exclude(posted_at__isnull=True)).qs
        RKEY = "region_subject__name"

        def rname(v):
            return v or "(без субʼєкта)"

        # 1) КРИТИКА по (республіка, день) + 2) УСІ повідомлення (знаменник) із
        #    ChannelDailyStat у тому ж обсязі задач. Гранулярність робить клієнт.
        task_ids = list(REL.values_list("task_id", flat=True).distinct())
        # .order_by() ОБОВ'ЯЗКОВО: changelist несе ordering=(-id), яке інакше
        # підмішується у GROUP BY і вибухає (рядок на кожну пару пост-тег).
        crit_rows = (REL.order_by().annotate(d=TruncDate("posted_at"))
                     .values("d", RKEY).annotate(n=Count("id")))
        # знаменник «усіх повідомлень»: авторитетний TeleZip-лік (telezip_total)
        # де він є, інакше — старий total. ВАЖЛИВО: той самий діапазон дат, що й у
        # changelist (інакше знаменник тягне весь рік і вісь розтягується).
        def _pdate(s):
            for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                try:
                    return datetime.datetime.strptime(s, fmt).date()
                except (ValueError, TypeError):
                    continue
            return None
        d_gte = _pdate(get_clean.get("posted_at__range__gte"))
        d_lte = _pdate(get_clean.get("posted_at__range__lte"))
        tot_qs = ChannelDailyStat.objects.order_by().filter(task_id__in=task_ids)
        if d_gte:
            tot_qs = tot_qs.filter(date__gte=d_gte)
        if d_lte:
            tot_qs = tot_qs.filter(date__lte=d_lte)
        tot_rows = (tot_qs.values("date", "channel__region_subject__name")
                    .annotate(t=Sum(Coalesce("telezip_total", "total"))))

        daily = {}          # (reg, iso_day) -> [crit, total]
        reg_crit = {}       # reg -> сумарна критика (для сортування/відбору)
        for r in crit_rows:
            reg, d = rname(r[RKEY]), r["d"].isoformat()
            daily.setdefault((reg, d), [0, 0])[0] += r["n"]
            reg_crit[reg] = reg_crit.get(reg, 0) + r["n"]
        for r in tot_rows:
            reg, d = rname(r["channel__region_subject__name"]), r["date"].isoformat()
            daily.setdefault((reg, d), [0, 0])[1] += r["t"] or 0

        regions = sorted(reg_crit, key=lambda k: -reg_crit[k])
        daily_series = [{"r": reg, "d": d, "c": c, "t": t}
                        for (reg, d), (c, t) in daily.items() if reg in reg_crit]

        # 3) об'єкти критики × республіка; 4) теми × республіка.
        def by_cat_region(cat):
            rows = (REL.order_by().filter(tags__category=cat)
                    .values(RKEY, "tags__name", "tags__id")
                    .annotate(n=Count("id", distinct=True)))
            return [{"r": rname(x[RKEY]), "name": x["tags__name"], "n": x["n"],
                     "id": x["tags__id"]} for x in rows]

        targets = by_cat_region("criticism_target")
        topics = by_cat_region("topic")

        # 5+6) ті самі категорії, але в ЧАСІ (на кожен об'єкт/тему — лінія).
        def by_cat_region_day(cat):
            rows = (REL.order_by().filter(tags__category=cat)
                    .annotate(d=TruncDate("posted_at"))
                    .values("d", RKEY, "tags__name", "tags__id")
                    .annotate(n=Count("id", distinct=True)))
            return [{"d": x["d"].isoformat(), "r": rname(x[RKEY]),
                     "name": x["tags__name"], "n": x["n"], "id": x["tags__id"]} for x in rows]

        targets_day = by_cat_region_day("criticism_target")
        topics_day = by_cat_region_day("topic")

        ctx = {
            **self.admin_site.each_context(request),
            "title": "Графіки критики",
            "opts": self.model._meta,
            "total": REL.count(),
            "preserved_qs": base_q,
            "drill_cfg_json": json.dumps({"clBase": cl_base, "baseQ": base_q}, ensure_ascii=False),
            "regions_json": json.dumps(regions, ensure_ascii=False),
            "daily_json": json.dumps(daily_series, ensure_ascii=False),
            "targets_json": json.dumps(targets, ensure_ascii=False),
            "topics_json": json.dumps(topics, ensure_ascii=False),
            "targets_day_json": json.dumps(targets_day, ensure_ascii=False),
            "topics_day_json": json.dumps(topics_day, ensure_ascii=False),
        }
        # _fragment=1 → лише тіло (inline #charts); інакше повна сторінка.
        tmpl = ("admin/analysis/post/_charts_body.html" if is_fragment
                else "admin/analysis/post/charts.html")
        return render(request, tmpl, ctx)

    @admin.display(description="критика")
    def criticism_targets(self, obj):
        names = sorted({t.name for t in obj.tags.all() if t.category == "criticism_target"})
        return ", ".join(names) or "—"

    @admin.display(description="тема")
    def topics(self, obj):
        names = sorted({t.name for t in obj.tags.all() if t.category == "topic"})
        return ", ".join(names) or "—"

    @admin.display(description="думка")
    def opinions(self, obj):
        names = sorted({t.name for t in obj.tags.all() if t.category == "opinion"})
        return ", ".join(names) or "—"

    @admin.display(description="текст")
    def text_preview(self, obj):
        t = (obj.text or "").replace("\n", " ").strip()
        return (t[:160] + "…") if len(t) > 160 else t

    @admin.display(description="tg")
    def tg_link(self, obj):
        if not obj.url: return "—"
        return format_html('<a href="{}" target="_blank">→</a>', obj.url)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    change_form_template = "admin/analysis/event/change_form.html"
    list_display = ("event_date", "review_badge",
                    "region_subject", "settlement",
                    "tags_list", "count_short", "reach_display",
                    "posts_preview", "summary")
    readonly_fields = ("source_text", "posts_all", "review_status", "review_notes", "reviewed_at",
                       "review_locked_at")   # службовий claim-lock review-воркерів — лише читання
    date_hierarchy = "event_date"
    change_list_template = "admin/analysis/event/change_list.html"

    # ---------- charts -------------------------------------------------------

    def get_urls(self):
        custom = [
            path("charts/", self.admin_site.admin_view(self.charts_view),
                 name="analysis_event_charts"),
            path("conflicts/", self.admin_site.admin_view(self.conflicts_view),
                 name="analysis_event_conflicts"),
            path("matrix/", self.admin_site.admin_view(self.matrix_view),
                 name="analysis_event_matrix"),
            path("<int:event_id>/posts/", self.admin_site.admin_view(self.event_posts_view),
                 name="analysis_event_posts"),
        ]
        return custom + super().get_urls()

    def event_posts_view(self, request, event_id):
        """Lightweight page listing ALL posts of ONE event. The Post changelist is
        unusably slow here (heavy filter dropdowns + charts over the 300k-row table);
        this view touches only this event's posts, so it opens instantly."""
        from django.shortcuts import get_object_or_404
        from django.template.response import TemplateResponse
        ev = get_object_or_404(Event, pk=event_id)
        posts = sorted(
            ev.posts.select_related("channel").all(),
            key=lambda p: -(p.channel.subscribers if p.channel and p.channel.subscribers else 0),
        )
        rows = [{
            "posted_at": p.posted_at,
            "channel": (p.channel_name or (p.channel.title if p.channel else "") or "приватний"),
            "subs": (p.channel.subscribers if p.channel else None),
            "url": p.url,
            "text": (p.text or "").strip(),
        } for p in posts]
        ctx = {
            **self.admin_site.each_context(request),
            "title": f"Пости події #{ev.id}",
            "opts": self.model._meta,
            "event": ev,
            "rows": rows,
        }
        return TemplateResponse(request, "admin/analysis/event/event_posts.html", ctx)

    PRESET_HOTSPOT_REGIONS = [
        "Бурятія", "Саха (Якутія)", "Тива", "Татарстан",
        "Башкортостан", "Чечня", "Інгушетія", "Дагестан",
    ]

    # ---------- 8-republic indicator matrix (auto dashboard) ----------
    # Columns grouped into 3 sections in the SAME order as the Google Sheet. Each column:
    #   ("clash", label, None)        — task=1 росіянин↔меншина, 2025 / 2026
    #   ("event", code, tag_id)       — approved Event(task=6) count за 2026 (live, реагує на чистку)
    #   ("crit",  label, crit_key)    — частка від усіх критичних коментарів, 2025 / 2026
    MATRIX_SECTIONS = [
        ("Етнічні (ФУР)", [
            ("clash", "Сутички рос↔меншина", None),
            ("event", "C2", 2363), ("event", "C3", 2364), ("event", "C4", 2365),
        ]),
        ("Економічні (ГЕР)", [
            ("crit", "Критика фед. влади (економіка)", "econ"),
            ("event", "E1", 2366), ("event", "E2", 2367),
            ("event", "E3", 2368), ("event", "E4", 2369),
        ]),
        ("Політичні (ПОЛ)", [
            ("crit", "Критика фед. влади (упередж. до меншин)", "ethnic"),
            ("event", "P1", 2370), ("event", "P2", 2371), ("event", "P3", 2372),
            ("crit", "Негат. згадки фед.+місц. влади (політика)", "polit"),
        ]),
    ]
    MATRIX_CODE_LABEL = {
        "C2": "Відкриті протести проти етнічної дискримінації (мовної, релігійної, культурної)",
        "C3": "Активізація або ознаки активізації націоналістичних рухів",
        "C4": "Публічні висловлювання діаспор, культурні форуми та ініціативи підтримки етноспільнот",
        "E1": "Економічні суперечки між регіоном і федеральним урядом (субсидії, квоти, ресурси)",
        "E2": "Унікальні корупційні економічні скандали в республіці",
        "E3": "Протестна активність та економічні страйки",
        "E4": "Заяви місцевої адміністрації про економічну дискримінацію республіки",
        "P1": "Суперечки між регіональним і федеральним урядом щодо політичних рішень",
        "P2": "Протестна активність етнічних груп проти політичних рішень федеральної влади",
        "P3": "Протестна активність російських шовіністичних рухів проти місцевої влади",
    }
    # comment-based criticism columns (task=3 is_relevant, 2026): share of all criticism + Δpp
    MATRIX_FED_TARGETS = ['крит_путін', 'крит_кремль', 'крит_уряду', 'крит_думи', 'крит_МО',
                          'крит_ФСБ', 'крит_МВД', 'крит_росгвардії', 'крит_совбезу',
                          'крит_єдиної_росії', 'крит_рішень_центру_щодо_регіону']
    MATRIX_LOCAL_TARGETS = ['крит_глави_регіону', 'крит_рег_правит', 'крит_мера', 'крит_місц_депутата']
    MATRIX_CRIT_COLS = [
        # key, label, targets-attr, topics
        ("econ", "Критика фед. влади — економіка", "fed",
         ['тема_економіки', 'тема_корупції', 'тема_інфраструктури']),
        ("ethnic", "Критика фед. влади — упереджене ставлення до меншин", "fed",
         ['тема_етнічна']),
        ("polit", "Негативні згадки фед.+місц. влади (політика)", "fedlocal",
         ['тема_СВО', 'тема_мобілізації', 'тема_репресій']),
    ]
    MATRIX_NAT_CATS = ("attacker_nationality", "victim_nationality", "nationality")

    def matrix_view(self, request):
        """Auto-aggregated 8-republic ethnic-tension indicator matrix. All numbers
        come live from the DB: event columns from approved Event(task=6) by tag,
        criticism % from Post(task=3, is_relevant), clashes from task=1."""
        from django.template.response import TemplateResponse
        from .models import Event as Ev, Post as Po, AnalysisTask as Task, Tag, Region as Reg

        EV_TASK, MON_TASK, CLASH_SLUG = 6, 3, "ethnic-clashes"
        regions = self.PRESET_HOTSPOT_REGIONS
        id2code = {ref: lbl for _s, cols in self.MATRIX_SECTIONS
                   for ctype, lbl, ref in cols if ctype == "event"}
        all_codes = list(id2code.values())

        # ---- event columns: single count, all of 2026 ----
        ev_cells = {r: {c: 0 for c in all_codes} for r in regions}
        evq = (Ev.objects.filter(task_id=EV_TASK, review_status="approved",
                                 tags__id__in=list(id2code), region_subject__name__in=regions)
               .distinct().prefetch_related("tags"))
        for ev in evq:
            rname = ev.region_subject.name if ev.region_subject else None
            cell = ev_cells.get(rname)
            if cell is None:
                continue
            for tg in ev.tags.all():
                code = id2code.get(tg.id)
                if code:
                    cell[code] += 1

        # ---- comment-based criticism columns ----
        targets_map = {"fed": self.MATRIX_FED_TARGETS,
                       "fedlocal": self.MATRIX_FED_TARGETS + self.MATRIX_LOCAL_TARGETS}

        def crit(region, targets, topics, year):
            b = Po.objects.filter(is_relevant=True, posted_at__year=year, task_id=MON_TASK,
                                  channel__region_subject__name=region)
            # denominator = criticism WITH a topic tag (not all criticism) — removes the
            # year-over-year topic-tagging-coverage bias (2026 is ~46% tagged vs 2025 ~64%).
            denom = b.filter(tags__category="topic").distinct().count()
            num = (b.filter(tags__name__in=targets).filter(tags__name__in=topics).distinct().count())
            return denom, num

        monitored = set(Po.objects.filter(is_relevant=True, posted_at__year__in=[2025, 2026],
                                          task_id=MON_TASK)
                        .values_list("channel__region_subject__name", flat=True).distinct())
        crit_cells = {r: {} for r in regions}
        for r in regions:
            for key, _lbl, tattr, topics in self.MATRIX_CRIT_COLS:
                if r not in monitored:
                    crit_cells[r][key] = None
                    continue
                tgts = targets_map[tattr]
                d25, n25 = crit(r, tgts, topics, 2025)
                d26, n26 = crit(r, tgts, topics, 2026)
                crit_cells[r][key] = {
                    "y2025": (100.0 * n25 / d25) if d25 else None,
                    "y2026": (100.0 * n26 / d26) if d26 else None,
                }

        # ---- clash column (task=1 росіянин↔minority, by canonical region_subject) ----
        # Mirrors RosMinorityClashFilter so the cell number == the filtered changelist.
        from django.db.models import Q
        clash_cells = {r: [0, 0] for r in regions}   # [2025, 2026]
        try:
            ct = Task.objects.get(slug=CLASH_SLUG)
            ros_ids = list(Tag.objects.filter(category__in=self.MATRIX_NAT_CATS, name="росіянин")
                           .values_list("id", flat=True))

            def rosmin_count(region, year):
                return (Ev.objects.filter(task=ct, review_status="approved",
                                          region_subject__name=region, event_date__year=year)
                        .annotate(_h=Count("tags", distinct=True, filter=Q(tags__id__in=ros_ids)),
                                  _o=Count("tags", distinct=True,
                                           filter=Q(tags__category__in=self.MATRIX_NAT_CATS)
                                           & ~Q(tags__name="росіянин")))
                        .filter(_h__gte=1, _o__gte=1).count())
            for r in regions:
                clash_cells[r] = [rosmin_count(r, 2025), rosmin_count(r, 2026)]
        except Task.DoesNotExist:
            pass

        # ---- header descriptor + per-row cells, ordered exactly like the sheet ----
        sections = []
        for sidx, (sname, cols) in enumerate(self.MATRIX_SECTIONS):
            subcols = []
            for ctype, lbl, _ref in cols:
                if ctype == "event":
                    label, period = self.MATRIX_CODE_LABEL.get(lbl, lbl), "2026"
                elif ctype == "clash":
                    label, period = lbl, "2025 / 2026"
                else:  # crit — single 2026 number
                    label, period = lbl, "2026"
                subcols.append({"label": label, "period": period, "kind": ctype, "g": sidx})
            sections.append({"name": sname, "g": sidx, "span": len(cols), "subcols": subcols})

        # ---- direct changelist links (same convention as charts_view: reverse + QueryDict) ----
        from django.urls import reverse
        from django.http import QueryDict
        ev_cl = reverse("admin:analysis_event_changelist")
        po_cl = reverse("admin:analysis_post_changelist")
        reg_pk = {x.name: x.id for x in Reg.objects.filter(name__in=regions)}
        sec_cat = {0: "ethnic_event", 1: "econ_event", 2: "political_event"}
        cn = {t.name: t.id for t in Tag.objects.filter(category__in=["criticism_target", "topic"])}
        fed_ids = [cn[n] for n in self.MATRIX_FED_TARGETS if n in cn]
        local_ids = [cn[n] for n in self.MATRIX_LOCAL_TARGETS if n in cn]
        crit_topic_ids = {k: [cn[n] for n in topics if n in cn]
                          for k, _l, _t, topics in self.MATRIX_CRIT_COLS}
        crit_tattr = {k: tattr for k, _l, tattr, _top in self.MATRIX_CRIT_COLS}

        def mkurl(base, params):
            qd = QueryDict("", mutable=True)
            for k, v in params.items():
                if isinstance(v, (list, tuple)):
                    qd.setlist(k, [str(x) for x in v])
                else:
                    qd[k] = str(v)
            return f"{base}?{qd.urlencode()}"

        def ev_url(cat, tagid, region):
            p = {"tag_" + cat: [tagid], "review_status": "approved",
                 "event_date__year": 2026}
            if region in reg_pk:
                p["region_id"] = [reg_pk[region]]
            return mkurl(ev_cl, p)

        def clash_url(region, year):
            p = {"ros_clash": 1, "event_date__year": year, "review_status": "approved"}
            if region in reg_pk:
                p["region_id"] = [reg_pk[region]]
            return mkurl(ev_cl, p)

        def crit_url(region, key, year):
            p = {"task": 3, "is_relevant__exact": 1, "posted_at__year": year,
                 "tag_criticism_target": fed_ids + (local_ids if crit_tattr[key] == "fedlocal" else []),
                 "tag_topic": crit_topic_ids[key]}
            if region in reg_pk:
                p["channel__region_subject__id__exact"] = [reg_pk[region]]
            return mkurl(po_cl, p)

        rows = []
        for r in regions:
            cells = []
            for sidx, (_sname, cols) in enumerate(self.MATRIX_SECTIONS):
                for ctype, lbl, ref in cols:
                    if ctype == "clash":
                        a, b = clash_cells[r]
                        cells.append({"g": sidx, "kind": "clash", "a": a, "b": b,
                                      "zero": a == 0 and b == 0,
                                      "url_a": clash_url(r, 2025), "url_b": clash_url(r, 2026)})
                    elif ctype == "event":
                        n = ev_cells[r].get(lbl, 0)
                        cells.append({"g": sidx, "kind": "event", "n": n, "zero": n == 0,
                                      "url": ev_url(sec_cat[sidx], ref, r)})
                    else:  # crit
                        cells.append({"g": sidx, "kind": "crit", "c": crit_cells[r].get(ref),
                                      "url_a": crit_url(r, ref, 2025),
                                      "url_b": crit_url(r, ref, 2026)})
            rows.append({"region": r, "cells": cells})

        ctx = {
            **self.admin_site.each_context(request),
            "title": "Матриця етнічної напруги — 8 республік",
            "opts": self.model._meta,
            "rows": rows,
            "sections": sections,
            "code_label_items": list(self.MATRIX_CODE_LABEL.items()),
        }
        return TemplateResponse(request, "admin/analysis/event/matrix.html", ctx)

    def conflicts_view(self, request):
        """Inter-ethnic tension explorer. Builds a co-occurrence matrix of
        nationality tags within the filtered event set: cell (A, B) is the
        number of events where BOTH A and B appear (it's not a strict
        attacker→victim mapping — the schema doesn't have that — but a
        heatmap of which ethnicities show up together in incidents).

        Filters via GET params (so a URL can be shared / bookmarked as a
        saved query):
          ?region_id=1&region_id=2     — RF subjects (default: 8 hotspots)
          ?date_from=YYYY-MM-DD&date_to=...
          ?conflict=напад&conflict=бійка   — restrict to conflict tags
          ?include_intra=1             — include self-pairs (A↔A)
          ?min_count=N                 — hide cells below threshold
        """
        from django.db.models import Count
        from collections import defaultdict

        # ---- parse filters ----------------------------------------------------
        region_ids = [int(x) for x in request.GET.getlist("region_id") if x.isdigit()]
        date_from = request.GET.get("date_from") or ""
        date_to = request.GET.get("date_to") or ""
        conflict_filter = request.GET.getlist("conflict")
        include_intra = request.GET.get("include_intra") == "1"
        try:
            min_count = max(1, int(request.GET.get("min_count", 2)))
        except (TypeError, ValueError):
            min_count = 2

        # Default to the 8 hotspot regions if user hasn't picked any
        if not region_ids:
            hotspots = list(Region.objects.filter(
                name__in=self.PRESET_HOTSPOT_REGIONS).values_list("id", flat=True))
            region_ids = hotspots

        # ---- base queryset (events satisfying the filters) -------------------
        qs = Event.objects.filter(review_status=Event.REVIEW_APPROVED)
        if region_ids:
            qs = qs.filter(region_subject_id__in=region_ids)
        if date_from:
            qs = qs.filter(event_date__gte=date_from)
        if date_to:
            qs = qs.filter(event_date__lte=date_to)
        if conflict_filter:
            qs = qs.filter(tags__name__in=conflict_filter,
                           tags__category="conflict").distinct()
        # Per-tag exclusions (parallel to the changelist's include set).
        # URL form: ?tag_<cat>_excl=<id>… — drops events carrying any of these.
        for _cat_key in TagCategory.objects.values_list("key", flat=True):
            ids = [int(x) for x in request.GET.getlist(f"tag_{_cat_key}_excl")
                   if x.isdigit()]
            if ids:
                qs = qs.exclude(tags__id__in=ids)

        # ---- build the matrix -------------------------------------------------
        # event_id -> set(nationality_name)
        from analysis.models import Tag as _Tag
        nat_map = defaultdict(set)
        for et in (qs.values("id")
                     .annotate(nat_id=Count("tags__id"))
                     .values_list("id", flat=True)):
            pass        # noqa — touch qs to force load? no — direct iteration:

        rows = (qs.filter(tags__category="nationality")
                  .values_list("id", "tags__name"))
        for eid, nat in rows:
            if nat:
                nat_map[eid].add(nat)

        # symmetric co-occurrence + diagonal (intra-ethnic = single-nat events)
        matrix = defaultdict(int)         # (A, B) sorted -> count
        diagonal = defaultdict(int)
        nat_totals = defaultdict(int)     # total events mentioning A
        for eid, nats in nat_map.items():
            nats = list(nats)
            for n in nats:
                nat_totals[n] += 1
            if len(nats) == 1:
                diagonal[nats[0]] += 1
            else:
                for i, a in enumerate(nats):
                    for b in nats[i + 1:]:
                        key = tuple(sorted((a, b)))
                        matrix[key] += 1

        # union of all nations used; sort by total volume
        all_nats = sorted(nat_totals.keys(),
                          key=lambda n: (-nat_totals[n], n))

        # render only nations involved in at least one cell >= min_count
        visible = {n for n in all_nats
                   if (include_intra and diagonal[n] >= min_count)
                   or any((tuple(sorted((n, m))) in matrix
                           and matrix[tuple(sorted((n, m)))] >= min_count)
                          for m in all_nats if m != n)}
        nations = [n for n in all_nats if n in visible]

        # first pass — compute raw values and max
        max_cell = 1
        raw = {}
        for a in nations:
            for b in nations:
                if a == b:
                    v = diagonal[a] if include_intra else 0
                else:
                    v = matrix.get(tuple(sorted((a, b))), 0)
                raw[(a, b)] = v
                if v > max_cell:
                    max_cell = v

        # drill-down URLs per cell — open events list with the same filter + tags
        from django.urls import reverse
        from django.http import QueryDict
        cl_url = reverse("admin:analysis_event_changelist")
        tag_ids = {t.name: t.id for t in
                   _Tag.objects.filter(category="nationality", name__in=nations)}

        def cell_url(a, b):
            qd = QueryDict("", mutable=True)
            for rid in region_ids:
                qd.appendlist("region_id", str(rid))
            if date_from:
                qd["event_date__gte"] = date_from
            if date_to:
                qd["event_date__lte"] = date_to
            ids = [tag_ids[a]] if a == b else [tag_ids[a], tag_ids[b]]
            for tid in ids:
                qd.appendlist("tag_nationality", str(tid))
            return f"{cl_url}?{qd.urlencode()}"

        # second pass — assemble fully-rendered cells (template-friendly)
        grid = []
        for a in nations:
            row_cells = []
            for b in nations:
                v = raw[(a, b)]
                alpha = (v / max_cell) if max_cell and v else 0
                bg = (f"rgba(220,40,40,{alpha:.3f})" if v else "transparent")
                fg = "#fff" if alpha > 0.5 else "#000"
                row_cells.append({
                    "v": v, "url": cell_url(a, b),
                    "bg": bg, "fg": fg,
                    "title": f"{a} × {b}: {v} подій",
                })
            grid.append({"name": a, "total": nat_totals[a], "cells": row_cells})

        # filter form: list available regions + conflict tags for the dropdowns
        all_regions = list(Region.objects.order_by("name").values("id", "name"))
        all_conflicts = list(_Tag.objects.filter(category="conflict")
                               .order_by("name").values_list("name", flat=True))
        # what's currently selected (for re-rendering the form)
        sel_region_ids = set(region_ids)
        for r in all_regions:
            r["checked"] = r["id"] in sel_region_ids

        ctx = {
            **self.admin_site.each_context(request),
            "title": "Аналітика конфліктів — co-occurrence",
            "opts": self.model._meta,
            "has_view_permission": True,
            "events_total": qs.distinct().count(),
            "nations": nations,
            "grid": grid,
            "max_cell": max_cell,
            "include_intra": include_intra,
            "min_count": min_count,
            "all_regions": all_regions,
            "all_conflicts": all_conflicts,
            "selected_conflicts": conflict_filter,
            "date_from": date_from, "date_to": date_to,
        }
        return render(request, "admin/analysis/event/conflicts.html", ctx)

    # query params owned by the charts page (NOT changelist filter lookups).
    # `_ts` is a cache-buster added by the inline-charts JS to defeat any
    # browser disk cache when the template layout changes — strip it so
    # ChangeList doesn't try to resolve it as a model field.
    _CHART_PARAMS = ("gran", "tag_cats", "tag_top", "tag_chart", "tag_cols",
                     "region_top", "channel_top", "label_max",
                     "_fragment", "_ts")

    def charts_view(self, request):
        """Charts page — reuses the changelist filters so charts reflect EXACTLY what
        the user currently sees in /admin/analysis/event/. Own params:
            gran=day|week|month
            tag_cats=<cat_key>[,…]       (which TagCategories to render)
            tag_top=<int>                (top-N tags per category)
            tag_chart=bar|pie|doughnut   (per-category chart type)
            tag_cols=1|2|3               (per-category grid columns)
        """
        gran = request.GET.get("gran", "day")
        if gran not in ("day", "week", "month"):
            gran = "day"
        trunc = {"day": TruncDate, "week": TruncWeek, "month": TruncMonth}[gran]

        # tag-config params (with sane defaults)
        all_cats = list(TagCategory.objects.order_by("order", "key"))
        all_keys = [c.key for c in all_cats]
        raw_sel = request.GET.getlist("tag_cats")
        tag_cats_selected = [k for k in raw_sel if k in all_keys] or all_keys
        try: tag_top = max(3, min(100, int(request.GET.get("tag_top", 20))))
        except (TypeError, ValueError): tag_top = 20
        tag_chart = request.GET.get("tag_chart", "bar")
        if tag_chart not in ("bar", "pie", "doughnut"): tag_chart = "bar"
        try: tag_cols = int(request.GET.get("tag_cols", 2))
        except (TypeError, ValueError): tag_cols = 2
        if tag_cols not in (1, 2, 3): tag_cols = 2
        try: region_top = max(3, min(200, int(request.GET.get("region_top", 25))))
        except (TypeError, ValueError): region_top = 25
        try: channel_top = max(3, min(200, int(request.GET.get("channel_top", 25))))
        except (TypeError, ValueError): channel_top = 25
        # Max characters per displayed label (axis ticks / pie legend). Longer values
        # get ellipsised; tooltips still show the full text.
        try: label_max = max(8, min(120, int(request.GET.get("label_max", 36))))
        except (TypeError, ValueError): label_max = 36

        # Apply the same filtering as the changelist (strip our own params first,
        # else ChangeList tries to resolve them as field lookups)
        from django.http import QueryDict
        get_clean = QueryDict(request.GET.urlencode(), mutable=True)
        for own in self._CHART_PARAMS:
            get_clean.pop(own, None)
        request.GET = get_clean
        cl = self.get_changelist_instance(request)
        qs = cl.queryset

        # Build a URL for the events list with the SAME changelist filters as now,
        # plus an extra param (so clicks drill down).
        from django.urls import reverse
        from datetime import timedelta as _td
        cl_base_url = reverse("admin:analysis_event_changelist")
        # Index of the reach_display column in EventAdmin.list_display (1-based) — used
        # for `?o=-<N>` to open the drill-down already sorted by reach desc.
        _ld = self.get_list_display(request)
        try:
            reach_idx = list(_ld).index("reach_display") + 1
        except ValueError:
            reach_idx = 0
        def drill_url(extra_params):
            # `get_clean.lists()` here is the changelist-filter subset (chart params stripped)
            qd = QueryDict("", mutable=True)
            for k, vs in get_clean.lists():
                for v in vs:
                    qd.appendlist(k, v)
            # extra_params can replace OR append; for date-range we REPLACE existing
            for k, v in extra_params.items():
                if isinstance(v, (list, tuple)):
                    qd.setlist(k, [str(x) for x in v])
                else:
                    qd[k] = str(v)
            # default sort = reach desc (most-followed channels first), unless the caller
            # explicitly set their own ordering via `o=`
            if reach_idx and "o" not in qd:
                qd["o"] = f"-{reach_idx}"
            return f"{cl_base_url}?{qd.urlencode()}" if qd else cl_base_url

        def bucket_range(d):
            """For a TruncDate/Week/Month bucket start date, return (date_from, date_to)
            covering the events that fell into that bucket."""
            if d is None:
                return None, None
            if gran == "day":
                return d, d
            if gran == "week":
                return d, d + _td(days=6)
            # month — last day of month
            from calendar import monthrange
            return d, d.replace(day=monthrange(d.year, d.month)[1])

        # Per-row event "preview" lists were here, but they triggered ~1 query per
        # bucket/region/tag/channel → N+1 (1000+ queries on a 300-event period).
        # The drill-down link already takes the user to the full filtered list,
        # so a tiny tooltip preview wasn't worth the round-trip cost.
        def samples_for(_qs):
            return []

        # ---- time series ------------------------------------------------------
        # Агрегації йдуть через спільний адаптер (services/metrics.py): та сама
        # математика для подій і критики; view лишає собі drill-URL і шаблон.
        from .services.metrics import EventSource
        src = EventSource(qs, gran)
        ts_events = src.timeseries()   # {bucket, count, reach, posts}
        # unique publishing channels per bucket (through posts)
        ts_channels = list(
            Post.objects.filter(event__in=qs, channel__isnull=False,
                                event__event_date__isnull=False)
                .annotate(bucket=trunc("event__event_date"))
                .values("bucket")
                .annotate(channels=Count("channel", distinct=True))
                .order_by("bucket")
        )
        ch_by_bucket = {r["bucket"]: r["channels"] for r in ts_channels}
        ev_by_bucket = {r["bucket"]: r for r in ts_events}

        # Fill EVERY bucket across [min, max] so the x-axis is continuous: days/weeks/
        # months with zero events render as a real 0, instead of the line jumping the
        # gap (which reads as "no data here" rather than the truth — "no events here").
        def _next_bucket(d):
            if gran == "day":
                return d + _td(days=1)
            if gran == "week":
                return d + _td(days=7)
            from datetime import date as _date
            y, m = d.year, d.month
            return _date(y + (m // 12), (m % 12) + 1, 1)  # 1st of next month

        all_buckets = []
        if ts_events:
            cur, last = ts_events[0]["bucket"], ts_events[-1]["bucket"]
            while cur <= last:
                all_buckets.append(cur)
                cur = _next_bucket(cur)

        timeseries = []
        for bk in all_buckets:
            r = ev_by_bucket.get(bk)
            d_from, d_to = bucket_range(bk)
            url = ""
            if d_from is not None:
                # Use the rangefilter's canonical param names so the date inputs
                # in the sidebar pre-fill correctly after the click-through.
                url = drill_url({"event_date__range__gte": d_from.isoformat(),
                                 "event_date__range__lte": d_to.isoformat()})
            timeseries.append({
                "date": bk.isoformat(),
                "events": r["count"] if r else 0,
                "posts": int(r["posts"] or 0) if r else 0,
                "reach": int(r["reach"] or 0) if r else 0,
                "channels": ch_by_bucket.get(bk, 0),
                "url": url,
                "samples": [],
            })

        # ---- breakdowns -------------------------------------------------------
        # RF subjects (усі, не top-N): count + reach + per-100k — рахує спільний
        # адаптер (одна формула для подій і критики); view додає drill-URL.
        by_region = []
        for row in src.by_region():
            row["url"] = drill_url({"region_id": [row["id"]]})
            row["samples"] = samples_for(qs.filter(region_subject_id=row["id"]))
            by_region.append(row)

        # Republic-focused breakdown: time series PER region + per-100k normalised.
        # Use every region present in the FILTERED qs (so changelist filters such
        # as date/tag/etc. propagate). Only regions with a populated `population`
        # field can be shown — per-100k is meaningless otherwise.
        rep_ids_in_qs = list(
            qs.exclude(region_subject_id__isnull=True)
              .values_list("region_subject_id", flat=True)
              .distinct()
        )
        # Allow explicit override via ?republic_id=… ; otherwise take everything
        # in qs (still gated by `population IS NOT NULL` below).
        rep_ids_param = [int(x) for x in request.GET.getlist("republic_id") if x.isdigit()]
        rep_ids = rep_ids_param or rep_ids_in_qs
        # totals + серії по республіках — через спільний адаптер (per-100k та
        # distinct-краї всередині metrics.py; view додає лише drill-URL).
        rep_ts_rows = src.republic_timeseries(rep_ids)   # {bucket, region_subject_id, count}
        by_republic_total = src.republic_totals(rep_ids)
        for r in by_republic_total:
            r["url"] = drill_url({"region_id": [r["id"]]})
        republics = [{"id": r["id"], "name": r["name"], "population": r["population"]}
                     for r in by_republic_total]

        # republic timeseries: assemble {bucket → {name → count, name_per_100k → ...}}
        # Each (republic, bucket) cell also carries a drill-down URL combining
        # the region filter AND the bucket's date range — so clicking a point
        # on the line chart lands on EXACTLY those events.
        rep_by_id = {r["id"]: r for r in republics}
        buckets = sorted({row["bucket"] for row in rep_ts_rows if row["bucket"]})
        republic_timeseries = []
        for b in buckets:
            d_from, d_to = bucket_range(b)
            entry = {"date": b.isoformat()}
            for rid, rep in rep_by_id.items():
                cnt = next((row["count"] for row in rep_ts_rows
                            if row["bucket"] == b and row["region_subject_id"] == rid), 0)
                entry[rep["name"]] = cnt
                if rep["population"]:
                    entry[rep["name"] + "_per_100k"] = round(
                        100000.0 * cnt / rep["population"], 3)
                entry[rep["name"] + "_url"] = drill_url({
                    "region_id": [rid],
                    "event_date__range__gte": d_from.isoformat(),
                    "event_date__range__lte": d_to.isoformat(),
                }) if d_from else ""
            republic_timeseries.append(entry)

        by_tag = []   # list of {category, label, rows: [{id, name, count, url, samples}]}
        for c in all_cats:
            if c.key not in tag_cats_selected:
                continue
            raw = src.by_tag(c.key, tag_top)
            if not raw:
                continue
            rows = []
            for r in raw:
                rows.append({
                    **r,
                    "url": drill_url({f"tag_{c.key}": [r["id"]]}),
                    "samples": samples_for(qs.filter(tags__id=r["id"])),
                })
            by_tag.append({"category": c.key, "label": c.label, "rows": rows})

        # ---- універсальні зрізи (успадковані з пост-графіків критики) --------
        # тег × регіон (стек по республіках) і тег × час (лінія на тег).
        # Для критики: об'єкти/теми × республіки/час; для інцидентів: типи
        # конфліктів, етно-теги тощо. Рахує спільний адаптер.
        tag_region, tag_time = [], []
        for c in all_cats:
            if c.key not in tag_cats_selected:
                continue
            rows_r = src.tag_by_region(c.key, tag_top)
            if rows_r:
                for r in rows_r:
                    r["url"] = drill_url({f"tag_{c.key}": [r["id"]],
                                          "region_id": [r["region_id"]]})
                tag_region.append({"category": c.key, "label": c.label, "rows": rows_r})
            rows_t = src.tag_timeseries(c.key, tag_top)
            if rows_t:
                out_t = []
                for r in rows_t:
                    d_from, d_to = bucket_range(r["bucket"])
                    out_t.append({
                        "date": r["bucket"].isoformat(), "name": r["name"],
                        "count": r["count"],
                        "url": (drill_url({f"tag_{c.key}": [r["id"]],
                                           "event_date__range__gte": d_from.isoformat(),
                                           "event_date__range__lte": d_to.isoformat()})
                                if d_from else ""),
                    })
                tag_time.append({"category": c.key, "label": c.label, "rows": out_t})

        # ---- % подій від усіх повідомлень (знаменник — ChannelDailyStat) -----
        # Є лише для monitor-задач (критика): частка критичних коментарів серед
        # УСІХ повідомлень чатів. Для подієвих задач знаменника немає — блок
        # порожній і картка не рендериться.
        coverage = []
        cov_task_ids = list(qs.order_by().values_list("task_id", flat=True).distinct())
        if cov_task_ids:
            tot_qs = ChannelDailyStat.objects.order_by().filter(task_id__in=cov_task_ids)

            def _pd(s):
                for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                    try:
                        return datetime.datetime.strptime(s, fmt).date()
                    except (TypeError, ValueError):
                        continue
                return None
            d_gte = _pd(get_clean.get("event_date__range__gte") or get_clean.get("event_date__gte"))
            d_lte = _pd(get_clean.get("event_date__range__lte") or get_clean.get("event_date__lte"))
            if d_gte:
                tot_qs = tot_qs.filter(date__gte=d_gte)
            if d_lte:
                tot_qs = tot_qs.filter(date__lte=d_lte)
            tot_rows = list(tot_qs.values("date", "channel__region_subject__name")
                            .annotate(t=Sum(Coalesce("telezip_total", "total"))))
            if tot_rows:
                def _bstart(d):
                    if gran == "week":
                        return d - _td(days=d.weekday())
                    if gran == "month":
                        return d.replace(day=1)
                    return d
                den = {}
                for r in tot_rows:
                    key = (r["channel__region_subject__name"] or "", _bstart(r["date"]))
                    den[key] = den.get(key, 0) + (r["t"] or 0)
                num_rows = (src.qs.exclude(region_subject__isnull=True)
                            .exclude(**{f"{src.date_field}__isnull": True})
                            .annotate(bucket=src.trunc(src.date_field))
                            .values("bucket", "region_subject__name")
                            .annotate(n=Count("id", distinct=True)))
                for r in num_rows:
                    t = den.get((r["region_subject__name"], r["bucket"]))
                    if t:
                        coverage.append({"date": r["bucket"].isoformat(),
                                         "region": r["region_subject__name"],
                                         "pct": round(100.0 * r["n"] / t, 2),
                                         "n": r["n"], "t": t})
                coverage.sort(key=lambda x: (x["region"], x["date"]))

        # Distribution by reach buckets (vertical bar): how many events fall into
        # each reach band? Useful to see if data is dominated by viral big-reach or
        # by local low-reach incidents. Same idea for channel_count buckets.
        REACH_BUCKETS = [
            ("< 1k",        0,           999),
            ("1k–2k",       1_000,       1_999),
            ("2k–3k",       2_000,       2_999),
            ("3k–5k",       3_000,       4_999),
            ("5k–7k",       5_000,       6_999),
            ("7k–10k",      7_000,       9_999),
            ("10k–100k",    10_000,      99_999),
            ("100k–1M",     100_000,     999_999),
            ("1M+",         1_000_000,   10**12),
        ]
        CHAN_BUCKETS = [
            ("1",           1,    1),
            ("2–5",         2,    5),
            ("6–20",        6,    20),
            ("21–100",      21,   100),
            ("100+",        101,  10**9),
        ]
        # Buckets drill into the rangefilter widgets — use their canonical
        # `<field>__range__gte/lte` param names. Plain `reach_min`/`reach_max`
        # don't exist as model fields and were being kicked to `?e=1`.
        by_reach_bucket = []
        for label, lo, hi in REACH_BUCKETS:
            n = qs.filter(reach__gte=lo, reach__lte=hi).count()
            by_reach_bucket.append({
                "name": label, "count": n,
                "url": drill_url({"reach__range__gte": lo,
                                  "reach__range__lte": hi}),
                "samples": [],
            })
        by_channel_count_bucket = []
        for label, lo, hi in CHAN_BUCKETS:
            n = qs.filter(channel_count__gte=lo, channel_count__lte=hi).count()
            by_channel_count_bucket.append({
                "name": label, "count": n,
                "url": drill_url({"channel_count__range__gte": lo,
                                  "channel_count__range__lte": hi}),
                "samples": [],
            })

        by_channel_raw = list(
            Post.objects.filter(event__in=qs, channel__isnull=False)
                .values("channel_id", "channel__title",
                        "channel__username", "channel__subscribers")
                .annotate(count=Count("event", distinct=True))
                .order_by("-count")[:channel_top]
        )
        by_channel = []
        for r in by_channel_raw:
            cid = r["channel_id"]
            by_channel.append({
                "id": cid,
                "name": r["channel__title"],
                "uname": r["channel__username"],
                "subs": r["channel__subscribers"],
                "count": r["count"],
                "url": drill_url({"channel_id": [cid]}),
                "samples": samples_for(qs.filter(posts__channel_id=cid).distinct()),
            })

        # original GET (with all our params), used by templates for round-tripping
        from django.http import QueryDict
        orig = QueryDict(get_clean.urlencode(), mutable=True)
        # add our params back so preserved_qs / charts list URL are full
        if gran != "day": orig["gran"] = gran  # default elided
        for k in raw_sel: orig.appendlist("tag_cats", k)
        if tag_top != 20: orig["tag_top"] = str(tag_top)
        if tag_chart != "bar": orig["tag_chart"] = tag_chart
        if tag_cols != 2: orig["tag_cols"] = str(tag_cols)
        preserved_qs = orig.urlencode()
        # querystring stripped of `gran` — used by the day/week/month switch links
        qd = QueryDict(preserved_qs, mutable=True); qd.pop("gran", None)
        qs_no_gran = qd.urlencode()
        # passthrough for the tag-config form: every NON-tag/-tag-related param
        # (changelist filters etc.) so submitting the form keeps the current filter scope
        # Form passthrough — every NON-form-owned GET param (changelist filters +
        # other chart params), so submitting any inline form keeps the current scope.
        chart_state = {
            "gran": [gran],
            "tag_cats": list(tag_cats_selected),
            "tag_top": [str(tag_top)],
            "tag_chart": [tag_chart],
            "tag_cols": [str(tag_cols)],
            "region_top": [str(region_top)],
            "channel_top": [str(channel_top)],
            "label_max": [str(label_max)],
        }
        def passthrough_for(form_owns):
            # changelist filters + all chart params EXCEPT the ones the form itself owns
            return (list(get_clean.lists())
                    + [(k, v) for k, v in chart_state.items() if k not in form_owns])
        # tag-config form owns: tag_cats, tag_top, tag_chart, tag_cols
        passthrough_pairs = passthrough_for({"tag_cats", "tag_top", "tag_chart", "tag_cols"})
        regiontop_passthrough = passthrough_for({"region_top"})
        channeltop_passthrough = passthrough_for({"channel_top"})
        labelmax_passthrough = passthrough_for({"label_max"})

        # ---- filter panel: option lists + currently-selected values ---------
        all_tasks_list = list(AnalysisTask.objects.order_by("name").values("id", "name"))
        all_regions_list = list(Region.objects.order_by("name").values("id", "name"))
        # tags per (currently displayed) category — for the filter dropdowns.
        # Each category exposes two parallel multi-selects: include + exclude.
        tag_options = []  # [{key, label, tags, selected, selected_exclude}]
        for c in all_cats:
            if c.key not in tag_cats_selected:
                continue
            sel_inc = request.GET.getlist(f"tag_{c.key}")
            sel_exc = request.GET.getlist(f"tag_{c.key}_excl")
            tag_options.append({
                "key": c.key, "label": c.label,
                "tags": list(Tag.objects.filter(category=c.key)
                             .order_by("name").values("id", "name")),
                "selected": sel_inc,
                "selected_exclude": sel_exc,
            })
        # Filter form owns: every changelist-filter param. So passthrough = all chart
        # params only (gran, tag_*, region_top, channel_top, label_max).
        filter_form_passthrough = [(k, v) for k, v in chart_state.items()]
        # currently-selected values for the form fields
        selected_filters = {
            "date_from": (request.GET.get("event_date__range__gte")
                          or request.GET.get("event_date__gte", "")),
            "date_to": (request.GET.get("event_date__range__lte")
                        or request.GET.get("event_date__lte", "")),
            "task": request.GET.get("task", ""),
            "review_status": request.GET.get("review_status", "approved"),
            "regions": request.GET.getlist("region_id"),
        }
        any_filter_set = any([
            selected_filters["date_from"], selected_filters["date_to"],
            selected_filters["task"], selected_filters["review_status"],
            selected_filters["regions"],
            any(opt["selected"] for opt in tag_options),
            any(opt["selected_exclude"] for opt in tag_options),
        ])

        # Chart height scales with the number of regions. We render 4 bars
        # per region (events / reach / events-per-100k / reach-per-100k) so
        # each needs vertical room — ~130 px per region.
        by_region_height = max(280, 60 + 130 * len(by_region))

        ctx = {
            **self.admin_site.each_context(request),
            "title": "Графіки подій",
            "opts": self.model._meta,
            "has_view_permission": True,
            "events_total": qs.count(),
            "gran": gran,
            "qs_no_gran": qs_no_gran,
            "preserved_qs": preserved_qs,
            "tag_cats_all": all_cats,
            "tag_cats_selected": tag_cats_selected,
            "tag_top": tag_top,
            "tag_chart": tag_chart,
            "tag_cols": tag_cols,
            "region_top": region_top,
            "channel_top": channel_top,
            "label_max": label_max,
            "by_region_height": by_region_height,
            "passthrough_pairs": passthrough_pairs,
            "regiontop_passthrough": regiontop_passthrough,
            "channeltop_passthrough": channeltop_passthrough,
            "labelmax_passthrough": labelmax_passthrough,
            "all_tasks_list": all_tasks_list,
            "all_regions_list": all_regions_list,
            "tag_options": tag_options,
            "filter_form_passthrough": filter_form_passthrough,
            "selected_filters": selected_filters,
            "any_filter_set": any_filter_set,
            "review_status_choices": Event.REVIEW_CHOICES,
            "data": json.dumps({
                "timeseries": timeseries,
                "by_region": list(by_region),
                "by_tag": by_tag,
                "by_channel": list(by_channel),
                "by_reach_bucket": by_reach_bucket,
                "by_channel_count_bucket": by_channel_count_bucket,
                "republics": [r["name"] for r in republics],
                "republic_timeseries": republic_timeseries,
                "by_republic_total": by_republic_total,
                "tag_region": tag_region,
                "tag_time": tag_time,
                "coverage": coverage,
                "gran": gran,
                "tag_chart": tag_chart,
                "label_max": label_max,
            }, ensure_ascii=False, default=str),
        }
        # Tell the browser NOT to cache — charts are highly dynamic (filter
        # changes + template edits both invalidate). Without this, browsers
        # sometimes serve a stale fragment when only the layout (template)
        # changed but the URL stayed the same.
        # єдиний режим — фрагмент для вбудованого #charts на списку подій
        resp = render(request, "admin/analysis/event/charts_fragment.html", ctx)
        resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    def changelist_view(self, request, extra_context=None):
        # forward the current querystring to the "Графіки" link
        extra_context = dict(extra_context or {})
        extra_context["charts_qs"] = request.GET.urlencode()
        # Chart-page params (gran, tag_top, …) may end up in the changelist URL when
        # users navigate from the inline charts view (e.g. clicking week/month buttons).
        # Django admin would treat them as field lookups and reject with `?e=1`,
        # so strip them here BEFORE the changelist processes the request.
        needs_rewrite = (
            any(p in request.GET for p in self._CHART_PARAMS)
            # Legacy: bookmarks with tag_<cat>_mode=exclude — convert in place
            # so old links keep working. Same for spurious _mode keys we now ignore.
            or any(k.endswith("_mode") and k.startswith("tag_") for k in request.GET)
        )
        if needs_rewrite:
            from django.http import QueryDict
            clean = QueryDict(request.GET.urlencode(), mutable=True)
            for p in self._CHART_PARAMS:
                clean.pop(p, None)
            # Legacy `_mode=exclude` → migrate to the new `_excl` param family.
            for key in list(clean.keys()):
                if not (key.startswith("tag_") and key.endswith("_mode")):
                    continue
                base = key[:-5]                        # 'tag_status_mode' → 'tag_status'
                mode = clean.get(key, "")
                clean.pop(key, None)
                if mode == "exclude" and base in clean:
                    vals = clean.getlist(base)
                    clean.pop(base, None)             # was include; convert to exclude
                    clean.setlist(f"{base}_excl", vals)
            request.GET = clean
        return super().changelist_view(request, extra_context=extra_context)

    def lookup_allowed(self, lookup, value, *args, **kwargs):
        # back-compat: старі посилання/закладки з ?review_status__exact=… мають
        # працювати і після заміни стокового фільтра на ReviewStatusDefaultFilter
        if lookup in ("review_status__exact", "review_status"):
            return True
        return super().lookup_allowed(lookup, value, *args, **kwargs)

    def get_list_filter(self, request):
        # build one faceted multiselect per tag category, dynamically from the registry
        cat_filters = [tag_category_filter(c.key, c.label)
                       for c in TagCategory.objects.all()]
        return (
            ("event_date", ISODateRangeFilterBuilder(title="Період")),
            TaskSingleFilter,
            ReviewStatusDefaultFilter,
            ReviewSourceFilter,
            InterEthnicFilter,
            RosMinorityClashFilter,
            SubjectFilter,
            *cat_filters,
            ChannelFilter,
            ("channel_count", ChannelCountFilter),
            ("reach", ReachFilter),
        )

    def response_change(self, request, obj):
        from django.utils import timezone as djtz
        verdict = None
        if "_approve_event" in request.POST:
            verdict = ("approved", "✅ Подію схвалено")
        elif "_reject_event" in request.POST:
            verdict = ("rejected", "❌ Подію відхилено")
        if verdict:
            status, msg = verdict
            obj.review_status = status
            obj.reviewed_at = djtz.now()
            obj.review_locked_at = None
            note = f"[manual admin] {'схвалено' if status == 'approved' else 'відхилено'} вручну"
            obj.review_notes = (obj.review_notes + " | " + note) if obj.review_notes else note
            obj.save(update_fields=["review_status", "reviewed_at",
                                    "review_locked_at", "review_notes"])
            self.message_user(request, msg)
            from django.http import HttpResponseRedirect
            return HttpResponseRedirect(request.path)
        return super().response_change(request, obj)

    actions = ["approve_selected", "reject_selected", "copy_links"]

    @admin.action(description="🔗 Скопіювати посилання на вибрані події")
    def copy_links(self, request, queryset):
        from django.template.response import TemplateResponse
        links = [request.build_absolute_uri(f"/admin/analysis/event/{e.id}/change/")
                 for e in queryset.order_by("id")]
        return TemplateResponse(request, "admin/analysis/event/copy_links.html", {
            "links": links,
            "opts": self.model._meta,
            "title": "Посилання на події",
        })

    @admin.action(description="✅ Схвалити вибрані події")
    def approve_selected(self, request, queryset):
        from django.utils import timezone as djtz
        n = queryset.update(
            review_status=Event.REVIEW_APPROVED,
            review_notes="manual: approved by user",
            reviewed_at=djtz.now(),
            review_locked_at=None,
        )
        self.message_user(request, f"Схвалено: {n} подій.")

    @admin.action(description="🚫 Відхилити вибрані події")
    def reject_selected(self, request, queryset):
        from django.utils import timezone as djtz
        n = queryset.update(
            review_status=Event.REVIEW_REJECTED,
            review_notes="manual: rejected by user",
            reviewed_at=djtz.now(),
            review_locked_at=None,
        )
        self.message_user(request, f"Відхилено: {n} подій.")

    @admin.display(description="Аудит")
    def review_badge(self, obj):
        icon = {"approved": "✅", "pending": "⏳", "rejected": "🚫"}.get(obj.review_status, "•")
        return format_html('<span title="{}">{}</span>', obj.review_notes or "", icon)

    search_fields = ("summary", "region", "settlement", "review_notes")
    filter_horizontal = ("tags",)
    autocomplete_fields = ("region_subject", "tags", "task")

    class Media:
        # Load jQuery + Select2 in Django's order so `django.jQuery.fn.select2`
        # is defined on the changelist (the pso filter template needs it).
        css = {"screen": ["admin/css/vendor/select2/select2.css"]}
        js = [
            "admin/js/vendor/jquery/jquery.js",
            "admin/js/vendor/select2/select2.full.js",
            "admin/js/jquery.init.js",
        ]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("posts__channel", "tags")

    @admin.display(description="Тип конфлікту")
    def conflict_display(self, obj):
        names = [t.name for t in obj.tags.all() if t.category == "conflict"]
        return ", ".join(names) or "—"

    @admin.display(description="Теги")
    def tags_list(self, obj):
        # sides only — the conflict type has its own column
        return ", ".join(t.name for t in obj.tags.all() if t.category != "conflict")

    @admin.display(description="к-сть", ordering="post_count")
    def count_short(self, obj):
        return obj.post_count

    @admin.display(description="Охоплення", ordering="reach")
    def reach_display(self, obj):
        return f"{obj.reach:,}".replace(",", " ")

    @staticmethod
    def _subs(post):
        """Channel subscriber count for sorting (largest first)."""
        return (post.channel.subscribers or 0) if post.channel_id else 0

    @admin.display(description="Публікації")
    def posts_preview(self, obj):
        """Up to 3 post links (channel handle), sorted by subscribers (desc)."""
        ordered = sorted(
            obj.posts.all(),
            key=lambda p: (-self._subs(p), not p.channel_name),
        )
        posts = ordered[:3]
        if not posts:
            return "—"
        links = format_html_join(
            ", ",
            '<a href="{}" target="_blank" rel="noopener">{}</a>',
            ((p.url, self._handle(p)) for p in posts),
        )
        extra = obj.post_count - len(posts)
        return format_html("{}{}", links, format_html(" +{}", extra) if extra > 0 else "")

    @staticmethod
    def _handle(post):
        if post.channel_name:
            return f"@{post.channel_name}"
        if post.channel and post.channel.title:
            return post.channel.title[:24]
        return "приватний"

    @admin.display(description="Текст джерела (макс. аудиторія)")
    def source_text(self, obj):
        """Full TeleZip text of the event's post from the largest-audience channel."""
        posts = sorted(obj.posts.all(), key=lambda p: (-self._subs(p), not p.channel_name))
        if not posts:
            return "—"
        p = posts[0]
        subs = self._subs(p)
        subs_lbl = f"{subs:,}".replace(",", " ") if subs else "—"
        head = format_html(
            '<a href="{}" target="_blank" rel="noopener">{}</a> · 👥 {}',
            p.url, p.channel_name or "приватний", subs_lbl)
        body = (p.text or "").strip() or "(порожній текст)"
        return format_html(
            '<div>{}</div>'
            '<div style="white-space:pre-wrap;max-width:900px;margin-top:6px;'
            'padding:10px;background:var(--darkened-bg);color:var(--body-fg);'
            'border:1px solid var(--border-color);border-radius:6px">{}</div>',
            head, body)

    @admin.display(description="Усі публікації")
    def posts_all(self, obj):
        """All post links in the detail view, sorted by subscribers (desc)."""
        posts = sorted(obj.posts.all(), key=lambda p: (-self._subs(p), not p.channel_name))
        if not posts:
            return "—"

        def subs_label(p):
            n = self._subs(p)
            return f"{n:,}".replace(",", " ") if n else "—"

        return format_html_join(
            mark_safe("<br>"),
            '<a href="{}" target="_blank" rel="noopener">{}</a> — {} (👥 {})',
            ((p.url, p.url, p.channel_name or "приватний", subs_label(p)) for p in posts),
        )
