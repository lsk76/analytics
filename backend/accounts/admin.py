import uuid

from django.contrib import admin, messages
from django.db import IntegrityError
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from analysis.multiselect_filter import MultiSelectFilter

from .models import AccountTag, Proxy, TelegramAccount, TelegramBot, TestBotJob, WarmUpJob
from .services.tdata_import import import_tdata_account_from_uploads
from .services.telegram_client import TelegramUserClient
from .services.translit import normalize_bot_username, slugify_bot_username


class AccountTagFilter(MultiSelectFilter):
    """Теги акаунтів з включенням/виключенням (той самий патерн, що й теги подій —
    TagCategoryMultiSelectFilter у analysis/admin.py, лише без категорій/фасетних лічильників
    проти інших активних фільтрів — тегів акаунтів мало, повний facet_base тут зайвий)."""
    title = "Теги"
    parameter_name = "tag"
    template = "admin/filters/multi_select_with_exclude.html"

    @property
    def exclude_param(self):
        return f"{self.parameter_name}_excl"

    def __init__(self, request, params, model, model_admin):
        super().__init__(request, params, model, model_admin)
        params.pop(self.exclude_param, None)

    def expected_parameters(self):
        return [self.parameter_name, self.exclude_param]

    def queryset(self, request, queryset):
        inc = self.request.GET.getlist(self.parameter_name)
        exc = self.request.GET.getlist(self.exclude_param)
        if inc:
            queryset = queryset.filter(tags__id__in=inc).distinct()
        if exc:
            queryset = queryset.exclude(tags__id__in=exc).distinct()
        return queryset

    def filter_queryset(self, queryset, values):  # kept for base-class contract
        return queryset.filter(tags__id__in=values)

    def lookups(self, request, model_admin):
        return [(str(t.id), t.name) for t in AccountTag.objects.order_by("name")]

    def choices(self, changelist):
        included = self.request.GET.getlist(self.parameter_name)
        excluded = self.request.GET.getlist(self.exclude_param)
        yield {
            "selected": not (included or excluded),
            "query_string": changelist.get_query_string(
                remove=[self.parameter_name, self.exclude_param]),
            "display": "Всі",
            "value": "__all__",
        }
        counts = dict(AccountTag.objects.annotate(n=Count("accounts", distinct=True))
                      .values_list("id", "n"))
        for tag in AccountTag.objects.order_by("name"):
            tid = str(tag.id)
            yield {
                "included": tid in included,
                "excluded": tid in excluded,
                "display": f"{tag.name} ({counts.get(tag.id, 0)})",
                "value": tid,
            }


@admin.register(AccountTag)
class AccountTagAdmin(admin.ModelAdmin):
    list_display = ("name", "accounts_count", "created_at")
    search_fields = ("name",)

    @admin.display(description="Акаунтів")
    def accounts_count(self, obj):
        return obj.accounts.count()


@admin.register(Proxy)
class ProxyAdmin(admin.ModelAdmin):
    list_display = ("proxy_string", "proxy_type", "is_active", "is_working", "fail_count",
                    "last_tested_at")
    list_filter = ("proxy_type", "is_active", "is_working")
    ordering = ("-last_tested_at",)


@admin.register(TelegramBot)
class TelegramBotAdmin(admin.ModelAdmin):
    list_display = ("username", "name", "account", "token", "updated_at", "edit_link")
    search_fields = ("username", "name", "account__name")
    list_filter = ("account",)
    readonly_fields = ("username", "name", "token", "account", "created_at", "updated_at",
                      "edit_link")

    def has_add_permission(self, request):
        return False

    @admin.display(description="")
    def edit_link(self, obj):
        if not obj or not obj.pk:
            return "—"
        url = reverse("admin:accounts_telegrambot_edit", args=[obj.pk])
        return format_html('<a class="button" href="{}">✏️ Редагувати</a>', url)

    def get_urls(self):
        custom = [
            path("<int:bot_id>/edit/",
                 self.admin_site.admin_view(self.edit_bot_view),
                 name="accounts_telegrambot_edit"),
        ]
        return custom + super().get_urls()

    def edit_bot_view(self, request, bot_id):
        bot = get_object_or_404(TelegramBot, pk=bot_id)
        if request.method == "POST":
            new_name = request.POST.get("name", "").strip()
            photo = request.FILES.get("photo")

            if new_name and new_name != bot.name:
                res = TelegramUserClient.set_bot_name_sync(bot.account, bot.username, new_name)
                if res.get("ok"):
                    bot.name = new_name
                    bot.save(update_fields=["name"])
                    messages.success(request, f"✓ Назву змінено на «{new_name}».")
                else:
                    messages.error(request, f"Не вдалось змінити назву: "
                                   f"{res.get('error')} {res.get('detail', '')[:200]}")

            if photo:
                res = TelegramUserClient.set_bot_photo_sync(bot.account, bot.username,
                                                            photo.read())
                if res.get("ok"):
                    messages.success(request, "✓ Аватарку оновлено.")
                else:
                    messages.error(request, f"Не вдалось змінити аватарку: {res.get('error')} "
                                   f"{res.get('detail', '')[:200]}")

            if not new_name and not photo:
                messages.error(request, "Вкажи нову назву і/або завантаж зображення.")

            return redirect(request.path)

        botfather_log = TelegramUserClient.get_recent_messages_sync(
            bot.account, "BotFather", limit=15)
        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "bot": bot,
            "botfather_log": botfather_log,
            "title": f"Редагувати бота: @{bot.username}",
        }
        return render(request, "admin/accounts/telegrambot/edit_bot.html", ctx)


@admin.register(WarmUpJob)
class WarmUpJobAdmin(admin.ModelAdmin):
    list_display = ("account", "status", "handles_count", "attempts", "created_at", "finished_at")
    list_filter = ("status",)
    search_fields = ("account__name", "account__phone_number")
    readonly_fields = ("account", "handles", "status", "scheduled_at", "locked_at", "attempts",
                      "result", "error", "created_at", "finished_at")
    ordering = ("-created_at",)
    actions = ["retry_job"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Каналів")
    def handles_count(self, obj):
        return len(obj.handles or [])

    @admin.action(description="🔁 Повторити (скидає лічильник спроб — знову 3 автоспроби)")
    def retry_job(self, request, queryset):
        n = (queryset.filter(status="failed")
             .update(status="pending", scheduled_at=None, locked_at=None,
                     attempts=0, error="", result=None))
        if n:
            self.message_user(request, f"Повернуто в чергу: {n}. Воркер підхопить негайно.",
                              level=messages.SUCCESS)
        else:
            self.message_user(request, "Нічого не повторено — обирай завдання зі статусом "
                              "«Помилка».", level=messages.WARNING)


@admin.register(TestBotJob)
class TestBotJobAdmin(admin.ModelAdmin):
    list_display = ("batch_id", "order", "account", "bot_username", "status", "attempts",
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

    @admin.action(description="🔁 Повторити (скидає лічильник спроб, з паузою pause_min-pause_max)")
    def retry_job(self, request, queryset):
        import random
        from datetime import timedelta

        from django.utils import timezone as _tz

        n = 0
        for job in queryset:
            if job.status not in ("failed", "cancelled"):
                continue
            delay = random.uniform(job.pause_min * 60, job.pause_max * 60)
            job.status = "pending"
            job.scheduled_at = _tz.now() + timedelta(seconds=delay)
            job.locked_at = None
            job.attempts = 0
            job.error = ""
            job.result = None
            job.save(update_fields=["status", "scheduled_at", "locked_at", "attempts",
                                    "error", "result"])
            n += 1
        if n:
            self.message_user(request, f"Повернуто в чергу: {n}, з паузою "
                              "pause_min-pause_max цього завдання (знову 3 автоспроби).",
                              level=messages.SUCCESS)
        else:
            self.message_user(request, "Нічого не повторено — обирай завдання зі статусом "
                              "«Помилка» або «Скасовано».", level=messages.WARNING)


@admin.register(TelegramAccount)
class TelegramAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "phone_number", "is_authenticated", "is_active",
                    "tag_list", "spam_status", "spam_status_checked_at", "last_used_at")
    list_filter = ("is_authenticated", "is_active", "spam_status", AccountTagFilter)
    search_fields = ("name", "phone_number", "tags__name")
    readonly_fields = ("authorize_button", "channels_button", "messages_button")
    filter_horizontal = ("tags",)
    actions = ["check_alive", "check_spam_status", "test_bot_flow", "warm_up_channels",
              "add_tag_action", "create_bot_action", "sync_bots_action"]

    @admin.display(description="Теги")
    def tag_list(self, obj):
        return ", ".join(obj.tags.values_list("name", flat=True)) or "—"

    @admin.action(description="🐣 Завести бота (через @BotFather)")
    def create_bot_action(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Вибери рівно один акаунт — саме він піде до @BotFather.",
                              level=messages.ERROR)
            return
        account = queryset.first()
        return redirect(reverse("admin:accounts_telegramaccount_create_bot", args=[account.pk]))

    @admin.action(description="🔄 Оновити список ботів (синк через @BotFather /token)")
    def sync_bots_action(self, request, queryset):
        import time as _time

        accounts = list(queryset.order_by("id"))
        for i, acc in enumerate(accounts):
            res = TelegramUserClient.sync_bots_via_botfather_sync(acc)
            if not res.get("ok"):
                self.message_user(request, f"#{acc.id} {acc.name}: {res.get('error')}",
                                  level=messages.ERROR)
            else:
                n = 0
                for b in res.get("bots", []):
                    defaults = {"account": acc, "name": b.get("name") or ""}
                    if b.get("token"):
                        defaults["token"] = b["token"]
                    TelegramBot.objects.update_or_create(username=b["username"], defaults=defaults)
                    n += 1
                self.message_user(request, f"#{acc.id} {acc.name}: синхронізовано {n} бот(ів).",
                                  level=messages.SUCCESS)
            if i < len(accounts) - 1:
                _time.sleep(2)

    @admin.action(description="🏷️ Додати тег (за назвою — вводиш у наступному діалозі)")
    def add_tag_action(self, request, queryset):
        ids = ",".join(str(pk) for pk in queryset.values_list("id", flat=True))
        return redirect(reverse("admin:accounts_telegramaccount_add_tag") + f"?ids={ids}")

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

    @admin.action(description="🚫 Перевірити статус (SpamBot) — обмеження на резолв/надсилання")
    def check_spam_status(self, request, queryset):
        """/start до @SpamBot по кожному виділеному акаунту, послідовно з паузою.

        Саме таке обмеження (а не мертва проксі) валить резолв юзернеймів у
        «голих»/непрогрітих акаунтів — див. warm_up_channels.
        """
        import time as _time
        from django.utils import timezone as _tz

        for acc in queryset.order_by("id"):
            res = TelegramUserClient.check_spam_status_sync(acc)
            acc.spam_status = res.get("status", "unknown")
            acc.spam_status_detail = (res.get("detail") or "")[:300]
            acc.spam_status_checked_at = _tz.now()
            acc.save(update_fields=["spam_status", "spam_status_detail",
                                    "spam_status_checked_at"])
            level = messages.SUCCESS if res.get("status") == "free" else messages.WARNING
            self.message_user(request,
                              f"#{acc.id} {acc.phone_number}: {acc.get_spam_status_display()} "
                              f"— {acc.spam_status_detail[:120]}", level=level)
            _time.sleep(2)

    @admin.action(description="🤖 Тестовий прогін бота (опитування через акаунт(и))")
    def test_bot_flow(self, request, queryset):
        ids = ",".join(str(pk) for pk in queryset.order_by("id").values_list("id", flat=True))
        return redirect(reverse("admin:accounts_telegramaccount_test_bot") + f"?ids={ids}")

    @admin.action(description="🌱 Прогріти (підписати на 5-10 випадкових каналів з моніторингу)")
    def warm_up_channels(self, request, queryset):
        """Новий/обмежений акаунт не резолвить чужі юзернейми (UsernameNotOccupiedError),
        поки не має звичайної активності. Підписуємо на канали з довідника моніторингу
        (analysis.Channel) — ті самі, що акаунт і так читає для збагачення/скрейпінгу.

        Лише ставить у чергу (WarmUpJob) — join кожного каналу займає секунди-
        десятки секунд (мережа + пауза 3-8с між ними), і кілька акаунтів одразу
        синхронно в одному HTTP-запиті надійно валять 504 (nginx/gunicorn — 120с).
        Виконує окремий воркер `run_worker --stage warm_up`.
        """
        import random as _random

        from analysis.models import Channel

        pool = list(Channel.objects.exclude(username="")
                   .values_list("username", flat=True).distinct())
        if not pool:
            self.message_user(request, "У довіднику Channel немає жодного каналу з username.",
                              level=messages.ERROR)
            return

        n = 0
        for acc in queryset.order_by("id"):
            handles = _random.sample(pool, min(_random.randint(5, 10), len(pool)))
            WarmUpJob.objects.create(account=acc, handles=handles)
            n += 1
        self.message_user(request,
                          f"У чергу поставлено {n} акаунт(и). Виконує воркер "
                          "`run_worker --stage warm_up` — прогрес дивись у "
                          "«Прогрів акаунта — завдання».", level=messages.SUCCESS)

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

    @admin.display(description="Підписки")
    def channels_button(self, obj):
        if not obj or not obj.pk:
            return "— (спершу збережи акаунт)"
        url = reverse("admin:accounts_telegramaccount_channels", args=[obj.pk])
        return format_html('<a class="button" href="{}">📡 Переглянути канали</a>', url)

    @admin.display(description="Повідомлення")
    def messages_button(self, obj):
        if not obj or not obj.pk:
            return "— (спершу збережи акаунт)"
        url = reverse("admin:accounts_telegramaccount_messages", args=[obj.pk])
        return format_html('<a class="button" href="{}">📨 Останні повідомлення '
                           '(коди входу тут)</a>', url)

    def get_urls(self):
        custom = [
            path("<int:account_id>/authorize/",
                 self.admin_site.admin_view(self.authorize_view),
                 name="accounts_telegramaccount_authorize"),
            path("<int:account_id>/channels/",
                 self.admin_site.admin_view(self.channels_view),
                 name="accounts_telegramaccount_channels"),
            path("<int:account_id>/messages/",
                 self.admin_site.admin_view(self.messages_view),
                 name="accounts_telegramaccount_messages"),
            path("<int:account_id>/messages/poll/",
                 self.admin_site.admin_view(self.messages_poll_view),
                 name="accounts_telegramaccount_messages_poll"),
            path("<int:account_id>/create-bot/",
                 self.admin_site.admin_view(self.create_bot_view),
                 name="accounts_telegramaccount_create_bot"),
            path("test-bot/",
                 self.admin_site.admin_view(self.test_bot_view),
                 name="accounts_telegramaccount_test_bot"),
            path("test-bot/<str:batch_id>/status/",
                 self.admin_site.admin_view(self.test_bot_status_view),
                 name="accounts_telegramaccount_test_bot_status"),
            path("add-tag/",
                 self.admin_site.admin_view(self.add_tag_view),
                 name="accounts_telegramaccount_add_tag"),
            path("import-tdata/",
                 self.admin_site.admin_view(self.import_tdata_view),
                 name="accounts_telegramaccount_import_tdata"),
        ]
        return custom + super().get_urls()

    def import_tdata_view(self, request):
        if request.method == "POST":
            json_file = request.FILES.get("json_file")
            session_file = request.FILES.get("session_file")
            tag_names = [t.strip() for t in request.POST.get("tags", "").split(",") if t.strip()]
            if not json_file or not session_file:
                messages.error(request, "Потрібні обидва файли: JSON і .session.")
            else:
                try:
                    account = import_tdata_account_from_uploads(
                        json_file, session_file, request.user, tag_names,
                    )
                    messages.success(request,
                                     f"✓ Акаунт «{account.name}» ({account.phone_number}) "
                                     "імпортовано й авторизовано.")
                    return redirect("admin:accounts_telegramaccount_change", account.pk)
                except IntegrityError:
                    messages.error(request, "Акаунт з таким номером телефону вже є в базі.")
                except Exception as e:  # noqa: BLE001
                    messages.error(request, f"Не вдалось імпортувати: {type(e).__name__}: {e}")

        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "existing_tags": AccountTag.objects.order_by("name"),
            "title": "Додати акаунт через файли (tdata JSON + .session)",
        }
        return render(request, "admin/accounts/telegramaccount/import_tdata.html", ctx)

    def add_tag_view(self, request):
        ids_raw = request.GET.get("ids") or request.POST.get("ids", "")
        ids = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()]
        accounts = list(TelegramAccount.objects.filter(pk__in=ids))
        if not accounts:
            messages.error(request, "Не вибрано жодного акаунта.")
            return redirect("admin:accounts_telegramaccount_changelist")

        if request.method == "POST":
            name = request.POST.get("tag_name", "").strip()
            if not name:
                messages.error(request, "Вкажи назву тегу.")
            else:
                tag, _ = AccountTag.objects.get_or_create(name=name)
                for acc in accounts:
                    acc.tags.add(tag)
                messages.success(request,
                                 f"Тег «{name}» додано {len(accounts)} акаунт(ам).")
                return redirect("admin:accounts_telegramaccount_changelist")

        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "accounts": accounts,
            "ids": ids_raw,
            "existing_tags": AccountTag.objects.order_by("name"),
            "title": "Додати тег",
        }
        return render(request, "admin/accounts/telegramaccount/add_tag.html", ctx)

    # feedback_text порожній => бот без кроку відгуку (has_feedback=False) — тест
    # зупиняється на першому кроці без кнопок (напр. «Спасибо» + фото сертифіката),
    # нічого зайвого не надсилаючи.
    TEST_BOT_CHOICES = [
        {"username": "@regionalnaya_programa_bot", "feedback_text": "хорошего не много"},
        {"username": "@RegionalnayaProgrammaBot", "feedback_text": "хорошего не много"},
        {"username": "@RegProgramaEdRosBot", "feedback_text": "хорошего не много"},
        {"username": "@gotovnostyedynstvobot", "feedback_text": ""},
        {"username": "@GotovnostyEdynstvoNabyevaBot", "feedback_text": ""},
    ]
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
            bot_username = request.POST.get("bot_username", "").strip() or \
                self.TEST_BOT_CHOICES[0]["username"]
            feedback_text = next((c["feedback_text"] for c in self.TEST_BOT_CHOICES
                                 if c["username"] == bot_username), "")
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
                    bot_username=bot_username, feedback_text=feedback_text,
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

        if request.method == "POST" and request.POST.get("action") == "restart":
            old_jobs = list(TestBotJob.objects.filter(batch_id=batch_id).order_by("order"))
            if not old_jobs:
                messages.error(request, "Такого запуску не знайдено.")
                return redirect("admin:accounts_telegramaccount_changelist")
            new_batch_id = uuid.uuid4().hex[:12]
            for j in old_jobs:
                TestBotJob.objects.create(
                    batch_id=new_batch_id, order=j.order, account=j.account,
                    bot_username=j.bot_username, feedback_text=j.feedback_text,
                    pause_min=j.pause_min, pause_max=j.pause_max,
                    status="pending" if j.order == 0 else "queued",
                )
            messages.success(request, f"Новий прогін запущено: {len(old_jobs)} акаунт(и).")
            return redirect("admin:accounts_telegramaccount_test_bot_status", new_batch_id)

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

    def channels_view(self, request, account_id):
        account = get_object_or_404(TelegramAccount, pk=account_id)
        res = TelegramUserClient.list_dialogs_sync(account)
        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "account": account,
            "result": res,
            "title": f"Підписки: {account.name}",
        }
        return render(request, "admin/accounts/telegramaccount/channels.html", ctx)

    MESSAGES_PEER_CHOICES = [
        (777000, "Telegram (службові — коди входу, попередження)"),
        ("BotFather", "@BotFather"),
        ("SpamBot", "@SpamBot"),
    ]

    def messages_view(self, request, account_id):
        account = get_object_or_404(TelegramAccount, pk=account_id)
        peer_raw = request.GET.get("peer", "777000")
        try:
            peer = int(peer_raw)
        except ValueError:
            peer = peer_raw
        res = TelegramUserClient.get_recent_messages_sync(account, peer, limit=30)
        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "account": account,
            "result": res,
            "peer": str(peer),
            "peer_choices": self.MESSAGES_PEER_CHOICES,
            "title": f"Повідомлення: {account.name}",
        }
        return render(request, "admin/accounts/telegramaccount/messages.html", ctx)

    def messages_poll_view(self, request, account_id):
        """Опитується JS-таймером зі сторінки повідомлень — лише нові (id > after_id)."""
        account = get_object_or_404(TelegramAccount, pk=account_id)
        peer_raw = request.GET.get("peer", "777000")
        try:
            peer = int(peer_raw)
        except ValueError:
            peer = peer_raw
        try:
            after_id = int(request.GET.get("after_id", 0))
        except ValueError:
            after_id = 0
        res = TelegramUserClient.get_recent_messages_sync(account, peer, limit=10)
        if not res.get("ok"):
            return JsonResponse({"ok": False, "error": res.get("error")})
        new = sorted((m for m in res["messages"] if m["id"] > after_id),
                    key=lambda m: m["id"])
        return JsonResponse({"ok": True, "messages": new})

    def create_bot_view(self, request, account_id):
        account = get_object_or_404(TelegramAccount, pk=account_id)
        result = None
        if request.method == "POST":
            name = request.POST.get("name", "").strip()
            identifier_raw = request.POST.get("identifier", "").strip()
            if not name:
                messages.error(request, "Вкажи назву бота.")
            else:
                username = (normalize_bot_username(identifier_raw) if identifier_raw
                           else slugify_bot_username(name))
                photo = request.FILES.get("photo")
                photo_bytes = photo.read() if photo else None
                result = TelegramUserClient.create_bot_via_botfather_sync(
                    account, name, username, photo_bytes,
                )
                if result.get("ok"):
                    TelegramBot.objects.update_or_create(
                        username=result["username"],
                        defaults={"account": account, "name": name, "token": result["token"]},
                    )
                    messages.success(request,
                                     f"✓ Бот @{result['username']} створено й збережено в БД "
                                     "(«Боти» в адмінці). Токен показано нижче.")
                else:
                    messages.error(request, f"Не вдалось: {result.get('error')} "
                                   f"{result.get('detail', '')[:200]}")

        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "account": account,
            "result": result,
            "title": f"Завести бота: {account.name}",
        }
        return render(request, "admin/accounts/telegramaccount/create_bot.html", ctx)

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
