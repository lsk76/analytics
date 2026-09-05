"""
Менеджер Telegram-акаунтів — переюзаний патерн з llm-council.
Telethon StringSession зберігається в БД; SOCKS5/HTTP проксі з failover.
Використовується для ЗБАГАЧЕННЯ (надійні дати постів, метадані каналів) результатів TeleZip.
"""
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models


def get_default_api_id():
    return getattr(settings, "TELEGRAM_API_ID", "")


def get_default_api_hash():
    return getattr(settings, "TELEGRAM_API_HASH", "")


class Proxy(models.Model):
    TYPE_CHOICES = [("socks5", "SOCKS5"), ("http", "HTTP")]

    proxy_string = models.TextField(
        verbose_name="Проксі-рядок",
        help_text="Формат: host:port:username:password або host:port",
    )
    proxy_type = models.CharField(
        max_length=10, choices=TYPE_CHOICES, default="socks5",
        verbose_name="Тип",
    )
    is_active = models.BooleanField(default=True, verbose_name="Активний")
    is_working = models.BooleanField(default=True, verbose_name="Працює")
    fail_count = models.PositiveIntegerField(default=0, verbose_name="Кількість відмов")
    last_tested_at = models.DateTimeField(null=True, blank=True, verbose_name="Остання перевірка")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Проксі"
        verbose_name_plural = "Проксі"
        ordering = ["fail_count", "id"]
        unique_together = [["proxy_string", "proxy_type"]]

    def _parts(self):
        parts = self.proxy_string.split(":", 3)
        host = parts[0] if len(parts) > 0 else ""
        port = int(parts[1]) if len(parts) > 1 else 0
        username = parts[2] if len(parts) > 2 else None
        password = parts[3] if len(parts) > 3 else None
        return host, port, username, password

    def to_telethon_proxy(self):
        if not self.is_active or not self.is_working:
            return None
        import socks
        host, port, username, password = self._parts()
        ptype = socks.SOCKS5 if self.proxy_type == "socks5" else socks.HTTP
        return (ptype, host, port, True, username or None, password or None)

    def __str__(self):
        return f"{'✓' if self.is_working else '✗'} {self.proxy_string}"


class TelegramAccount(models.Model):
    """Акаунт Telegram User API (Telethon StringSession) для скрейпінгу/збагачення."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="telegram_accounts",
        verbose_name="Користувач",
    )
    name = models.CharField(
        max_length=100, verbose_name="Назва",
        help_text="Зручна назва для цього акаунту",
    )
    phone_number = models.CharField(
        max_length=20, verbose_name="Номер телефону",
        help_text="Номер телефону з кодом країни (напр., +380501234567)",
    )
    api_id = models.CharField(
        max_length=20, default=get_default_api_id, verbose_name="API ID",
        help_text="Telegram API ID з my.telegram.org (за замовчуванням з TELEGRAM_API_ID)",
    )
    api_hash = models.CharField(
        max_length=64, default=get_default_api_hash, verbose_name="API Hash",
        help_text="Telegram API Hash з my.telegram.org (за замовчуванням з TELEGRAM_API_HASH)",
    )
    session_string = models.TextField(
        blank=True, verbose_name="Сесія",
        help_text="Telethon session string (генерується автоматично після авторизації)",
    )
    is_authenticated = models.BooleanField(default=False, verbose_name="Авторизовано")
    auth_code_hash = models.CharField(
        max_length=100, blank=True, verbose_name="Хеш коду",
        help_text="Тимчасовий хеш для верифікації коду",
    )
    proxy = models.ForeignKey(
        Proxy, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="accounts", verbose_name="Проксі",
    )
    is_active = models.BooleanField(default=True, verbose_name="Активний")

    # --- відбиток клієнта (для імпортованих сесій) ---
    # Telegram показує ці значення у «Активних сесіях» акаунта і помічає, коли
    # вони змінюються. Для сесії, створеної іншим клієнтом, треба передавати ТІ САМІ
    # значення, інакше пристрій «стрибне» на дефолтний Telethon. Порожньо = дефолти Telethon.
    device_model = models.CharField(
        max_length=100, blank=True, verbose_name="Модель пристрою",
        help_text="Напр. «Lenovo G50-80». Порожньо — дефолт Telethon.",
    )
    system_version = models.CharField(
        max_length=64, blank=True, verbose_name="Версія ОС",
        help_text="Напр. «Windows 10».",
    )
    app_version = models.CharField(
        max_length=64, blank=True, verbose_name="Версія застосунку",
        help_text="Напр. «3.4.3 x64».",
    )
    lang_code = models.CharField(max_length=8, blank=True, verbose_name="Мова клієнта")
    system_lang_code = models.CharField(max_length=8, blank=True, verbose_name="Мова системи")

    last_used_at = models.DateTimeField(null=True, blank=True, verbose_name="Останнє використання")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    def client_kwargs(self) -> dict:
        """Непорожні device-параметри для TelegramClient (порожні — дефолти Telethon)."""
        pairs = (("device_model", self.device_model),
                 ("system_version", self.system_version),
                 ("app_version", self.app_version),
                 ("lang_code", self.lang_code),
                 ("system_lang_code", self.system_lang_code))
        return {k: v for k, v in pairs if v}

    class Meta:
        verbose_name = "Telegram акаунт"
        verbose_name_plural = "Telegram акаунти"
        unique_together = ["user", "phone_number"]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{'✓' if self.is_authenticated else '○'} {self.name} ({self.phone_number})"


class TestBotJob(models.Model):
    """Черга «тестовий прогін бота» — один запис на акаунт у межах одного запуску (batch).

    Ланцюжок у batch виконується послідовно: перше завдання одразу `pending`,
    решта створюються `queued` і переходять у `pending` (з `scheduled_at` — випадкова
    пауза pause_min..pause_max хв від моменту завершення попереднього) лише коли
    попереднє в цьому ж batch завершилось. Воркер (`test_bot` стадія, taskless,
    claim через SELECT ... FOR UPDATE SKIP LOCKED) забирає найдавніше добуте
    `pending`-завдання; `scheduled_at` — той самий гейт-патерн, що й
    `CollectChunk.next_retry_at` у events-конвеєрі.
    """
    STATUS_CHOICES = [
        ("queued", "У черзі (чекає попереднього)"),
        ("pending", "Готове до запуску"),
        ("running", "Виконується"),
        ("done", "Завершено"),
        ("failed", "Помилка"),
        ("cancelled", "Скасовано"),
    ]

    batch_id = models.CharField(max_length=32, db_index=True, verbose_name="Запуск")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок у запуску")
    account = models.ForeignKey(
        TelegramAccount, on_delete=models.CASCADE, related_name="test_bot_jobs",
        verbose_name="Акаунт",
    )
    bot_username = models.CharField(max_length=64, verbose_name="Бот")
    feedback_text = models.CharField(max_length=200, verbose_name="Текст відгуку")
    pause_min = models.PositiveIntegerField(verbose_name="Пауза від, хв")
    pause_max = models.PositiveIntegerField(verbose_name="Пауза до, хв")

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="queued",
                              db_index=True, verbose_name="Статус")
    scheduled_at = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name="Не раніше",
        help_text="Гейт готовності: pending-завдання забирається воркером лише після цього часу.",
    )
    locked_at = models.DateTimeField(null=True, blank=True, verbose_name="Захоплено")
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name="Спроб")

    result = models.JSONField(null=True, blank=True, verbose_name="Результат (кроки)")
    error = models.TextField(blank=True, verbose_name="Помилка")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершено")

    class Meta:
        verbose_name = "Тестовий прогін бота — завдання"
        verbose_name_plural = "Тестовий прогін бота — завдання"
        ordering = ["batch_id", "order"]
        indexes = [
            models.Index(fields=["status", "scheduled_at"]),
            models.Index(fields=["batch_id", "order"]),
        ]

    def __str__(self):
        return f"{self.batch_id}#{self.order} {self.account.name} [{self.status}]"
