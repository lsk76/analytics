"""MCP-шар керування сервісом (акаунти, моніторинги, конвеєри).

Точка входу — `call(tool, payload) -> str`; реєстр наповнюють модулі нижче.
Транспорт до цього шару — `manage.py mcp_rpc`, host-сервер — `mcp/server.py`.
"""
from .registry import (SCOPE_ADMIN, SCOPE_CREATE, SCOPE_READ, SCOPE_WRITE, TOOLS, Actor,  # noqa: F401
                       NeedsConfirmation, Tool, ToolError, actor, call, manifest, readonly,
                       require_confirmation, tool)

from . import (service, accounts, monitor_try, monitoring, parsers, posts,  # noqa: E402,F401
               prompts, publish, tags, telegram, telezip, tgstat, channels)  # noqa: E402,F401  (реєструють інструменти)

__all__ = ["TOOLS", "Actor", "NeedsConfirmation", "Tool", "ToolError", "actor", "call",
           "manifest", "require_confirmation",
           "readonly", "tool", "SCOPE_READ", "SCOPE_WRITE", "SCOPE_CREATE", "SCOPE_ADMIN"]
