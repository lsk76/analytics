import json
import random
import threading
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import Proxy, TelegramAccount
from .services.telegram_client import TelegramUserClient

TEST_BOT_RUNS_DIR = Path(settings.BASE_DIR) / "_dir" / "_test_bot_runs"


def _test_bot_status_path(run_id: str) -> Path:
    TEST_BOT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return TEST_BOT_RUNS_DIR / f"{run_id}.json"


def _write_test_bot_status(run_id: str, status: dict) -> None:
    _test_bot_status_path(run_id).write_text(json.dumps(status, ensure_ascii=False, default=str))


def _read_test_bot_status(run_id: str) -> dict | None:
    path = _test_bot_status_path(run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _run_test_bot_queue(run_id: str, account_ids: list, bot_username: str,
                        pause_min: int, pause_max: int, feedback_text: str) -> None:
    """Фоновий тред: акаунти по черзі, з довільною паузою pause_min..pause_max хв між ними.

    Живе, поки живий процес web (daemon-тред) — переживає лише в межах поточного
    контейнера; рестарт/деплой обірве недороблену чергу. Прогрес пишеться у файл
    на спільному томі, щоб його бачили всі gunicorn-воркери, що обслуговують
    сторінку статусу.
    """
    accounts = list(TelegramAccount.objects.filter(pk__in=account_ids).order_by("id"))
    status = {
        "run_id": run_id, "bot_username": bot_username,
        "pause_min": pause_min, "pause_max": pause_max,
        "total": len(accounts), "entries": [], "done": False,
        "started_at": timezone.now().isoformat(),
    }
    _write_test_bot_status(run_id, status)
    for i, account in enumerate(accounts):
        if i > 0:
            delay = random.uniform(pause_min * 60, pause_max * 60)
            status["entries"].append({
                "wait": True, "waiting_minutes": round(delay / 60, 1),
                "before_account": account.name,
            })
            _write_test_bot_status(run_id, status)
            time.sleep(delay)
        res = TelegramUserClient.test_bot_flow_sync(account, bot_username,
                                                     feedback_text=feedback_text)
        status["entries"].append({
            "account_id": account.id, "account": account.name,
            "phone": account.phone_number, "ok": res.get("ok"),
            "error": res.get("error"), "steps": res.get("steps", []),
            "finished_at": timezone.now().isoformat(),
        })
        _write_test_bot_status(run_id, status)
    status["done"] = True
    status["finished_at"] = timezone.now().isoformat()
    _write_test_bot_status(run_id, status)


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
            path("test-bot/<str:run_id>/status/",
                 self.admin_site.admin_view(self.test_bot_status_view),
                 name="accounts_telegramaccount_test_bot_status"),
        ]
        return custom + super().get_urls()

    TEST_BOT_CHOICES = ["@regionalnaya_programa_bot"]
    TEST_BOT_FEEDBACK = "хорошего не много"
    TEST_BOT_PAUSE_MIN_DEFAULT = 10
    TEST_BOT_PAUSE_MAX_DEFAULT = 30

    def test_bot_view(self, request):
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

            run_id = uuid.uuid4().hex[:12]
            threading.Thread(
                target=_run_test_bot_queue,
                args=(run_id, ids, bot_username, pause_min, pause_max, self.TEST_BOT_FEEDBACK),
                daemon=True,
            ).start()
            messages.success(request,
                             f"Чергу запущено: {len(accounts)} акаунт(и), пауза {pause_min}-{pause_max} "
                             "хв між ними. Онови сторінку статусу, щоб побачити прогрес.")
            return redirect("admin:accounts_telegramaccount_test_bot_status", run_id)

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

    def test_bot_status_view(self, request, run_id):
        status = _read_test_bot_status(run_id)
        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "run_id": run_id,
            "status": status,
            "title": f"Прогін бота — статус {run_id}",
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
