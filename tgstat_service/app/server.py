"""HTTP-API сервісу tgstat (внутрішня мережа compose, порт 8020).

Етап 1 — лише авторизація:
  GET  /health             процес живий, чи піднятий Chrome
  GET  /auth/status        стан сесії (?reload=1 — перезайти на сайт)
  POST /auth/login         вивести tgstat у вікно VNC для ручного входу
  GET  /auth/screenshot    PNG поточної вкладки — глянути без VNC
  POST /auth/manual        ручний вхід у ЗВИЧАЙНОМУ Chrome (без Playwright)
  POST /auth/manual/finish закрити його і повернути Chrome під Playwright

Сервіс не має БД і не тримає стану між запитами: усе, що переживає рестарт, —
профіль Chrome у томі. Пошук/збір додаються наступними етапами поверх того ж
Browser (docs/tgstat-service.md).
"""
import asyncio
import functools
import json
import logging

from aiohttp import web

from .browser import Browser
from .config import Config
from .session import OK

log = logging.getLogger("tgstat")

# Кирилиця в JSON — як є, а не \uXXXX: відповіді читає людина.
_json = functools.partial(web.json_response, dumps=functools.partial(
    json.dumps, ensure_ascii=False))

LOGIN_HOW = (
    "Відкрий екран браузера: ssh -N -L 6080:127.0.0.1:6080 tg-analytics, "
    "далі http://localhost:6080/vnc.html (пароль — TGSTAT_VNC_PASSWORD з .env). "
    "Увійди на tgstat у цьому вікні; готовність — GET /auth/status (state=ok)."
)


async def health(request: web.Request) -> web.Response:
    browser: Browser = request.app["browser"]
    return _json({"ok": True, "browser": browser.running, "manual": browser.manual})


async def auth_status(request: web.Request) -> web.Response:
    browser: Browser = request.app["browser"]
    reload = request.query.get("reload", "") in ("1", "true", "yes")
    state = await browser.check(reload=reload)
    body = state.as_dict()
    if not state.usable:
        body["how_to_login"] = LOGIN_HOW
    return _json(body)


async def auth_login(request: web.Request) -> web.Response:
    browser: Browser = request.app["browser"]
    url = await browser.open_login()
    return _json({"opened": url, "how_to_login": LOGIN_HOW})


async def auth_manual(request: web.Request) -> web.Response:
    browser: Browser = request.app["browser"]
    await browser.start_manual()
    return _json({"manual": True, "how_to_login": LOGIN_HOW + (
        " Після входу закрий вкладку Chrome (або POST /auth/manual/finish) — "
        "сервіс сам повернеться до роботи з цим профілем.")})


async def auth_manual_finish(request: web.Request) -> web.Response:
    browser: Browser = request.app["browser"]
    await browser.finish_manual()
    state = await browser.check(reload=True)
    return _json(state.as_dict())


async def auth_screenshot(request: web.Request) -> web.Response:
    browser: Browser = request.app["browser"]
    return web.Response(body=await browser.screenshot(), content_type="image/png")


async def keepalive(app: web.Application) -> None:
    """Живу сесію періодично оновлюємо (кліренс Cloudflare протухає в простої).
    Неживу лише спостерігаємо, не перезавантажуючи сторінку: людина в цей
    момент може саме логінитись у VNC."""
    cfg: Config = app["cfg"]
    browser: Browser = app["browser"]
    last = None
    while True:
        await asyncio.sleep(cfg.keepalive)
        state = await browser.check(reload=last == OK)
        if state.state != last:
            log.warning("сесія tgstat: %s -> %s %s", last, state.state, state.detail)
        last = state.state


async def on_startup(app: web.Application) -> None:
    try:
        await app["browser"].start()
    except Exception:
        # Не валимо процес: /health скаже browser=false, наступний запит спробує ще.
        log.exception("Chrome не стартував")
    if app["cfg"].keepalive > 0:
        app["keepalive"] = asyncio.create_task(keepalive(app))


async def on_cleanup(app: web.Application) -> None:
    task = app.get("keepalive")
    if task:
        task.cancel()
    await app["browser"].stop()


def make_app(cfg: Config) -> web.Application:
    app = web.Application()
    app["cfg"] = cfg
    app["browser"] = Browser(cfg)
    app.router.add_get("/health", health)
    app.router.add_get("/auth/status", auth_status)
    app.router.add_post("/auth/login", auth_login)
    app.router.add_get("/auth/screenshot", auth_screenshot)
    app.router.add_post("/auth/manual", auth_manual)
    app.router.add_post("/auth/manual/finish", auth_manual_finish)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    web.run_app(make_app(cfg), host="0.0.0.0", port=cfg.port, access_log=None)


if __name__ == "__main__":
    main()
