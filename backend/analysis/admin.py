import json

from django.contrib import admin, messages
from django.db.models import Count, Sum, F
from django.db.models.functions import TruncDate, TruncWeek, TruncMonth
from django.shortcuts import render
from django.urls import path
from django.utils import timezone as djtz
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from rangefilter.filters import DateRangeFilterBuilder

from .services import stages

from .models import (
    AnalysisTask, Channel, Tag, TagAlias, TagCategory,
    Post, Event, ResearchRun, CollectChunk,
    Region, RegionAlias, MonitorChat,
)
from .multiselect_filter import (
    multiselect_filter, autocomplete_filter, MultiSelectFilter,
)

# --- reusable filter widgets (pso-style) -----------------------------------
TaskFilter = multiselect_filter(AnalysisTask, "Задача", "task", ordering="name")


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
        rows = (Event.objects.filter(pk__in=ids, tags__category=self.category)
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
            pcounts = dict(Post.objects.filter(task=j.task,
                                                posted_at__date__gte=j.date_from,
                                                posted_at__date__lte=j.date_to)
                           .values_list("stage").annotate(n=Count("id")))
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
                "post_stages": [(lbl, pcounts.get(st, 0))
                                for st, lbl in self._STAGE_SEQ if pcounts.get(st)],
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

    # пайплайн-стадії в порядку проходження (термінальні: done/failed)
    _STAGE_SEQ = [
        (Post.STAGE_COLLECTED, "збір"),
        (Post.STAGE_ENRICHED, "збагач."),
        (Post.STAGE_PRECLUSTERED, "преклас."),
        (Post.STAGE_CLASSIFIED, "класиф."),
        (Post.STAGE_DEDUPED, "дедуп"),
        (Post.STAGE_DONE, "готово"),
    ]

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


@admin.action(description="▶ Зібрати за період задачі")
def collect_task_period_action(modeladmin, request, queryset):
    for task in queryset:
        run = ResearchRun.objects.create(
            task=task, title=f"admin {djtz.now():%Y-%m-%d %H:%M}",
            date_from=task.date_from, date_to=task.date_to,
            chunk_days=task.collect_chunk_days or 3, status="collecting")
        n = stages.enqueue_collection(task, task.date_from, task.date_to,
                                      chunk_days=run.chunk_days, job=run)
        messages.success(
            request,
            f"«{task.name}» {task.date_from}…{task.date_to}: job #{run.id}, +{n} чанків. "
            f"Стеж за прогресом у «Збори (jobs)» та лічильниках стадій постів.")


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
    list_display = ("name", "slug", "date_from", "date_to", "geo_enabled",
                    "is_active", "monitor_chats_count")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug")
    filter_horizontal = ("tag_categories",)
    actions = [collect_task_period_action]
    inlines = [MonitorChatInline]

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


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ("username", "title", "subscribers", "inferred_region", "is_channel", "enriched")
    search_fields = ("username", "title")
    list_filter = ("is_channel", "enriched")


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
      * audit  — both `attacker_tags` and `victim_tags` non-empty (only the
        ~hundred events we hand-audited; strictest signal of inter-ethnic
        framing because sides are explicit, not co-occurrence)
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
            return (queryset
                    .annotate(_n_att=Count("attacker_tags", distinct=True),
                              _n_vic=Count("victim_tags", distinct=True))
                    .filter(_n_att__gte=1, _n_vic__gte=1))
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


class TagCategoryFilter(admin.SimpleListFilter):
    title = "Категорія тегів"
    parameter_name = "tag_cat"

    def lookups(self, request, model_admin):
        return [
            ("criticism_target", "має об'єкт критики"),
            ("topic", "має тему"),
            ("opinion", "має тип думки"),
            ("any", "має будь-який тег"),
            ("none", "без тегів"),
        ]

    def queryset(self, request, qs):
        v = self.value()
        if not v: return qs
        if v == "none":
            return qs.filter(tags__isnull=True).distinct()
        if v == "any":
            return qs.filter(tags__isnull=False).distinct()
        return qs.filter(tags__category=v).distinct()


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


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("posted_at", "channel_name", "criticism_targets",
                    "topics", "opinions", "text_preview", "tg_link",
                    "is_relevant")
    list_filter = ("task", JobPeriodFilter, "stage", "is_relevant",
                   TagCategoryFilter, PrescreenFilter)
    search_fields = ("url", "text")
    readonly_fields = ("stage_locked_at", "stage_attempts", "stage_error", "created_at")
    list_per_page = 100

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related("channel")
                .prefetch_related("tags"))

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
    list_display = ("event_date", "review_badge", "bot_farm_badge",
                    "region_subject", "settlement",
                    "conflict_display", "tags_list", "count_short", "reach_display",
                    "posts_preview", "summary")
    readonly_fields = ("source_text", "posts_all", "review_status", "review_notes", "reviewed_at")
    date_hierarchy = "event_date"
    change_list_template = "admin/analysis/event/change_list.html"

    # ---------- charts -------------------------------------------------------

    def get_urls(self):
        custom = [
            path("charts/", self.admin_site.admin_view(self.charts_view),
                 name="analysis_event_charts"),
            path("conflicts/", self.admin_site.admin_view(self.conflicts_view),
                 name="analysis_event_conflicts"),
        ]
        return custom + super().get_urls()

    PRESET_HOTSPOT_REGIONS = [
        "Бурятія", "Саха (Якутія)", "Тива", "Татарстан",
        "Башкортостан", "Чечня", "Інгушетія", "Дагестан",
    ]

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
        is_fragment = request.GET.get("_fragment") == "1"
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
        # events + total reach per bucket
        ts_events = list(
            qs.exclude(event_date__isnull=True)
              .annotate(bucket=trunc("event_date"))
              .values("bucket")
              .annotate(events=Count("id"), reach=Sum("reach"))
              .order_by("bucket")
        )
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
        timeseries = []
        for r in ts_events:
            d_from, d_to = bucket_range(r["bucket"])
            url, samples = "", []
            if d_from is not None:
                # Use the rangefilter's canonical param names so the date inputs
                # in the sidebar pre-fill correctly after the click-through.
                url = drill_url({"event_date__range__gte": d_from.isoformat(),
                                 "event_date__range__lte": d_to.isoformat()})
                samples = samples_for(qs.filter(event_date__gte=d_from,
                                                event_date__lte=d_to))
            timeseries.append({
                "date": r["bucket"].isoformat() if r["bucket"] else None,
                "events": r["events"],
                "reach": int(r["reach"] or 0),
                "channels": ch_by_bucket.get(r["bucket"], 0),
                "url": url,
                "samples": samples,
            })

        # ---- breakdowns -------------------------------------------------------
        # RF subjects (ALL with events, not just top-N): events count +
        # summed reach + per-100k normalised by Region.population. The old
        # standalone per-100k bar chart was folded into this one, so the
        # single chart now carries all three metrics side-by-side.
        # `.order_by()` clears the changelist's default sort so it doesn't
        # leak into GROUP BY (otherwise we'd get 1 row per event with
        # count=1 — same bug we hit on rep_totals_map).
        by_region_raw = list(
            qs.exclude(region_subject__isnull=True)
              .order_by()
              .values("region_subject_id", "region_subject__name")
              .annotate(count=Count("id", distinct=True),
                        reach=Sum("reach"))
              .order_by("-count")
        )
        pops = dict(
            Region.objects.filter(id__in=[r["region_subject_id"] for r in by_region_raw],
                                   population__isnull=False)
                          .values_list("id", "population")
        )
        by_region = []
        for r in by_region_raw:
            rid = r.pop("region_subject_id")
            name = r.pop("region_subject__name")
            pop = pops.get(rid)
            reach_v = int(r["reach"] or 0)
            per_100k = round(100000.0 * r["count"] / pop, 2) if pop else 0
            # Reach normalised by population — comparable across regions of
            # very different sizes (e.g. Buryatia 970k vs Moscow 13M).
            reach_per_100k = int(100000.0 * reach_v / pop) if pop else 0
            row = {
                "id": rid, "name": name,
                "count": r["count"],
                "reach": reach_v,
                "per_100k": per_100k,
                "reach_per_100k": reach_per_100k,
                "population": pop,
            }
            row["url"] = drill_url({"region_id": [rid]})
            row["samples"] = samples_for(qs.filter(region_subject_id=rid))
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
        republics = list(Region.objects.filter(id__in=rep_ids, population__isnull=False)
                         .order_by("name").values("id", "name", "population"))
        # ts: (bucket, region_id) → count (distinct for the same JOIN reason)
        rep_ts_rows = list(
            qs.filter(region_subject_id__in=rep_ids)
              .exclude(event_date__isnull=True)
              .annotate(bucket=trunc("event_date"))
              .values("bucket", "region_subject_id")
              .annotate(events=Count("id", distinct=True))
              .order_by("bucket")
        )
        # totals per republic (events overall + per-100k). distinct=True because
        # `qs` may already have JOINs from changelist tag filters; without it,
        # M2M tag rows multiply each event into its number of tags. `.order_by()`
        # is critical: the changelist passes its own ordering (event_date, id)
        # which Django silently appends to GROUP BY → one row per event with
        # COUNT=1. Clearing ordering makes the GROUP BY honour only our keys.
        rep_totals_map = {}
        for row in (qs.filter(region_subject_id__in=rep_ids)
                      .order_by()
                      .values("region_subject_id")
                      .annotate(events=Count("id", distinct=True))):
            rep_totals_map[row["region_subject_id"]] = row["events"]

        by_republic_total = []
        for r in republics:
            total = rep_totals_map.get(r["id"], 0)
            per_100k = (100000.0 * total / r["population"]) if r["population"] else 0
            by_republic_total.append({
                "id": r["id"], "name": r["name"], "population": r["population"],
                "count": total, "per_100k": round(per_100k, 2),
                "url": drill_url({"region_id": [r["id"]]}),
            })

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
                cnt = next((row["events"] for row in rep_ts_rows
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
            raw = list(
                qs.filter(tags__category=c.key)
                  .values("tags__id", "tags__name")
                  .annotate(count=Count("id", distinct=True))
                  .order_by("-count")[:tag_top]
            )
            if not raw:
                continue
            rows = []
            for r in raw:
                tid = r["tags__id"]; name = r["tags__name"]
                rows.append({
                    "id": tid, "name": name, "count": r["count"],
                    "url": drill_url({f"tag_{c.key}": [tid]}),
                    "samples": samples_for(qs.filter(tags__id=tid)),
                })
            by_tag.append({"category": c.key, "label": c.label, "rows": rows})

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
            "review_status": request.GET.get("review_status", ""),
            "is_corroborated": request.GET.get("is_corroborated__exact", ""),
            "regions": request.GET.getlist("region_id"),
        }
        any_filter_set = any([
            selected_filters["date_from"], selected_filters["date_to"],
            selected_filters["task"], selected_filters["review_status"],
            selected_filters["is_corroborated"], selected_filters["regions"],
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
                "gran": gran,
                "tag_chart": tag_chart,
                "label_max": label_max,
            }, ensure_ascii=False, default=str),
        }
        # Tell the browser NOT to cache — charts are highly dynamic (filter
        # changes + template edits both invalidate). Without this, browsers
        # sometimes serve a stale fragment when only the layout (template)
        # changed but the URL stayed the same.
        tmpl = ("admin/analysis/event/charts_fragment.html" if is_fragment
                else "admin/analysis/event/charts.html")
        resp = render(request, tmpl, ctx)
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

    def get_list_filter(self, request):
        # build one faceted multiselect per tag category, dynamically from the registry
        cat_filters = [tag_category_filter(c.key, c.label)
                       for c in TagCategory.objects.all()]
        return (
            ("event_date", DateRangeFilterBuilder(title="Період")),
            TaskFilter,
            "review_status",
            InterEthnicFilter,
            SubjectFilter,
            *cat_filters,
            ChannelFilter,
            ("channel_count", ChannelCountFilter),
            ("reach", ReachFilter),
            "is_corroborated",
            "is_bot_farm",
        )

    actions = ["approve_selected", "reject_selected"]

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

    @admin.display(description="🤖", ordering="-bot_farm_score")
    def bot_farm_badge(self, obj):
        if not obj.is_bot_farm:
            return ""
        return format_html('<span title="bot-farm score: {}">🤖</span>',
                           f"{obj.bot_farm_score:.2f}")

    search_fields = ("summary", "region", "settlement")
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

    @admin.display(description="Сторони/теги")
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
