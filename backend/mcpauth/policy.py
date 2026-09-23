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

# Усі скоупи, які сервер узагалі вміє. Клієнт (Claude) не знає наших ролей і
# просить те, що ми віддали йому при реєстрації, — тож просити він має ВСЕ, а
# звужує до ролі вже сервер (`granted_scopes`). Був тут лише `mcp:read` — і
# оператор отримував токен на саме читання, хоч роль давала більше.
ALL_SCOPES = ["mcp:read", "mcp:write", "mcp:create", "mcp:admin"]

SCOPE_LABELS = {
    "mcp:read": "бачити стан сервісу, задачі, акаунти й джерела (у межах твоєї видимості)",
    "mcp:write": "запускати збори та правити чати, джерела й акаунти",
    "mcp:create": "створювати дослідження, канали, джерела й акаунти",
    "mcp:admin": "змінювати налаштування, перезапускати воркери, звертатись до платного API",
}


def granted_scopes(user, requested) -> list[str]:
    """Перетин запитаного з тим, що дозволяє роль. Немає ролі — немає доступу.

    Клієнт може попросити `mcp:admin`, але читач отримає лише `mcp:read`:
    стеля завжди на боці сервера, не клієнта.
    """
    role = McpRole.objects.filter(user=user, is_active=True).first()
    if not role:
        return []
    allowed = role.scopes
    asked = [s for s in (requested or []) if s]
    if not asked:
        return allowed          # клієнт не звузив запит — даємо все, що дає роль
    return [s for s in allowed if s in asked]


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
