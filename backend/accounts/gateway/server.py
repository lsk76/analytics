"""HTTP-сервер gateway (aiohttp), лише внутрішня docker-мережа.

POST /accounts/{id}/{op}     тіло JSON = kwargs операції (+ "timeout" опц.)
                             → {"ok": true, "result": ...}
                             | {"ok": false, "error": {kind, message, retry_after, reason}}
POST /accounts/{id}/repair   → результат repair.repair
POST /accounts/{id}/replace_proxy  {"proxy_id": N}
POST /accounts/{id}/invalidate     (рядок змінено поза gateway)
GET  /health                 → стан пулу + RSS
Завжди HTTP 200 з ok: клієнт розрізняє «gateway недоступний» (нема відповіді)
і «операція не вдалась» (ok=false).
"""
from __future__ import annotations

import asyncio
import logging
import os
import resource
import signal

from aiohttp import web

from . import repair as rp
from .live import GatewayError
from .pool import AccountPool

logger = logging.getLogger("accounts.gateway.server")


def _err(e: GatewayError) -> web.Response:
    return web.json_response({"ok": False, "error": e.as_dict()})


def make_app(pool: AccountPool | None = None) -> web.Application:
    pool = pool or AccountPool()
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app["pool"] = pool

    async def health(request):
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return web.json_response({"ok": True, "pool": pool.stats(),
                                  "rss_mb": round(rss_kb / 1024, 1), "pid": os.getpid()})

    async def call(request):
        account_id = int(request.match_info["id"])
        op = request.match_info["op"]
        try:
            body = await request.json() if request.can_read_body else {}
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": {
                "kind": "internal", "message": "тіло не JSON", "retry_after": None,
                "reason": "bad_request"}})
        body = body or {}
        timeout = body.pop("timeout", None)
        live = pool.get(account_id)
        try:
            if op == "repair":
                return web.json_response({"ok": True, "result": await rp.repair(live)})
            if op == "replace_proxy":
                return web.json_response({"ok": True,
                                          "result": await rp.replace_proxy(live, int(body["proxy_id"]))})
            if op == "invalidate":
                await pool.invalidate(account_id)
                return web.json_response({"ok": True, "result": {"invalidated": account_id}})
            result = await live.call(op, body, timeout=float(timeout) if timeout else None)
            return web.json_response({"ok": True, "result": result})
        except GatewayError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001 — не валимо сервер через один виклик
            logger.exception("acc=#%s op=%s: %r", account_id, op, e)
            return web.json_response({"ok": False, "error": {
                "kind": "internal", "message": f"{type(e).__name__}: {str(e)[:300]}",
                "retry_after": None, "reason": "exception"}})

    app.router.add_get("/health", health)
    app.router.add_post("/accounts/{id:\\d+}/{op}", call)

    async def on_startup(app):
        pool.start_background()

    async def on_shutdown(app):
        await pool.shutdown()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


async def serve(host: str, port: int) -> None:
    app = make_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("tg-gateway слухає %s:%s", host, port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    logger.info("tg-gateway: зупинка…")
    await runner.cleanup()
