from django.contrib import admin
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from rangefilter.filters import DateRangeFilterBuilder

from .models import (
    AnalysisTask, Channel, Tag, TagAlias,
    Post, Event, ResearchRun,
    Region, RegionAlias,
)
from .multiselect_filter import (
    multiselect_filter, autocomplete_filter, create_multiselect_filter,
)

# --- reusable filter widgets (pso-style) -----------------------------------
TaskFilter = multiselect_filter(AnalysisTask, "Задача", "task", ordering="name")
RunFilter = multiselect_filter(
    ResearchRun, "Запуск", "run", ordering="-created_at", label_callback=str)


def tag_category_filter(category: str):
    """A multi-select checkbox filter listing only the tags of ONE category.
    Combining several (e.g. nationality + conflict) ANDs across categories,
    ORs within a category — the dynamic 'category -> its tags' structure."""
    label = dict(Tag.CATEGORY_CHOICES).get(category, category)
    return create_multiselect_filter(
        model=Tag,
        title=label,
        parameter_name=f"tag_{category}",
        filter_field="tags__id__in",
        queryset_callback=lambda qs, c=category: qs.filter(category=c),
        label_callback=lambda t: t.name,
        ordering="name",
    )


# one multiselect per tag category (dynamic structure)
TAG_CATEGORY_FILTERS = [tag_category_filter(cat) for cat, _label in Tag.CATEGORY_CHOICES]

SubjectFilter = autocomplete_filter(
    title="Суб'єкт РФ", parameter_name="region_id",
    filter_field="region_subject__id__in",
    selected_lookup=lambda req, ids: Region.objects.filter(id__in=ids),
    admin_autocomplete_field="region_subject", placeholder="Пошук суб'єкта…")

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
                    "events_total", "events_corroborated", "created_at")
    list_filter = ("task", "status")
    search_fields = ("title", "task__name")
    readonly_fields = ("started_at", "finished_at", "params", "stats",
                       "posts_collected", "posts_relevant", "events_total",
                       "events_corroborated", "created_at")


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
    list_display = ("url", "channel_name", "posted_at", "is_classified", "is_relevant", "event")
    list_filter = ("task", "is_classified", "is_relevant")
    search_fields = ("url", "text")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("event_date", "region_subject", "settlement", "conflict_display",
                    "tags_list", "count_short", "reach_display", "posts_preview", "summary")
    readonly_fields = ("posts_all",)
    date_hierarchy = "event_date"
    list_filter = (
        ("event_date", DateRangeFilterBuilder(title="Період")),   # період (from/to)
        TaskFilter,                        # за задачею
        RunFilter,                         # за запуском
        SubjectFilter,                     # за суб'єктом РФ
        *TAG_CATEGORY_FILTERS,             # мультиселект тегів по КОЖНІЙ категорії
        ChannelFilter,                     # за каналами
        "is_corroborated",
    )
    search_fields = ("summary", "region", "settlement")
    filter_horizontal = ("tags",)
    autocomplete_fields = ("region_subject", "tags", "run", "task")

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
