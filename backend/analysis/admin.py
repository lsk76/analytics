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
    Region, RegionAlias,
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
    Faceted multi-select for tags of ONE category:
      * shows only tags PRESENT in the current selection (other filters applied);
      * each option carries the number of events that would remain.
    Within a category the selected tags are OR-ed; across categories — AND-ed.
    """
    category = None

    def filter_queryset(self, queryset, values):
        return queryset.filter(tags__id__in=values)

    def lookups(self, request, model_admin):  # fallback; choices() does the real work
        return [(str(t.id), t.name)
                for t in Tag.objects.filter(category=self.category).order_by("name")]

    def _facet_base(self, changelist):
        return facet_base(changelist, self.request, self)

    def choices(self, changelist):
        selected = self.request.GET.getlist(self.parameter_name)
        yield {
            "selected": len(selected) == 0,
            "query_string": changelist.get_query_string(remove=[self.parameter_name]),
            "display": _("All"),
            "value": "__all__",
        }
        base = self._facet_base(changelist)
        # materialize event ids first — base may carry .distinct()/M2M joins that
        # corrupt a grouped annotate; count on a clean queryset instead
        ids = list(base.values_list("pk", flat=True).distinct())
        rows = (Event.objects.filter(pk__in=ids, tags__category=self.category)
                .values("tags__id", "tags__name")
                .annotate(n=Count("pk", distinct=True)))
        present = {str(r["tags__id"]): (r["tags__name"], r["n"]) for r in rows}
        # keep currently-selected tags visible even if they yield 0 now
        for tid in selected:
            if tid not in present:
                t = Tag.objects.filter(id=tid).first()
                if t:
                    present[tid] = (t.name, 0)
        for tid, (name, n) in sorted(present.items(), key=lambda kv: kv[1][0]):
            yield {
                "selected": tid in selected,
                "query_string": "",
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
        done = obj.chunks.filter(status="done").count()
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


@admin.register(AnalysisTask)
class AnalysisTaskAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "date_from", "date_to", "geo_enabled", "is_active")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug")
    filter_horizontal = ("tag_categories",)
    actions = [collect_task_period_action]


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


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("url", "channel_name", "posted_at", "stage", "is_relevant", "event")
    list_filter = ("task", JobPeriodFilter, "stage", "is_relevant")
    search_fields = ("url", "text")
    readonly_fields = ("stage_locked_at", "stage_attempts", "stage_error", "created_at")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("event_date", "review_badge", "region_subject", "settlement",
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
        ]
        return custom + super().get_urls()

    # query params owned by the charts page (NOT changelist filter lookups)
    _CHART_PARAMS = ("gran", "tag_cats", "tag_top", "tag_chart", "tag_cols",
                     "region_top", "channel_top", "label_max")

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

        def samples_for(qs_):
            """Few representative events for tooltips."""
            return [
                {"date": e.event_date.isoformat() if e.event_date else "",
                 "summary": (e.summary or "")[:90]}
                for e in qs_.order_by("-reach", "id")[:5]
            ]

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
                url = drill_url({"event_date__gte": d_from.isoformat(),
                                  "event_date__lte": d_to.isoformat()})
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
        # top RF subjects — include id + URL + samples
        by_region_raw = list(
            qs.exclude(region_subject__isnull=True)
              .values("region_subject_id", "region_subject__name")
              .annotate(count=Count("id"))
              .order_by("-count")[:region_top]
        )
        by_region = []
        for r in by_region_raw:
            rid = r.pop("region_subject_id")
            name = r.pop("region_subject__name")
            row = {"id": rid, "name": name, "count": r["count"]}
            row["url"] = drill_url({"region_id": [rid]})
            row["samples"] = samples_for(qs.filter(region_subject_id=rid))
            by_region.append(row)

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
            "passthrough_pairs": passthrough_pairs,
            "regiontop_passthrough": regiontop_passthrough,
            "channeltop_passthrough": channeltop_passthrough,
            "labelmax_passthrough": labelmax_passthrough,
            "data": json.dumps({
                "timeseries": timeseries,
                "by_region": list(by_region),
                "by_tag": by_tag,
                "by_channel": list(by_channel),
                "gran": gran,
                "tag_chart": tag_chart,
                "label_max": label_max,
            }, ensure_ascii=False, default=str),
        }
        return render(request, "admin/analysis/event/charts.html", ctx)

    def changelist_view(self, request, extra_context=None):
        # forward the current querystring to the "Графіки" link
        extra_context = dict(extra_context or {})
        extra_context["charts_qs"] = request.GET.urlencode()
        return super().changelist_view(request, extra_context=extra_context)

    def get_list_filter(self, request):
        # build one faceted multiselect per tag category, dynamically from the registry
        cat_filters = [tag_category_filter(c.key, c.label)
                       for c in TagCategory.objects.all()]
        return (
            ("event_date", DateRangeFilterBuilder(title="Період")),
            TaskFilter,
            "review_status",
            SubjectFilter,
            *cat_filters,
            ChannelFilter,
            "is_corroborated",
        )

    @admin.display(description="Аудит")
    def review_badge(self, obj):
        icon = {"approved": "✅", "pending": "⏳", "rejected": "🚫"}.get(obj.review_status, "•")
        return format_html('<span title="{}">{}</span>', obj.review_notes or "", icon)

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
