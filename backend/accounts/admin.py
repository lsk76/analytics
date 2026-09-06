import uuid

from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Proxy, TelegramAccount, TestBotJob
from .services.telegram_client import TelegramUserClient


@admin.register(Proxy)
class ProxyAdmin(admin.ModelAdmin):
    list_display = ("proxy_string", "proxy_type", "is_active", "is_working", "fail_count")
    list_filter = ("proxy_type", "is_active", "is_working")


@admin.register(TestBotJob)
class TestBotJobAdmin(admin.ModelAdmin):
    list_display = ("batch_id", "order", "account", "bot_username", "status",
                    "pause_min", "pause_max", "created_at", "finished_at", "status_link")
    list_filter = ("status", "bot_username")
    search_fields = ("batch_id", "account__name", "account__phone_number")
    readonly_fields = ("batch_id", "order", "account", "bot_username", "feedback_text",
                      "pause_min", "pause_max", "status", "scheduled_at", "locked_at",
                      "attempts", "result", "error", "created_at", "finished_at")
    ordering = ("-created_at", "order")
    actions = ["retry_job"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Статус запуску")
    def status_link(self, obj):
        url = reverse("admin:accounts_telegramaccount_test_bot_status", args=[obj.batch_id])
        return format_html('<a href="{}">переглянути →</a>', url)

    @admin.action(description="🔁 Повторити (скинути в чергу негайно)")
    def retry_job(self, request, queryset):
        n = 0
        for job in queryset:
            if job.status not in ("failed", "cancelled"):
                continue
            job.status = "pending"
            job.scheduled_at = None
            job.locked_at = None
            job.error = ""
            job.result = None
            job.save(update_fields=["status", "scheduled_at", "locked_at", "error", "result"])
            n += 1
        if n:
            self.message_user(request, f"Повернуто в чергу: {n}. Воркер підхопить негайно.",
                              level=messages.SUCCESS)
        else:
            self.message_user(request, "Нічого не повторено — обирай завдання зі статусом "
                              "«Помилка» або «Скасовано».", level=messages.WARNING)


@admin.register(TelegramAccount)
class TelegramAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "phone_number", "proxy", "is_authenticated", "is_active",
                    "last_used_at")
    list_filter = ("is_authenticated", "is_active")
    search_fields = ("name", "phone_number")
    readonly_fields = ("authorize_button",)
    actions = ["check_alive", "test_bot_flow"]

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

    @admin.action(description="🤖 Тестовий прогін бота (опитування через акаунт(и))")
    def test_bot_flow(self, request, queryset):
        ids = ",".join(str(pk) for pk in queryset.order_by("id").values_list("id", flat=True))
        return redirect(reverse("admin:accounts_telegramaccount_test_bot") + f"?ids={ids}")

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
            path("test-bot/",
                 self.admin_site.admin_view(self.test_bot_view),
                 name="accounts_telegramaccount_test_bot"),
            path("test-bot/<str:batch_id>/status/",
                 self.admin_site.admin_view(self.test_bot_status_view),
                 name="accounts_telegramaccount_test_bot_status"),
        ]
        return custom + super().get_urls()

    TEST_BOT_CHOICES = ["@regionalnaya_programa_bot", "@RegionalnayaProgrammaBot",
                       "@RegProgramaEdRosBot"]
    TEST_BOT_FEEDBACK = "хорошего не много"
    TEST_BOT_PAUSE_MIN_DEFAULT = 10
    TEST_BOT_PAUSE_MAX_DEFAULT = 30

    def test_bot_view(self, request):
        """Поставити в чергу TestBotJob по одному на акаунт. Виконує воркер `run_worker --stage test_bot`."""
        ids_raw = request.GET.get("ids", "")
        ids = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()]
        accounts = list(TelegramAccount.objects.filter(pk__in=ids).order_by("id"))
        if not accounts:
            messages.error(request, "Не вибрано жодного акаунта.")
            return redirect("admin:accounts_telegramaccount_changelist")

        if request.method == "POST":
            bot_username = request.POST.get("bot_username", "").strip() or self.TEST_BOT_CHOICES[0]
            try:
                pause_min = int(request.POST.get("pause_min", self.TEST_BOT_PAUSE_MIN_DEFAULT))
                pause_max = int(request.POST.get("pause_max", self.TEST_BOT_PAUSE_MAX_DEFAULT))
            except ValueError:
                messages.error(request, "Пауза — ціле число хвилин.")
                return redirect(f"{request.path}?ids={ids_raw}")
            if pause_min < 0 or pause_max < pause_min:
                messages.error(request, "Мін. пауза ≥ 0, макс. пауза ≥ мін.")
                return redirect(f"{request.path}?ids={ids_raw}")

            batch_id = uuid.uuid4().hex[:12]
            for i, account in enumerate(accounts):
                TestBotJob.objects.create(
                    batch_id=batch_id, order=i, account=account,
                    bot_username=bot_username, feedback_text=self.TEST_BOT_FEEDBACK,
                    pause_min=pause_min, pause_max=pause_max,
                    status="pending" if i == 0 else "queued",
                )
            messages.success(request,
                             f"У чергу поставлено {len(accounts)} акаунт(и), пауза "
                             f"{pause_min}-{pause_max} хв між ними. Виконує воркер "
                             "`run_worker --stage test_bot` — онови сторінку статусу для прогресу.")
            return redirect("admin:accounts_telegramaccount_test_bot_status", batch_id)

        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "accounts": accounts,
            "ids": ids_raw,
            "bot_choices": self.TEST_BOT_CHOICES,
            "pause_min": self.TEST_BOT_PAUSE_MIN_DEFAULT,
            "pause_max": self.TEST_BOT_PAUSE_MAX_DEFAULT,
            "title": "Тестовий прогін бота",
        }
        return render(request, "admin/accounts/telegramaccount/test_bot.html", ctx)

    def test_bot_status_view(self, request, batch_id):
        if request.method == "POST" and request.POST.get("action") == "cancel":
            n = (TestBotJob.objects.filter(batch_id=batch_id, status__in=["queued", "pending"])
                 .update(status="cancelled"))
            messages.success(request, f"Скасовано {n} завдання(нь), що ще не почались.")
            return redirect(request.path)

        jobs = list(TestBotJob.objects.filter(batch_id=batch_id).select_related("account")
                    .order_by("order"))
        if not jobs:
            messages.error(request, "Такого запуску не знайдено.")
            return redirect("admin:accounts_telegramaccount_changelist")

        can_cancel = any(j.status in ("queued", "pending") for j in jobs)
        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "batch_id": batch_id,
            "jobs": jobs,
            "can_cancel": can_cancel,
            "all_finished": all(j.status in ("done", "failed", "cancelled") for j in jobs),
            "title": f"Прогін бота — статус {batch_id}",
        }
        return render(request, "admin/accounts/telegramaccount/test_bot_status.html", ctx)

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
