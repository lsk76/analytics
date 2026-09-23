"""MCP-шар керування сервісом (акаунти, моніторинги, конвеєри).

Точка входу — `call(tool, payload) -> str`; реєстр наповнюють модулі нижче.
Транспорт до цього шару — `manage.py mcp_rpc`, host-сервер — `mcp/server.py`.
"""
from .registry import (SCOPE_ADMIN, SCOPE_CREATE, SCOPE_READ, SCOPE_WRITE, TOOLS, Actor, Tool,  # noqa: F401
                       ToolError, actor, call, manifest, readonly, tool)

from . import service, accounts, monitoring, publish, telegram, telezip  # noqa: E402,F401  (реєструють інструменти)

__all__ = ["TOOLS", "Actor", "Tool", "ToolError", "actor", "call", "manifest",
           "readonly", "tool", "SCOPE_READ", "SCOPE_WRITE", "SCOPE_CREATE", "SCOPE_ADMIN"]
