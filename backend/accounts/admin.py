from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Proxy, TelegramAccount
from .services.telegram_client import TelegramUserClient


@admin.register(Proxy)
class ProxyAdmin(admin.ModelAdmin):
    list_display = ("proxy_string", "proxy_type", "is_active", "is_working", "fail_count")
    list_filter = ("proxy_type", "is_active", "is_working")


@admin.register(TelegramAccount)
class TelegramAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "phone_number", "proxy", "is_authenticated", "is_active",
                    "last_used_at")
    list_filter = ("is_authenticated", "is_active")
    search_fields = ("name", "phone_number")
    readonly_fields = ("authorize_button",)
    actions = ["check_alive"]

    @admin.action(description="🔎 Перевірити живість (get_me через проксі, без надсилання)")
    def check_alive(self, request, queryset):
        """Прогнати кожен виділений акаунт через check_alive_sync і показати стан.

        Читання: connect + get_me. Нічого не надсилає. Послідовно, з паузою,
        щоб не бити всі акаунти в мережу одночасно.
        """
        import time as _time
        from django.utils import timezone as _tz

        alive = dead = 0
        for acc in queryset.order_by("id"):
            res = TelegramUserClient.check_alive_sync(acc)
            detail = f" — {res['detail']}" if res.get("detail") else ""
            if res.get("ok"):
                alive += 1
                acc.last_used_at = _tz.now()
                acc.save(update_fields=["last_used_at"])
                self.message_user(request,
                                  f"#{acc.id} {acc.phone_number}: {res['state']}{detail}",
                                  level=messages.SUCCESS)
            else:
                dead += 1
                self.message_user(request,
                                  f"#{acc.id} {acc.phone_number}: {res['state']}{detail}",
                                  level=messages.WARNING)
            _time.sleep(2)
        self.message_user(request, f"Готово: живих {alive}, проблемних {dead} із {alive+dead}.",
                          level=messages.INFO if not dead else messages.WARNING)

    # ---- authorize button on the change page ----
    @admin.display(description="Авторизація")
    def authorize_button(self, obj):
        if not obj or not obj.pk:
            return "— (спершу збережи акаунт)"
        url = reverse("admin:accounts_telegramaccount_authorize", args=[obj.pk])
        if obj.is_authenticated:
            return format_html('<b style="color:#16a34a">✓ авторизовано</b>'
                               '&nbsp;&nbsp;<a class="button" href="{}">переавторизувати</a>', url)
        return format_html('<a class="button" style="background:#2563eb;color:#fff" href="{}">'
                           '🔑 Авторизувати</a>', url)

    def get_urls(self):
        custom = [
            path("<int:account_id>/authorize/",
                 self.admin_site.admin_view(self.authorize_view),
                 name="accounts_telegramaccount_authorize"),
        ]
        return custom + super().get_urls()

    def authorize_view(self, request, account_id):
        account = get_object_or_404(TelegramAccount, pk=account_id)
        if request.method == "POST":
            act = request.POST.get("action")
            if act == "send_code":
                res = TelegramUserClient.send_code_sync(account)
                if res.get("success"):
                    where = {
                        "SentCodeTypeApp": "у ЗАСТОСУНОК Telegram (службовий чат «Telegram» / 777000) на пристрої, де цей номер залогінений — НЕ SMS",
                        "SentCodeTypeSms": "SMS на номер",
                        "SentCodeTypeCall": "дзвінком (продиктують)",
                        "SentCodeTypeFlashCall": "flash-call (останні цифри вхідного номера)",
                        "SentCodeTypeMissedCall": "пропущеним дзвінком (останні цифри номера)",
                    }.get(res.get("code_type"), res.get("code_type"))
                    nxt = res.get("next_type")
                    messages.success(request, f"Код надіслано: {where}."
                                     + (f" (повторний запит піде через {nxt})" if nxt else ""))
                else:
                    messages.error(request, f"Не вдалося надіслати код: {res}")
            elif act == "verify":
                res = TelegramUserClient.verify_code_sync(
                    account,
                    request.POST.get("code", "").strip(),
                    (request.POST.get("password", "").strip() or None),
                )
                if res.get("success"):
                    messages.success(request, "✓ Акаунт авторизовано.")
                    return redirect("admin:accounts_telegramaccount_change", account.pk)
                messages.error(request, f"Невірний код / помилка: {res}")
            return redirect(request.path)

        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "account": account,
            "code_sent": bool(account.auth_code_hash),
            "title": f"Авторизація: {account.name}",
        }
        return render(request, "admin/accounts/telegramaccount/authorize.html", ctx)
