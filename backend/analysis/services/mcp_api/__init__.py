"""MCP-шар керування сервісом (акаунти, моніторинги, конвеєри).

Точка входу — `call(tool, payload) -> str`; реєстр наповнюють модулі нижче.
Транспорт до цього шару — `manage.py mcp_rpc`, host-сервер — `mcp/server.py`.
"""
from .registry import TOOLS, Tool, ToolError, call, manifest, readonly, tool  # noqa: F401

from . import service, accounts, monitoring, telezip  # noqa: E402,F401  (реєструють інструменти)

__all__ = ["TOOLS", "Tool", "ToolError", "call", "manifest", "readonly", "tool"]
