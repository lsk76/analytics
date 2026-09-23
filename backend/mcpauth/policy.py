"""Правила доступу, спільні для Django-веба й MCP-процесу.

Живуть окремо від `oauth.py` навмисне: сторінка згоди й адмінка не мають
залежати від пакета `mcp` (він потрібен лише самому MCP-серверу). Один
помилковий імпорт тут — і веб не підніметься взагалі.
"""
from datetime import timedelta

from .models import McpRole

ACCESS_TTL = timedelta(hours=8)      # робочий день; далі — refresh
REFRESH_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=5)
REQUEST_TTL = timedelta(minutes=15)  # скільки людина має на логін і згоду

SCOPE_READ = "mcp:read"
# Усі скоупи, які сервер узагалі вміє (valid_scopes). Реєструємо клієнта лише
# з `mcp:read`: у його запиті не має світитися `mcp:admin`.
ALL_SCOPES = [SCOPE_READ, "mcp:write", "mcp:create", "mcp:admin"]

SCOPE_LABELS = {
    "mcp:read": "бачити стан сервісу, задачі, акаунти й джерела (у межах твоєї видимості)",
    "mcp:write": "запускати збори та правити чати, джерела й акаунти",
    "mcp:create": "створювати дослідження, канали, джерела й акаунти",
    "mcp:admin": "змінювати налаштування, перезапускати воркери, звертатись до платного API",
}


# Наші застосунки: право з чужого (напр. auth.view_user) скоупів не дає.
OUR_APPS = ("analysis", "accounts")


def scopes_for(user, max_scope: str = "") -> list[str]:
    """Скоупи користувача, виведені з його прав Django.

    Окремої «ролі MCP» немає: що людина може в адмінці, те саме їй можна через
    асистента. Правило просте — дія в назві права стає скоупом:

        view_*            → mcp:read
        change_*/delete_* → mcp:write
        add_*             → mcp:create
        суперюзер або change_setting → mcp:admin

    `mcp:admin` окремо, бо ним закриті речі, яких із конкретного права не
    вивести: глобальні налаштування й надсилання повідомлень від імені
    акаунта (спам-ризик). `max_scope` — необовʼязкова стеля ЗВУЖЕННЯ
    (`McpRole.max_scope`): розширити нею не можна.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    if user.is_superuser:
        return _cap(list(ALL_SCOPES), max_scope)
    actions = {p.split(".", 1)[1].split("_", 1)[0]
               for p in user.get_all_permissions()
               if p.split(".", 1)[0] in OUR_APPS and "_" in p.split(".", 1)[1]}
    scopes = []
    if actions:                                   # будь-яке наше право = доступ на читання
        scopes.append(SCOPE_READ)
    if {"change", "delete"} & actions:
        scopes.append("mcp:write")
    if "add" in actions:
        scopes.append("mcp:create")
    if user.has_perm("analysis.change_setting"):
        scopes.append("mcp:admin")
    return _cap(scopes, max_scope)


def _cap(scopes: list[str], max_scope: str) -> list[str]:
    """Зрізати набір стелею: `mcp:write` лишає read+write, і т.д."""
    if not max_scope:
        return scopes
    keep = ALL_SCOPES[:ALL_SCOPES.index(max_scope) + 1] if max_scope in ALL_SCOPES else [SCOPE_READ]
    return [s for s in scopes if s in keep]


def granted_scopes(user, requested, registered=None) -> list[str]:
    """Скоупи токена: стеля — права користувача, звуження — лише СВІДОМЕ.

    Немає рядка `McpRole` (або він вимкнений) — немає доступу взагалі, навіть
    якщо прав в адмінці повно: мережевий MCP допускають окремо.

    `registered` — те, що ми самі видали клієнту при реєстрації
    (`McpClient.scope`, за замовчуванням `mcp:read`). Клієнт типу Claude просто
    повторює цей рядок — це не «прошу лише читання», а «прошу як домовились»,
    тож такий запит стелю не зрізає. Якщо ж клієнт попросив ВУЖЧЕ за видане —
    це свідомий вибір, і ми його поважаємо.
    """
    row = McpRole.objects.filter(user=user, is_active=True).first()
    if not row:
        return []
    allowed = row.scopes
    asked = [s for s in (requested or []) if s]
    given = [s for s in (registered or []) if s] or [SCOPE_READ]
    if not asked or set(asked) >= set(given):
        return allowed          # клієнт не звужував — даємо все, що дають права
    return [s for s in allowed if s in asked]


def scope_summary(scopes) -> str:
    """Компактний слід для аудиту: ['mcp:read','mcp:write'] → 'rw'."""
    return "".join(s.split(":")[1][0] for s in scopes) or "—"


# --------------------------------------------------------------------------- TeleZip
# Один виклик TeleZip ≈ $0.10, і платить власник, а не той, хто питає. Тому
# платні запити рахуються на користувача за добу (за paid_requests в аудиті),
# стеля — на ролі (0 = спільний дефолт із Setting).
TELEZIP_PAID_TOOLS = ("tz_find", "tz_channels", "tz_users")
TELEZIP_DEFAULT_DAILY_LIMIT = 30
TELEZIP_LIMIT_SETTING = "mcp_telezip_daily_limit"


def telezip_daily_limit(role) -> int:
    """Стеля платних запитів/добу для ролі. 0 = без ліміту."""
    if role and role.telezip_daily_limit:
        return int(role.telezip_daily_limit)
    from analysis.models import Setting
    raw = Setting.get(TELEZIP_LIMIT_SETTING, str(TELEZIP_DEFAULT_DAILY_LIMIT))
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return TELEZIP_DEFAULT_DAILY_LIMIT


def telezip_used(user, days: int = 1) -> int:
    """Скільки платних запитів зробив користувач за останні `days` діб (сьогодні = 1)."""
    from django.db.models import Sum
    from django.utils import timezone
    from .models import McpAuditLog
    start = timezone.localdate() - timedelta(days=days - 1)
    agg = McpAuditLog.objects.filter(user=user, created_at__date__gte=start) \
        .aggregate(n=Sum("paid_requests"))
    return int(agg["n"] or 0)
