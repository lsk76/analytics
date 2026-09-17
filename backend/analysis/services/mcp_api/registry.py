"""Реєстр інструментів MCP-шару.

Уся логіка живе ТУТ, у Django (де є ORM, Telethon-клієнти й стадії), а не в
host-процесі MCP: той лише транспорт (`manage.py mcp_rpc`, див. mcp/README.md).
Тож будь-який інструмент можна викликати й руками:

    docker compose exec -T web python manage.py mcp_rpc accounts_list

Хендлер повертає ГОТОВИЙ ТЕКСТ (див. `fmt`), а не структуру: це кінцева
відповідь моделі, а не проміжний формат.
"""
import inspect
import os

TOOLS = {}


class ToolError(Exception):
    """Очікувана помилка інструмента (не баг): показуємо текст як є."""


class Tool:
    def __init__(self, name, fn, mutates, group):
        self.name, self.fn, self.mutates, self.group = name, fn, mutates, group
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
        # None від host-шару = «параметр не передали» (MCP шле всі поля схеми)
        return self.fn(**{k: v for k, v in payload.items() if v is not None})


def readonly() -> bool:
    return os.environ.get("MCP_READONLY", "").strip().lower() in ("1", "true", "yes")


def tool(name, *, mutates=False, group="service"):
    """Зареєструвати хендлер. `mutates=True` — інструмент змінює стан сервісу."""
    def deco(fn):
        if name in TOOLS:
            raise RuntimeError(f"дубль інструмента MCP: {name}")
        TOOLS[name] = Tool(name, fn, mutates, group)
        return fn
    return deco


def call(name: str, payload: dict | None = None) -> str:
    t = TOOLS.get(name)
    if not t:
        raise ToolError(f"невідомий інструмент: {name}. Є: {', '.join(sorted(TOOLS))}")
    return t(payload or {})


def manifest() -> list[dict]:
    """Опис інструментів для host-шару (звірка сигнатур у тестах/доках)."""
    return [{
        "name": t.name, "group": t.group, "mutates": t.mutates,
        "doc": t.doc.split("\n\n")[0],
        "params": [
            {"name": p.name,
             "default": None if p.default is inspect.Parameter.empty else p.default,
             "required": p.default is inspect.Parameter.empty}
            for p in t.sig.parameters.values()
        ],
    } for t in sorted(TOOLS.values(), key=lambda x: (x.group, x.name))]
