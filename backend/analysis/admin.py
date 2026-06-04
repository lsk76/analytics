from django.contrib import admin
from django.db.models import Count
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from rangefilter.filters import DateRangeFilterBuilder

from .models import (
    AnalysisTask, Channel, Tag, TagAlias,
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


def tag_category_filter(category: str):
    """Dynamic 'category -> its tags' faceted multiselect (one per category)."""
    label = dict(Tag.CATEGORY_CHOICES).get(category, category)
    return type(
        f"Tag{category.capitalize()}Filter",
        (TagCategoryMultiSelectFilter,),
        {"title": label, "parameter_name": f"tag_{category}", "category": category},
    )


# one multiselect per tag category (dynamic structure)
TAG_CATEGORY_FILTERS = [tag_category_filter(cat) for cat, _label in Tag.CATEGORY_CHOICES]

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


@admin.register(ResearchRun)
class ResearchRunAdmin(admin.ModelAdmin):
    list_display = ("__str__", "task", "date_from", "date_to", "status",
                    "chunk_progress", "posts_collected", "created_at")
    list_filter = ("task", "status")
    search_fields = ("title", "task__name")
    readonly_fields = ("started_at", "finished_at", "params", "stats",
                       "posts_collected", "posts_relevant", "events_total",
                       "events_corroborated", "created_at")

    @admin.display(description="Чанки")
    def chunk_progress(self, obj):
        total = obj.chunks.count()
        done = obj.chunks.filter(status="done").count()
        return f"{done}/{total}" if total else "—"


@admin.register(CollectChunk)
class CollectChunkAdmin(admin.ModelAdmin):
    list_display = ("task", "date_from", "date_to", "status", "attempts",
                    "posts_collected", "finished_at")
    list_filter = ("task", "status")
    date_hierarchy = "date_from"
    readonly_fields = ("locked_at", "attempts", "posts_collected", "error",
                       "created_at", "finished_at")


@admin.register(AnalysisTask)
class AnalysisTaskAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "date_from", "date_to", "is_active")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug")


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
    list_display = ("name", "category")
    list_filter = ("category",)
    search_fields = ("name", "aliases__raw")
    inlines = [TagAliasInline]


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("url", "channel_name", "posted_at", "stage", "is_relevant", "event")
    list_filter = ("task", "stage", "is_relevant")
    search_fields = ("url", "text")
    readonly_fields = ("stage_locked_at", "stage_attempts", "stage_error", "created_at")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("event_date", "region_subject", "settlement", "conflict_display",
                    "tags_list", "count_short", "reach_display", "posts_preview", "summary")
    readonly_fields = ("posts_all",)
    date_hierarchy = "event_date"
    list_filter = (
        ("event_date", DateRangeFilterBuilder(title="Період")),   # період (from/to)
        TaskFilter,                        # за задачею
        SubjectFilter,                     # за суб'єктом РФ
        *TAG_CATEGORY_FILTERS,             # мультиселект тегів по КОЖНІЙ категорії
        ChannelFilter,                     # за каналами
        "is_corroborated",
    )
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
