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

SCOPE_LABELS = {
    "mcp:read": "бачити стан сервісу, задачі, акаунти й джерела (у межах твоєї видимості)",
    "mcp:write": "запускати збори та правити чати, джерела й акаунти",
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
