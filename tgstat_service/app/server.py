"""HTTP-API сервісу tgstat (внутрішня мережа compose, порт 8020).

Дані (кожна сторінка результатів = запит до tgstat, темп обмежено):
  GET  /channels/search    пошук каналів за словами (?q, in_about, min_subs, …)
  GET  /catalog/tags       список підбірок (?kind=geo|theme)
  GET  /catalog/{tag}      канали/чати підбірки (?kind=channel|chat) — єдиний шлях до чатів
  GET  /channel/{ref}      картка каналу/чату (ref: @h, t.me/h, url tgstat)
  GET  /posts/search       пошук публікацій, Premium (?q, from, to, peer_type, …)
  GET  /links/{ref}        посилання tgstat/t.me без запиту (?post_id, kind)
  GET|POST /raw            сире тіло відповіді tgstat — діагностика парсерів

Сесія:
  GET  /health             процес живий, чи піднятий Chrome
  GET  /selftest           чи ще працює розбір живого tgstat (5 запитів, раз на добу)
  GET  /auth/status        стан сесії (?reload=1 — перезайти на сайт)
  POST /auth/login         вивести tgstat у вікно VNC для ручного входу
  GET  /auth/screenshot    PNG поточної вкладки — глянути без VNC
  POST /auth/manual        ручний вхід у ЗВИЧАЙНОМУ Chrome (без Playwright)
  POST /auth/manual/finish закрити його і повернути Chrome під Playwright

Сервіс не має БД і не тримає стану між запитами: усе, що переживає рестарт, —
профіль Chrome у томі (docs/tgstat-service.md).
"""
import asyncio
import functools
import json
import logging

from aiohttp import web

from . import ops, selftest
from .browser import Browser, TgstatAuthError
from .config import Config
from .parse import Restricted
from .session import CAPTCHA, LOGIN_REQUIRED, MANUAL, OK

log = logging.getLogger("tgstat")

# Кирилиця в JSON — як є, а не \uXXXX: відповіді читає людина.
_json = functools.partial(web.json_response, dumps=functools.partial(
    json.dumps, ensure_ascii=False))

LOGIN_HOW = (
    "Відкрий екран браузера: ssh -N -L 6080:127.0.0.1:6080 tg-analytics, "
    "далі http://localhost:6080/vnc.html (пароль — TGSTAT_VNC_PASSWORD з .env). "
    "Увійди на tgstat у цьому вікні; готовність — GET /auth/status (state=ok)."
)


@web.middleware
async def errors(request: web.Request, handler):
    """Помилки tgstat -> зрозумілі відповіді: 503 зі state, коли потрібна людина."""
    try:
        return await handler(request)
    except Restricted as e:
        return _json({"error": str(e), "state": CAPTCHA, "how_to_login": LOGIN_HOW},
                     status=503)
    except TgstatAuthError as e:
        return _json({"error": str(e), "state": LOGIN_REQUIRED,
                      "how_to_login": LOGIN_HOW}, status=503)
    except ValueError as e:
        return _json({"error": str(e)}, status=400)
    except LookupError as e:
        return _json({"error": str(e)}, status=404)
    except RuntimeError as e:
        if request.app["browser"].manual:
            return _json({"error": str(e), "state": MANUAL}, status=409)
        raise


def _q(request: web.Request, name: str, default=None, cast=str):
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.lower() in ("1", "true", "yes", "on")
    try:
        return cast(raw)
    except ValueError:
        raise ValueError(f"{name}: погане значення {raw!r}")


async def channels_search(request: web.Request) -> web.Response:
    return _json(await ops.search_channels(
        request.app["browser"], _q(request, "q", ""),
        in_about=_q(request, "in_about", False, bool),
        min_subs=_q(request, "min_subs", None, int),
        max_subs=_q(request, "max_subs", None, int),
        country=_q(request, "country", "Россия"),
        category=_q(request, "category"), language=_q(request, "language"),
        sort=_q(request, "sort", "participants"),
        limit=_q(request, "limit", 100, int),
        max_pages=_q(request, "max_pages", 3, int)))


async def catalog_tags(request: web.Request) -> web.Response:
    return _json(await ops.tags(request.app["browser"], _q(request, "kind", "geo")))


async def catalog_items(request: web.Request) -> web.Response:
    return _json(await ops.catalog(
        request.app["browser"], request.match_info["tag"],
        kind=_q(request, "kind", "channel"),
        category_id=_q(request, "category_id", 0, int),
        limit=_q(request, "limit", 200, int),
        max_pages=_q(request, "max_pages", 5, int)))


async def channel_card(request: web.Request) -> web.Response:
    return _json(await ops.channel(request.app["browser"], request.match_info["ref"],
                                   kind=_q(request, "kind")))


async def posts_search(request: web.Request) -> web.Response:
    return _json(await ops.search_posts(
        request.app["browser"], _q(request, "q", ""),
        date_from=_q(request, "from"), date_to=_q(request, "to"),
        peer_type=_q(request, "peer_type", "all"), sort=_q(request, "sort", "date"),
        hide_forwards=_q(request, "hide_forwards", False, bool),
        strong=_q(request, "strong", False, bool),
        extended=_q(request, "extended", False, bool),
        minus_words=_q(request, "minus_words", ""),
        limit=_q(request, "limit", 100, int),
        max_pages=_q(request, "max_pages", 3, int)))


async def peer_links(request: web.Request) -> web.Response:
    cfg: Config = request.app["cfg"]
    return _json(ops.links(request.match_info["ref"], kind=_q(request, "kind"),
                           post_id=_q(request, "post_id", None, int),
                           base=cfg.base_url))


async def selftest_run(request: web.Request) -> web.Response:
    """Живий самоконтроль розбору: 5 запитів до tgstat, раз на добу (капча!).

    200 + verdict=ok — розмітка на місці; 200 + verdict=broken — щось
    розбирається порожньо (деталі в checks); 503 + verdict=unverified —
    сесія непридатна, тобто НЕ перевірено (не плутати з «зламалось»).
    """
    res = await selftest.run(request.app["browser"], only=_q(request, "only", ""))
    status = 503 if res["verdict"] == selftest.VERDICT_UNVERIFIED else 200
    if status == 503:
        res["how_to_login"] = LOGIN_HOW
    return _json(res, status=status)


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


async def raw(request: web.Request) -> web.Response:
    """Сире тіло відповіді tgstat — для діагностики парсерів.
    GET ?path=/channel/@x або POST {"path": ..., "form": [[k, v], ...]}."""
    browser: Browser = request.app["browser"]
    if request.method == "GET":
        method, path, form = "GET", request.query.get("path", ""), None
    else:
        body = await request.json()
        method, path, form = "POST", body.get("path", ""), body.get("form") or []
    if not path.startswith("/") or "logout" in path or "payments" in path:
        raise web.HTTPBadRequest(text="path: лише відносний шлях tgstat")
    text = await browser.request(method, path, form)
    return web.Response(text=text, content_type="text/plain")


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
    app = web.Application(middlewares=[errors])
    app["cfg"] = cfg
    app["browser"] = Browser(cfg)
    app.router.add_get("/health", health)
    app.router.add_get("/selftest", selftest_run)
    app.router.add_get("/auth/status", auth_status)
    app.router.add_post("/auth/login", auth_login)
    app.router.add_get("/auth/screenshot", auth_screenshot)
    app.router.add_get("/channels/search", channels_search)
    app.router.add_get("/catalog/tags", catalog_tags)
    app.router.add_get("/catalog/{tag}", catalog_items)
    app.router.add_get("/channel/{ref:.+}", channel_card)
    app.router.add_get("/posts/search", posts_search)
    app.router.add_get("/links/{ref:.+}", peer_links)
    app.router.add_get("/raw", raw)
    app.router.add_post("/raw", raw)
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
