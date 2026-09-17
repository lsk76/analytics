"""RPC-міст MCP: викликає інструмент `analysis.services.mcp_api` і друкує результат.

Host-процес MCP (`mcp/server.py`) не має ні ORM, ні Telethon — він лише
транспорт: `docker compose exec -T web python manage.py mcp_rpc <tool>` з
JSON-параметрами у stdin. Результат обгортається маркерами, щоб випадковий
`print` зі стороннього коду не зламав розбір.

Людині теж зручно:

    docker compose exec -T web python manage.py mcp_rpc --list
    docker compose exec -T web python manage.py mcp_rpc service_health --raw
    echo '{"ref":"7"}' | docker compose exec -T web python manage.py mcp_rpc account_show
"""
import json
import sys
import traceback

from django.core.management.base import BaseCommand

MARK_BEGIN = "<<<MCP-RESULT-BEGIN>>>"
MARK_END = "<<<MCP-RESULT-END>>>"


class Command(BaseCommand):
    help = "Викликати інструмент MCP-шару керування сервісом"

    def add_arguments(self, parser):
        parser.add_argument("tool", nargs="?", default="",
                            help="назва інструмента (без неї — список)")
        parser.add_argument("--payload", default="",
                            help="параметри JSON-об'єктом (інакше читаються зі stdin)")
        parser.add_argument("--list", action="store_true",
                            help="манифест інструментів у JSON")
        parser.add_argument("--raw", action="store_true",
                            help="без маркерів/JSON — просто текст (для людини)")

    def handle(self, *args, **opts):
        from analysis.services import mcp_api

        if opts["list"] or not opts["tool"]:
            manifest = mcp_api.manifest()
            if opts["raw"]:
                self.stdout.write(mcp_api.call("tools_manifest"))
            else:
                self._emit({"ok": True, "manifest": manifest}, opts)
            return

        payload = self._payload(opts)
        if payload is None:
            return
        try:
            text = mcp_api.call(opts["tool"], payload)
            result = {"ok": True, "text": text}
        except mcp_api.ToolError as e:
            result = {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 — падіння інструмента не має валити транспорт
            result = {"ok": False, "error": f"{type(e).__name__}: {e}",
                      "traceback": traceback.format_exc()[-2000:]}
        self._emit(result, opts)

    # ---- внутрішнє ---------------------------------------------------------

    def _payload(self, opts):
        raw = opts["payload"]
        if not raw and not sys.stdin.isatty():
            raw = sys.stdin.read()
        raw = (raw or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self._emit({"ok": False, "error": f"payload не JSON: {e}"}, opts)
            return None
        if not isinstance(data, dict):
            self._emit({"ok": False, "error": "payload має бути JSON-об'єктом"}, opts)
            return None
        return data

    def _emit(self, result, opts):
        if opts["raw"]:
            out = result.get("text") or result.get("error") or json.dumps(
                result.get("manifest", result), ensure_ascii=False, indent=2)
            self.stdout.write(out)
            if not result.get("ok"):
                sys.exit(1)
            return
        self.stdout.write(MARK_BEGIN)
        self.stdout.write(json.dumps(result, ensure_ascii=False))
        self.stdout.write(MARK_END)
