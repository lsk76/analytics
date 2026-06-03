from django.contrib import admin

from .models import Proxy, TelegramAccount


@admin.register(Proxy)
class ProxyAdmin(admin.ModelAdmin):
    list_display = ("proxy_string", "proxy_type", "is_active", "is_working", "fail_count")
    list_filter = ("proxy_type", "is_active", "is_working")


@admin.register(TelegramAccount)
class TelegramAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "phone_number", "is_authenticated", "is_active", "last_used_at")
    list_filter = ("is_authenticated", "is_active")
    search_fields = ("name", "phone_number")
