"""Реєстр інструментів MCP-шару.

Уся логіка живе ТУТ, у Django (де є ORM, Telethon-клієнти й стадії), а не в
host-процесі MCP: той лише транспорт (`manage.py mcp_rpc`, див. mcp/README.md).
Тож будь-який інструмент можна викликати й руками:

    docker compose exec -T web python manage.py mcp_rpc accounts_list

Хендлер повертає ГОТОВИЙ ТЕКСТ (див. `fmt`), а не структуру: це кінцева
відповідь моделі, а не проміжний формат.
"""
import contextvars
import inspect
import os
import time

TOOLS = {}

# Хто саме зараз викликає інструмент. Локальний stdio-сервер працює без
# користувача (повні права — доступ до машини вже все вирішив), мережевий
# прод-сервер завжди підставляє реального Django-юзера з токена.
_actor: contextvars.ContextVar = contextvars.ContextVar("mcp_actor", default=None)
# Скільки платних запитів TeleZip зробив поточний виклик — лягає в аудит.
_charged: contextvars.ContextVar = contextvars.ContextVar("mcp_charged", default=0)

SCOPE_READ = "mcp:read"
SCOPE_WRITE = "mcp:write"
SCOPE_CREATE = "mcp:create"   # нові обʼєкти: дослідження, канали, джерела, акаунти
SCOPE_ADMIN = "mcp:admin"


class ToolError(Exception):
    """Очікувана помилка інструмента (не баг): показуємо текст як є."""


class Actor:
    """Викликач: Django-користувач + його скоупи. `local()` — режим без мережі."""

    def __init__(self, user=None, scopes=None, role="", client=None, unrestricted=False):
        self.user = user
        self.scopes = list(scopes or [])
        self.role = role
        self.client = client
        self.unrestricted = unrestricted

    @classmethod
    def local(cls):
        """Локальний stdio: користувача немає, обмежень теж (це ноутбук власника)."""
        return cls(unrestricted=True, role="local")

    @property
    def is_superuser(self) -> bool:
        return self.unrestricted or bool(self.user and self.user.is_superuser)

    def can(self, scope: str) -> bool:
        return self.unrestricted or scope in self.scopes

    def __str__(self):
        return "local" if self.unrestricted else f"{getattr(self.user, 'username', '?')}/{self.role}"


def actor() -> "Actor":
    """Поточний викликач (для скоупінгу вибірок усередині інструментів)."""
    return _actor.get() or Actor.local()


class Tool:
    def __init__(self, name, fn, mutates, group, scope="", params=None):
        self.name, self.fn, self.mutates, self.group = name, fn, mutates, group
        # Описи параметрів ідуть у JSON-схему інструмента — це єдине, що модель
        # бачить про аргумент, окрім його імені й типу. Без них вона вгадує
        # (і, напр., пише пробіл там, де в TeleZip це АБО, а не І).
        self.param_docs = dict(params or {})
        # «Що для цього треба мати»: читання — усім, зміни — операторам,
        # створення — аналітикам (mcp:create), небезпечне (налаштування,
        # контейнери) — лише адмінам.
        self.scope = scope or (SCOPE_WRITE if mutates else SCOPE_READ)
        self.doc = inspect.getdoc(fn) or ""
        self.sig = inspect.signature(fn)

    @property
    def params(self):
        return list(self.sig.parameters)

    def __call__(self, payload: dict) -> str:
        unknown = set(payload) - set(self.params)
        if unknown:
            raise ToolError(f"невідомі параметри: {', '.join(sorted(unknown))}. "
                            f"Приймає: {', '.join(self.params) or '—'}")
        if self.mutates and readonly():
            raise ToolError(f"{self.name} змінює стан, а сервер запущено в режимі "
                            "лише-читання (MCP_READONLY=1)")
        who = actor()
        if not who.can(self.scope):
            raise ToolError(
                f"бракує прав: «{self.name}» потребує {self.scope}, а роль "
                f"«{who.role or 'без ролі'}» дає {', '.join(who.scopes) or 'нічого'}. "
                "Права змінює власник в адмінці (Ролі у MCP).")
        # право Django — те саме, що пускає в розділ адмінки (mcp_api/perms.py)
        from analysis.services.mcp_api import perms as _perms
        perm = _perms.required(self.name)
        if perm and not who.is_superuser and not (who.user and who.user.has_perm(perm)):
            raise ToolError(
                f"бракує права в адмінці: «{self.name}» потребує «{_perms.label(perm)}» "
                f"({perm}). Права дає власник групою користувача (/admin/auth/user/).")
        # None від host-шару = «параметр не передали» (MCP шле всі поля схеми)
        return self.fn(**{k: v for k, v in payload.items() if v is not None})


def charge(n_requests: int = 1) -> None:
    """Платний запит до TeleZip: звірити добову квоту користувача й зарахувати.

    Викликати ПЕРЕД самим запитом: перевищення ліміту має зупинити виклик, а не
    констатувати його постфактум. Локальний stdio без обмежень і без обліку.
    Стеля — на ролі (`McpRole.telezip_daily_limit`, 0 = Setting
    `mcp_telezip_daily_limit`); витрату видно в адмінці ролей і в аудиті.
    """
    who = actor()
    if not who.unrestricted:
        from mcpauth.models import McpRole
        from mcpauth.policy import telezip_daily_limit, telezip_used
        role = McpRole.objects.filter(user=who.user, is_active=True).first()
        limit = telezip_daily_limit(role)
        used = telezip_used(who.user) + _charged.get()
        if limit and used + n_requests > limit:
            raise ToolError(
                f"добовий ліміт TeleZip вичерпано: {used} з {limit} платних запитів "
                f"(≈${used * 0.10:.2f}). Ліміт піднімає власник в адмінці (Ролі у MCP, "
                "колонка «TeleZip: ліміт запитів/добу») або налаштуванням "
                "mcp_telezip_daily_limit.")
    _charged.set(_charged.get() + n_requests)


class NeedsConfirmation(ToolError):
    """Платний виклик без згоди людини: текст уже містить кошторис і код."""


CONFIRM_SETTING = "mcp_telezip_confirm"


def confirmation_required() -> bool:
    """Чи питати згоду перед платним викликом. Вимикається без деплою:
    `Setting mcp_telezip_confirm = 0`."""
    from analysis.models import Setting
    return str(Setting.get(CONFIRM_SETTING, "1")).strip().lower() not in ("0", "false", "no")


def require_confirmation(tool_name: str, payload: dict, *, n_requests: int = 1,
                         what: str = "") -> None:
    """Пропустити платний виклик лише зі згодою людини на ЦІ параметри.

    Гроші за TeleZip платить власник, а не той, хто питає, тож рішення «шукати»
    належить людині. Без `confirm` інструмент нічого не шукає (це безкоштовно),
    а віддає кошторис і одноразовий код; з правильним кодом — виконується.
    Код привʼязаний до інструмента Й параметрів, живе 15 хв і згорає після
    використання, тож «одна згода на сесію» не вийде.

    Викликати ПІСЛЯ розбору параметрів і ПЕРЕД `charge()`.
    """
    import hashlib
    import json
    from django.utils import timezone as djtz
    from mcpauth.models import McpPendingCall, new_secret

    if not confirmation_required():
        return
    who = actor()
    given = str(payload.get("confirm") or "").strip()
    body = {k: v for k, v in payload.items() if k != "confirm" and v not in (None, "", [], {})}
    digest = hashlib.sha256(
        json.dumps([tool_name, body], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    if given:
        exact = McpPendingCall.objects.filter(code=given, tool=tool_name,
                                              payload_hash=digest).first()
        if exact and exact.is_valid:
            McpPendingCall.objects.filter(pk=exact.pk).update(used_at=djtz.now())
            return
        if exact:
            why = "він уже використаний або протермінований (15 хв)"
        elif McpPendingCall.objects.filter(code=given).exists():
            why = "параметри пошуку змінилися — згода була на інші"
        else:
            why = "такого коду немає"
        raise NeedsConfirmation(
            f"код «{given}» не підходить: {why}. Попроси у людини згоду ще раз — "
            "виклич цей інструмент без confirm.")

    # Повторний виклик тих самих параметрів НЕ плодить новий код: модель, яка не
    # зрозуміла, що треба спитати людину, інакше крутиться в петлі й засмічує
    # журнал (бачили 5 однакових запитів підряд).
    pending = McpPendingCall.objects.filter(
        user=who.user, tool=tool_name, payload_hash=digest,
        used_at__isnull=True).order_by("-id").first()
    again = bool(pending and pending.is_valid)
    if again:
        code = pending.code
    else:
        code = new_secret(4)[:8]
        McpPendingCall.objects.create(user=who.user, tool=tool_name, payload=body,
                                     payload_hash=digest, code=code, requests=n_requests)
    raise NeedsConfirmation(
        ("ЗГОДИ ВСЕ ЩЕ НЕМА — я вже питав про цей самий пошук, код той самий. "
         "НЕ повторюй виклик: спитай людину й чекай відповіді.\n"
         if again else "ПОТРІБНА ЗГОДА ЛЮДИНИ — платний пошук ще НЕ виконано.\n")
        + f"інструмент : {tool_name}\n"
        + f"{'запит      : ' + what if what else 'параметри  : ' + ', '.join(f'{k}={v}' for k, v in body.items())}\n"
        + f"ціна       : {n_requests} × ≈$0.10 = ≈${n_requests * 0.10:.2f} (гроші власника)\n"
        + f"{_quota_line(who)}\n"
        + "Покажи це людині, спитай згоди і лише після її «так» повтори виклик "
        + f"з тими самими параметрами і confirm=\"{code}\" (код одноразовий, 15 хв). "
        "Сама собі згоду не вигадуй.")


def _quota_line(who) -> str:
    """Рядок «скільки платних запитів ще можна сьогодні» — щоб людина вирішувала
    із цифрою перед очима, а не наосліп."""
    if who.unrestricted or who.user is None:
        return "квота      : локальний режим, без ліміту"
    from mcpauth.models import McpRole
    from mcpauth.policy import telezip_daily_limit, telezip_used
    role = McpRole.objects.filter(user=who.user, is_active=True).first()
    limit = telezip_daily_limit(role)
    used = telezip_used(who.user)
    if not limit:
        return f"квота      : без ліміту; сьогодні вже {used} запитів"
    return (f"квота      : сьогодні {used} з {limit}; після цього лишиться "
            f"{max(0, limit - used - 1)}")


def readonly() -> bool:
    return os.environ.get("MCP_READONLY", "").strip().lower() in ("1", "true", "yes")


def tool(name, *, mutates=False, group="service", scope="", params=None):
    """Зареєструвати хендлер.

    `mutates=True` — інструмент змінює стан сервісу (і потребує mcp:write).
    `scope="mcp:admin"` — явно піднімає планку для небезпечного: налаштування,
    рестарт контейнерів, сирі виклики платного API.
    """
    def deco(fn):
        if name in TOOLS:
            raise RuntimeError(f"дубль інструмента MCP: {name}")
        TOOLS[name] = Tool(name, fn, mutates, group, scope, params)
        return fn
    return deco


def call(name: str, payload: dict | None = None, who: "Actor | None" = None) -> str:
    """Викликати інструмент від імені `who` і лишити слід в аудиті.

    Аудит пишеться і на відмовах — інакше «хто вимкнув джерело» лишається без
    відповіді. Локальні stdio-виклики (без користувача) не журналюємо: це
    власник на своїй машині, шуму більше, ніж користі.
    """
    started = time.monotonic()
    token = _actor.set(who) if who is not None else None
    charged = _charged.set(0)
    t = TOOLS.get(name)
    try:
        if not t:
            raise ToolError(f"невідомий інструмент: {name}. Є: {', '.join(sorted(TOOLS))}")
        text = t(payload or {})
        _audit(name, payload, who, True, "", started)
        return text
    except Exception as e:  # noqa: BLE001 — журнал важливіший за тип помилки
        _audit(name, payload, who, False, str(e)[:300], started)
        raise
    finally:
        _charged.reset(charged)
        if token is not None:
            _actor.reset(token)


def _audit(tool_name, payload, who, ok, error, started):
    if who is None or getattr(who, "unrestricted", False):
        return
    try:
        from mcpauth.models import McpAuditLog
        McpAuditLog.objects.create(
            user=who.user, role=who.role, tool=tool_name,
            payload={k: v for k, v in (payload or {}).items() if v is not None},
            ok=ok, error=error, client=who.client, paid_requests=_charged.get(),
            duration_ms=int((time.monotonic() - started) * 1000))
    except Exception:  # noqa: BLE001 — журнал не має права валити виклик
        pass


def _type_name(ann) -> str:
    """Ім'я типу з анотації — host-шар будує з нього JSON-схему інструмента."""
    if ann is inspect.Parameter.empty:
        return "str"
    return getattr(ann, "__name__", str(ann))


def manifest() -> list[dict]:
    """Опис інструментів для host-шару (звірка сигнатур у тестах/доках)."""
    from analysis.services.mcp_api import perms as _perms
    return [{
        "name": t.name, "group": t.group, "mutates": t.mutates, "scope": t.scope,
        "perm": _perms.required(t.name),
        # ПОВНИЙ докстрінг, а не перший абзац: саме він стає описом інструмента
        # в JSON-схемі, і саме там живуть застереження («пробіл = АБО»), без
        # яких модель складає хибні запити. `summary` — для компактних таблиць.
        "doc": t.doc,
        "summary": t.doc.split("\n\n")[0].replace("\n", " "),
        "params": [
            {"name": p.name,
             "type": _type_name(p.annotation),
             "doc": t.param_docs.get(p.name, ""),
             "default": None if p.default is inspect.Parameter.empty else p.default,
             "required": p.default is inspect.Parameter.empty}
            for p in t.sig.parameters.values()
        ],
    } for t in sorted(TOOLS.values(), key=lambda x: (x.group, x.name))]
