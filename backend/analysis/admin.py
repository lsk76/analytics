from django.contrib import admin
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from rangefilter.filters import DateRangeFilterBuilder

from .models import (
    AnalysisTask, Channel, Nationality, NationalityAlias,
    ConflictType, ConflictTypeAlias, Post, Event, ResearchRun,
    Region, RegionAlias,
)
from .multiselect_filter import multiselect_filter, autocomplete_filter

# --- reusable filter widgets (pso-style) -----------------------------------
TaskFilter = multiselect_filter(AnalysisTask, "Задача", "task", ordering="name")
RunFilter = multiselect_filter(
    ResearchRun, "Запуск", "run", ordering="-created_at", label_callback=str)
TypeFilter = multiselect_filter(ConflictType, "Тип конфлікту", "conflict_type", ordering="name")

SidesFilter = autocomplete_filter(
    title="Сторони (нації)", parameter_name="side_id",
    filter_field="sides__id__in",
    selected_lookup=lambda req, ids: Nationality.objects.filter(id__in=ids),
    admin_autocomplete_field="sides", placeholder="Пошук нації…")

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


class NationalityAliasInline(admin.TabularInline):
    model = NationalityAlias
    extra = 0


@admin.register(Nationality)
class NationalityAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "region_hint")
    search_fields = ("name", "aliases__raw")
    inlines = [NationalityAliasInline]


class ConflictTypeAliasInline(admin.TabularInline):
    model = ConflictTypeAlias
    extra = 0


@admin.register(ConflictType)
class ConflictTypeAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name", "aliases__raw")
    inlines = [ConflictTypeAliasInline]


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("url", "channel_name", "posted_at", "is_classified", "is_relevant", "event")
    list_filter = ("task", "is_classified", "is_relevant")
    search_fields = ("url", "text")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("event_date", "region_subject", "settlement", "conflict_type",
                    "sides_list", "count_short", "reach_display", "posts_preview", "summary")
    readonly_fields = ("posts_all",)
    date_hierarchy = "event_date"
    list_filter = (
        ("event_date", DateRangeFilterBuilder(title="Період")),   # період (from/to)
        TaskFilter,                        # за задачею
        RunFilter,                         # за запуском
        SubjectFilter,                     # за суб'єктом РФ
        SidesFilter,                       # за національностями
        ChannelFilter,                     # за каналами
        TypeFilter,                        # за типом
        "is_corroborated",
    )
    search_fields = ("summary", "region", "settlement")
    filter_horizontal = ("sides",)
    autocomplete_fields = ("region_subject", "conflict_type", "sides", "run", "task")

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
        return super().get_queryset(request).prefetch_related("posts__channel", "sides")

    @admin.display(description="Сторони")
    def sides_list(self, obj):
        return ", ".join(s.name for s in obj.sides.all())

    @admin.display(description="к-сть", ordering="post_count")
    def count_short(self, obj):
        return obj.post_count

    @admin.display(description="Охоплення", ordering="reach")
    def reach_display(self, obj):
        return f"{obj.reach:,}".replace(",", " ")

    @admin.display(description="Публікації")
    def posts_preview(self, obj):
        """Up to 3 post links (channel handle as label); public channels first."""
        ordered = sorted(
            obj.posts.all(),
            key=lambda p: (not p.channel_name, p.posted_at or p.created_at),
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
        """All post links in the detail view."""
        posts = list(obj.posts.all())
        if not posts:
            return "—"
        return format_html_join(
            mark_safe("<br>"),
            '<a href="{}" target="_blank" rel="noopener">{}</a> — {}',
            ((p.url, p.url, p.channel_name or "") for p in posts),
        )
