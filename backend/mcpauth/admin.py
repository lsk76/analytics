"""Адмінка доступу до MCP: ролі, клієнти, токени, аудит.

Усе, крім ролей, доступне лише на читання: токени й коди видає сам OAuth-флоу,
а руками їх можна хіба що ВІДКЛИКАТИ (дія нижче) — редагувати хеші безглуздо.
"""
from django.contrib import admin, messages
from django.utils import timezone

from .models import McpAuditLog, McpAuthCode, McpClient, McpRole, McpToken
from .policy import telezip_daily_limit, telezip_used


@admin.register(McpRole)
class McpRoleAdmin(admin.ModelAdmin):
    list_display = ("user", "is_active", "scopes_display", "max_scope", "tokens_count",
                    "telezip_daily_limit", "telezip_usage", "last_call", "updated_at")
    # стеля й ліміт TeleZip правляться прямо в таблиці — їх крутять часто
    list_editable = ("max_scope", "telezip_daily_limit")
    list_filter = ("max_scope", "is_active")
    search_fields = ("user__username", "user__email", "notes")
    autocomplete_fields = ("user",)

    @admin.display(description="Може через MCP (з прав в адмінці)")
    def scopes_display(self, obj):
        return ", ".join(obj.scopes) or "— (немає прав у розділах)"

    @admin.display(description="Токенів")
    def tokens_count(self, obj):
        return obj.user.mcp_tokens.filter(revoked_at__isnull=True).count()

    @admin.display(description="TeleZip сьогодні / 30 дн")
    def telezip_usage(self, obj):
        limit = telezip_daily_limit(obj)
        today, month = telezip_used(obj.user, 1), telezip_used(obj.user, 30)
        return f"{today} з {limit or '∞'} / {month}"

    @admin.display(description="Останній виклик")
    def last_call(self, obj):
        row = obj.user.mcp_calls.order_by("-created_at").values("created_at", "tool").first()
        return f"{row['tool']} · {row['created_at']:%d.%m %H:%M}" if row else "—"


@admin.register(McpClient)
class McpClientAdmin(admin.ModelAdmin):
    list_display = ("client_id", "name", "is_active", "public", "tokens_count", "created_at")
    list_filter = ("is_active",)
    search_fields = ("client_id", "name")
    readonly_fields = ("client_id", "client_secret_hash", "created_at")

    @admin.display(boolean=True, description="Публічний (PKCE)")
    def public(self, obj):
        return not obj.client_secret_hash

    @admin.display(description="Токенів")
    def tokens_count(self, obj):
        return obj.tokens.filter(revoked_at__isnull=True).count()


@admin.register(McpToken)
class McpTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "kind", "client", "scopes_display", "state",
                    "last_used_at", "created_at")
    list_filter = ("kind", "client")
    search_fields = ("user__username",)
    readonly_fields = [f.name for f in McpToken._meta.fields]
    actions = ["revoke_tokens"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Скоупи")
    def scopes_display(self, obj):
        return ", ".join(obj.scopes)

    @admin.display(description="Стан")
    def state(self, obj):
        if obj.revoked_at:
            return "відкликано"
        return "дійсний" if obj.is_valid else "протермінований"

    @admin.action(description="🔒 Відкликати (клієнт одразу втрачає доступ)")
    def revoke_tokens(self, request, queryset):
        n = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        self.message_user(request, f"Відкликано токенів: {n}", messages.WARNING)


@admin.register(McpAuthCode)
class McpAuthCodeAdmin(admin.ModelAdmin):
    list_display = ("user", "client", "expires_at", "used_at", "created_at")
    readonly_fields = [f.name for f in McpAuthCode._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(McpAuditLog)
class McpAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "role", "tool", "ok", "duration_ms",
                    "paid_requests", "payload_short", "error")
    list_filter = ("ok", "tool", "role", "user")
    search_fields = ("tool", "error", "user__username")
    readonly_fields = [f.name for f in McpAuditLog._meta.fields]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Параметри")
    def payload_short(self, obj):
        text = ", ".join(f"{k}={v}" for k, v in (obj.payload or {}).items())
        return text[:90] + ("…" if len(text) > 90 else "")
