"""Доступ до MCP-сервера: OAuth-стан, ролі користувачів, аудит викликів.

Навіщо окремий застосунок: stdio-сервер на ноутбуці не мав ні користувачів, ні
прав — «авторизацією» був доступ до самої машини. Щойно сервер стає мережевим
(прод, кілька людей), потрібні три речі, яких у Django для цього ще не було:

1. **Хто прийшов** — OAuth за стандартом MCP: клієнт реєструється сам
   (RFC 7591), людина логіниться вже наявним Django-акаунтом і підтверджує
   доступ, на виході — токен із `subject` (юзернейм) і `scopes` (роль).
2. **Що йому можна** — роль на користувачеві (`McpRole`) → скоупи в токені.
   Видимість даних лишається такою, як в адмінці: `visible_to()` для
   Telegram-акаунтів, `owner` для задач.
3. **Що він зробив** — `McpAuditLog`: на проді без сліду не можна.

Секрети (токени, коди, client_secret) зберігаються ЛИШЕ як sha256: у базі немає
нічого, що можна було б пред'явити серверу вкраденим.
"""
import hashlib
import secrets

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


class McpRole(models.Model):
    """Роль користувача в MCP. Немає рядка — немає доступу взагалі."""

    READER = "reader"
    OPERATOR = "operator"
    ANALYST = "analyst"
    ADMIN = "admin"
    ROLE_CHOICES = [
        (READER, "Читач — лише перегляд стану"),
        (OPERATOR, "Оператор — збори, чати, джерела, акаунти (наявні)"),
        (ANALYST, "Просунутий аналітик — + створює дослідження, канали, джерела, акаунти"),
        (ADMIN, "Адмін — усе, включно з налаштуваннями й контейнерами"),
    ]
    # скоупи, які отримає токен із цією роллю (перевіряються на кожному виклику).
    # mcp:create — створення нових обʼєктів (task_create, channel_add, source_add,
    # account_import): оператор працює з тим, що є, аналітик заводить своє.
    SCOPES = {
        READER: ["mcp:read"],
        OPERATOR: ["mcp:read", "mcp:write"],
        ANALYST: ["mcp:read", "mcp:write", "mcp:create"],
        ADMIN: ["mcp:read", "mcp:write", "mcp:create", "mcp:admin"],
    }

    user = models.OneToOneField(User, on_delete=models.CASCADE,
                                related_name="mcp_role", verbose_name="Користувач")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=READER,
                            verbose_name="Роль")
    is_active = models.BooleanField(default=True, verbose_name="Активний")
    # Платні виклики TeleZip (≈$0.10 кожен) рахуються на користувача за добу;
    # 0 = взяти дефолт із Setting `mcp_telezip_daily_limit`. Редагується прямо
    # у списку ролей.
    telezip_daily_limit = models.PositiveIntegerField(
        default=0, verbose_name="TeleZip: ліміт запитів/добу",
        help_text="0 = дефолт із налаштування mcp_telezip_daily_limit.")
    notes = models.CharField(max_length=200, blank=True, verbose_name="Нотатки")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Роль у MCP"
        verbose_name_plural = "Ролі у MCP"
        ordering = ["user__username"]

    @property
    def scopes(self) -> list[str]:
        return list(self.SCOPES.get(self.role, []))

    def __str__(self):
        return f"{self.user.username}: {self.get_role_display()}"


class McpClient(models.Model):
    """OAuth-клієнт (реєструється сам за RFC 7591, або заводиться руками)."""

    client_id = models.CharField(max_length=64, unique=True, verbose_name="client_id")
    # УВАГА: саме ЗНАЧЕННЯ, а не хеш. SDK звіряє секрет прямим порівнянням із
    # тим, що поверне провайдер (`ClientAuthenticator`), тож хеша там замало —
    # із ним автентифікація клієнта падає в 401 на /token.
    # Ризик обмежений: це облікові дані ЗАСТОСУНКУ, не людини. Самим секретом
    # нічого не отримати — потрібен ще код авторизації (тобто згода людини) або
    # refresh-токен, а ті зберігаються хешами.
    client_secret = models.CharField(
        max_length=200, blank=True, verbose_name="client_secret",
        help_text="Порожньо — публічний клієнт (PKCE без секрету).")
    client_secret_hash = models.CharField(
        max_length=64, blank=True, verbose_name="Хеш client_secret (легасі)")
    name = models.CharField(max_length=200, blank=True, verbose_name="Назва")
    redirect_uris = models.JSONField(default=list, verbose_name="Redirect URI")
    scope = models.CharField(max_length=200, blank=True, verbose_name="Запитані скоупи")
    grant_types = models.JSONField(default=list, verbose_name="Grant types")
    is_active = models.BooleanField(default=True, verbose_name="Активний")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "MCP-клієнт (OAuth)"
        verbose_name_plural = "MCP-клієнти (OAuth)"
        ordering = ["-created_at"]

    def check_secret(self, secret: str) -> bool:
        if not self.client_secret:
            return True                      # публічний клієнт (PKCE)
        return secrets.compare_digest(self.client_secret, secret or "")

    def __str__(self):
        return self.name or self.client_id


class McpAuthCode(models.Model):
    """Одноразовий код авторизації (живе хвилини, гине після обміну)."""

    code_hash = models.CharField(max_length=64, unique=True, verbose_name="Хеш коду")
    client = models.ForeignKey(McpClient, on_delete=models.CASCADE,
                               related_name="auth_codes", verbose_name="Клієнт")
    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             related_name="mcp_auth_codes", verbose_name="Користувач")
    redirect_uri = models.CharField(max_length=500, verbose_name="Redirect URI")
    redirect_uri_provided_explicitly = models.BooleanField(default=True)
    code_challenge = models.CharField(max_length=128, blank=True, verbose_name="PKCE challenge")
    scopes = models.JSONField(default=list, verbose_name="Скоупи")
    resource = models.CharField(max_length=500, blank=True, verbose_name="Resource (RFC 8707)")
    expires_at = models.DateTimeField(verbose_name="Дійсний до")
    used_at = models.DateTimeField(null=True, blank=True, verbose_name="Використано")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Код авторизації MCP"
        verbose_name_plural = "Коди авторизації MCP"
        ordering = ["-created_at"]

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"{self.user.username}/{self.client_id} ({'живий' if self.is_usable else 'згорів'})"


class McpAuthRequest(models.Model):
    """Запит авторизації, що чекає на людину.

    OAuth-ендпоінти живуть у MCP-процесі, а логін і згода — у Django (там сесії
    й користувачі). Місток між ними — цей рядок: провайдер кладе сюди параметри
    запиту й відправляє браузер на сторінку згоди, а та, після входу, створює
    код і повертає користувача клієнтові.
    """

    key = models.CharField(max_length=64, unique=True, verbose_name="Ключ запиту")
    client = models.ForeignKey(McpClient, on_delete=models.CASCADE,
                               related_name="auth_requests", verbose_name="Клієнт")
    redirect_uri = models.CharField(max_length=500, verbose_name="Redirect URI")
    redirect_uri_provided_explicitly = models.BooleanField(default=True)
    code_challenge = models.CharField(max_length=128, blank=True, verbose_name="PKCE challenge")
    scopes = models.JSONField(default=list, verbose_name="Запитані скоупи")
    state = models.CharField(max_length=300, blank=True, verbose_name="state")
    resource = models.CharField(max_length=500, blank=True, verbose_name="Resource")
    expires_at = models.DateTimeField(verbose_name="Дійсний до")
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершено")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Запит авторизації MCP"
        verbose_name_plural = "Запити авторизації MCP"
        ordering = ["-created_at"]

    @property
    def is_usable(self) -> bool:
        return self.completed_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"{self.client}: {'чекає' if self.is_usable else 'закрито'}"


class McpToken(models.Model):
    """Виданий токен (access або refresh). У базі — лише sha256."""

    ACCESS = "access"
    REFRESH = "refresh"
    KIND_CHOICES = [(ACCESS, "Access"), (REFRESH, "Refresh")]

    token_hash = models.CharField(max_length=64, unique=True, db_index=True,
                                  verbose_name="Хеш токена")
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=ACCESS,
                            verbose_name="Тип")
    client = models.ForeignKey(McpClient, on_delete=models.CASCADE,
                               related_name="tokens", verbose_name="Клієнт")
    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             related_name="mcp_tokens", verbose_name="Користувач")
    scopes = models.JSONField(default=list, verbose_name="Скоупи")
    resource = models.CharField(max_length=500, blank=True, verbose_name="Resource")
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name="Дійсний до")
    revoked_at = models.DateTimeField(null=True, blank=True, verbose_name="Відкликано")
    last_used_at = models.DateTimeField(null=True, blank=True, verbose_name="Востаннє вжито")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Токен MCP"
        verbose_name_plural = "Токени MCP"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["kind", "revoked_at"])]

    @property
    def is_valid(self) -> bool:
        if self.revoked_at:
            return False
        return not self.expires_at or self.expires_at > timezone.now()

    def __str__(self):
        state = "дійсний" if self.is_valid else "недійсний"
        return f"{self.kind} {self.user.username} ({state})"


class McpAuditLog(models.Model):
    """Слід кожного виклику: хто, що, з якими параметрами, чим скінчилось.

    Пишеться ЗАВЖДИ, зокрема на відмовах доступу — інакше «хто вимкнув джерело»
    лишається без відповіді.
    """

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="mcp_calls", verbose_name="Користувач")
    role = models.CharField(max_length=10, blank=True, verbose_name="Роль")
    tool = models.CharField(max_length=64, db_index=True, verbose_name="Інструмент")
    payload = models.JSONField(default=dict, blank=True, verbose_name="Параметри")
    ok = models.BooleanField(default=True, verbose_name="Успіх")
    error = models.CharField(max_length=300, blank=True, verbose_name="Помилка")
    duration_ms = models.PositiveIntegerField(default=0, verbose_name="Тривалість, мс")
    # скільки платних запитів до TeleZip зробив цей виклик (tz_users може 2)
    paid_requests = models.PositiveSmallIntegerField(
        default=0, verbose_name="Платних запитів TeleZip")
    client = models.ForeignKey(McpClient, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="calls", verbose_name="Клієнт")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True,
                                      verbose_name="Коли")

    class Meta:
        verbose_name = "Виклик MCP (аудит)"
        verbose_name_plural = "Виклики MCP (аудит)"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self):
        who = self.user.username if self.user_id else "—"
        return f"{who} → {self.tool} [{'ok' if self.ok else 'fail'}]"
