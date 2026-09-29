"""Один постійний Chrome на весь сервіс.

Сесія TGStat прив'язана до профілю, IP виходу і збірки браузера, тому вхід
робиться руками В ЦЬОМУ Ж браузері (через noVNC на дисплеї Xvfb), а сервіс
далі працює з тим самим вікном. Окремого «логін-скрипта», який би бився з
сервісом за блокування профілю, немає.

Єдиний стан — каталог профілю (томом у docker); сам процес нічого не зберігає,
після рестарту контейнера сесія піднімається з профілю.
"""
import asyncio
import logging
import random
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from .config import Config
from .parse import check_restricted
from .session import ERROR, MANUAL, SNAPSHOT_JS, SessionState, classify

log = logging.getLogger(__name__)

# Cloudflare челенджить голий захід на сторінку, але пропускає візит «з пошуку».
REFERER = "https://www.google.com/"
_PROFILE_LOCKS = ("SingletonLock", "SingletonCookie", "SingletonSocket",
                  "DevToolsActivePort")
# Запит робить сама сторінка tgstat (fetch із її cookies і CSRF-токеном): сесія
# прив'язана до браузера, тож «зовнішній» HTTP-клієнт з тими ж cookies не пройде.
REQUEST_JS = """
async ({method, path, form}) => {
    const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    const opts = {method, credentials: 'include',
                  headers: {'X-Requested-With': 'XMLHttpRequest'}};
    if (method === 'POST') {
        const p = new URLSearchParams();
        p.set('_tgstat_csrk', csrf);
        for (const [k, v] of form) p.append(k, v);
        opts.headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8';
        opts.body = p.toString();
    }
    const r = await fetch(path, opts);
    return {http: r.status, text: await r.text(), csrf: !!csrf};
}
"""


class TgstatAuthError(Exception):
    """Сесія непридатна (Cloudflare, розлогін) — потрібен вхід через VNC."""


# Бінарник Google Chrome, який ставить `playwright install chrome`.
GOOGLE_CHROME = Path("/opt/google/chrome/chrome")


def parse_proxy(url: str) -> Optional[dict]:
    """http://user:pass@host:port -> налаштування проксі Playwright."""
    if not url:
        return None
    p = urlsplit(url if "://" in url else f"http://{url}")
    proxy = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        proxy["username"] = unquote(p.username)
    if p.password:
        proxy["password"] = unquote(p.password)
    return proxy


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Browser:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = asyncio.Lock()
        self._pw: Optional[Playwright] = None
        self._ctx: Optional[BrowserContext] = None
        # Звичайний Chrome для ручного входу (без Playwright/CDP) і його сторож.
        self._manual: Optional[asyncio.subprocess.Process] = None
        self._manual_watch: Optional[asyncio.Task] = None
        self._last_request = 0.0

    @property
    def running(self) -> bool:
        return self._ctx is not None

    @property
    def manual(self) -> bool:
        return self._manual is not None

    def _unlock_profile(self) -> Path:
        profile = Path(self.cfg.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        # Локи лишаються після kill контейнера — з ними Chrome не стартує.
        for name in _PROFILE_LOCKS:
            (profile / name).unlink(missing_ok=True)
        return profile

    async def start(self) -> None:
        profile = self._unlock_profile()
        if self._pw is None:
            self._pw = await async_playwright().start()
        launch: dict[str, Any] = dict(
            user_data_dir=str(profile),
            headless=False,  # headless міняє User-Agent і ламає кліренс Cloudflare
            timeout=60_000,
            no_viewport=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--window-position=0,0",
                f"--window-size={self.cfg.screen_w},{self.cfg.screen_h}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        proxy = parse_proxy(self.cfg.proxy)
        if proxy:
            launch["proxy"] = proxy
            log.info("проксі %s", proxy["server"])
        self._ctx = await self._launch(launch)
        self._ctx.set_default_timeout(self.cfg.timeout * 1000)
        await self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        self._ctx.on("close", lambda _: self._forget())
        page = await self.page()
        if not page.url.startswith(self.cfg.base_url):
            await self._open_home(page)
        log.info("Chrome готовий, профіль %s", profile)

    async def _launch(self, launch: dict) -> BrowserContext:
        assert self._pw is not None
        if self.cfg.channel:
            try:
                return await self._pw.chromium.launch_persistent_context(
                    **launch, channel=self.cfg.channel)
            except Exception as e:
                log.warning("Chrome %r недоступний (%s) — беру вбудований Chromium",
                            self.cfg.channel, str(e).splitlines()[0])
        return await self._pw.chromium.launch_persistent_context(**launch)

    def _forget(self) -> None:
        log.warning("Chrome закрився — підніму заново на наступному запиті")
        self._ctx = None

    async def stop(self) -> None:
        await self.finish_manual()
        if self._ctx:
            await self._ctx.close()
        self._ctx = None
        if self._pw:
            await self._pw.stop()
        self._pw = None

    async def ensure(self) -> None:
        if self.manual:
            raise RuntimeError("іде ручний вхід (звичайний Chrome) — "
                               "спершу POST /auth/manual/finish")
        if self._ctx is None:
            await self.start()

    # --- ручний вхід у звичайному Chrome -------------------------------------
    # Cloudflare і логін tgstat можуть поводитись інакше з браузером під
    # Playwright (прапорці автоматизації, CDP). На час входу відпускаємо профіль
    # і запускаємо той самий бінарник Chrome як звичайну програму: TLS-відбиток
    # і профіль ті самі, керування — лише людини через VNC. Коли вікно закрите
    # (або /auth/manual/finish), сервіс знову бере профіль уже з сесією.

    def _manual_binary(self) -> str:
        if self.cfg.channel == "chrome" and GOOGLE_CHROME.exists():
            return str(GOOGLE_CHROME)
        assert self._pw is not None
        return self._pw.chromium.executable_path

    async def start_manual(self) -> None:
        async with self.lock:
            if self.manual:
                return
            if self._pw is None:
                self._pw = await async_playwright().start()
            if self._ctx:
                ctx, self._ctx = self._ctx, None
                await ctx.close()
            profile = self._unlock_profile()
            args = [
                self._manual_binary(),
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--window-position=0,0",
                f"--window-size={self.cfg.screen_w},{self.cfg.screen_h}",
                # Контейнер працює від root, а root-Chrome без цього не стартує.
                "--no-sandbox",
            ]
            proxy = parse_proxy(self.cfg.proxy)
            if proxy:
                # Chrome не бере логін/пароль проксі з прапорця — лише сервер.
                args.append(f"--proxy-server={proxy['server']}")
            args.append(f"{self.cfg.base_url}/")
            self._manual = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            self._manual_watch = asyncio.create_task(self._watch_manual())
            log.info("ручний вхід: звичайний Chrome pid=%s", self._manual.pid)

    async def _watch_manual(self) -> None:
        proc = self._manual
        assert proc is not None
        await proc.wait()
        log.info("ручний Chrome закрито (код %s) — повертаю Playwright", proc.returncode)
        async with self.lock:
            self._manual = None
            try:
                await self.start()
            except Exception:
                log.exception("Chrome під Playwright не стартував після ручного входу")

    async def finish_manual(self) -> None:
        """Чемно закрити ручний Chrome: SIGTERM дає йому зберегти cookies."""
        proc, watch = self._manual, self._manual_watch
        if proc is None:
            return
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 20)
            except asyncio.TimeoutError:
                proc.kill()
        if watch:
            await watch

    async def page(self) -> Page:
        """Вкладка tgstat (якщо відкрита), інакше перша."""
        assert self._ctx is not None
        pages = [p for p in self._ctx.pages if not p.is_closed()]
        for p in pages:
            if p.url.startswith(self.cfg.base_url):
                return p
        return pages[0] if pages else await self._ctx.new_page()

    async def _open_home(self, page: Page) -> None:
        await page.goto(f"{self.cfg.base_url}/", referer=REFERER,
                        wait_until="domcontentloaded")

    async def _wait_cloudflare(self, page: Page) -> None:
        """Челендж зазвичай проходить сам за кілька секунд — даємо йому час."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.cloudflare_wait
        while loop.time() < deadline:
            title = (await self._title(page)).lower()
            if "just a moment" not in title:
                return
            await page.wait_for_timeout(1500)

    async def _title(self, page: Page) -> str:
        # Під час навігації контекст сторінки знищується — просто чекаємо.
        for _ in range(20):
            try:
                return await page.title()
            except Exception as e:
                msg = str(e).lower()
                if "destroyed" not in msg and "navigat" not in msg:
                    raise
                await asyncio.sleep(0.2)
        return ""

    async def check(self, reload: bool) -> SessionState:
        """Стан сесії. reload=True — перезайти на головну (оновлює кліренс
        Cloudflare); False — лише подивитись на поточну сторінку, не заважаючи
        входу, що саме йде у VNC."""
        if self.manual:
            return SessionState(MANUAL, checked_at=_now(), detail=(
                "іде ручний вхід у звичайному Chrome; після входу закрий вікно "
                "або POST /auth/manual/finish"))
        async with self.lock:
            try:
                await self.ensure()
                page = await self.page()
                if reload or not page.url.startswith(self.cfg.base_url):
                    await self._open_home(page)
                await self._wait_cloudflare(page)
                snap = await page.evaluate(SNAPSHOT_JS)
                state = classify(snap)
            except Exception as e:
                log.exception("перевірка сесії впала")
                state = SessionState(ERROR, detail=f"{type(e).__name__}: {e}"[:300])
            state.checked_at = _now()
            return state

    async def open_login(self) -> str:
        """Вивести tgstat на передній план — далі людина логіниться у VNC."""
        async with self.lock:
            await self.ensure()
            page = await self.page()
            await self._open_home(page)
            await page.bring_to_front()
            return page.url

    async def screenshot(self) -> bytes:
        async with self.lock:
            await self.ensure()
            page = await self.page()
            return await page.screenshot(type="png")

    async def request(self, method: str, path: str,
                      form: Optional[list[tuple[str, str]]] = None) -> str:
        """Запит до tgstat від імені залогіненої сторінки. Повертає тіло.

        GET — звичайна навігація вкладки (як людина відкриває сторінку),
        POST — AJAX зі сторінки з її CSRF, як це робить сам сайт.
        Запити йдуть по одному (лок) і не частіше за request_delay з випадковою
        добавкою: на частоту tgstat відповідає 429 і reCAPTCHA.
        """
        async with self.lock:
            await self.ensure()
            page = await self.page()
            await self._pace()
            try:
                if method == "GET":
                    text = await self._goto(page, path)
                else:
                    text = await self._post(page, path, form or [])
            finally:
                self._last_request = asyncio.get_running_loop().time()
            check_restricted(text)
            return text

    async def _pace(self) -> None:
        delay = self.cfg.request_delay * (1 + random.random() / 2)
        pause = self._last_request + delay - asyncio.get_running_loop().time()
        if pause > 0:
            await asyncio.sleep(pause)

    async def _goto(self, page: Page, path: str) -> str:
        referer = page.url if page.url.startswith(self.cfg.base_url) else REFERER
        resp = await page.goto(f"{self.cfg.base_url}{path}", referer=referer,
                               wait_until="domcontentloaded")
        await self._wait_cloudflare(page)
        status = resp.status if resp else 0
        text = await page.content()
        if "just a moment" in (await self._title(page)).lower():
            raise TgstatAuthError("Cloudflare не пропускає — потрібен вхід через VNC")
        if status >= 400 and status != 429:
            raise RuntimeError(f"tgstat {status} на GET {path}")
        return text

    async def _post(self, page: Page, path: str, form: list) -> str:
        if not page.url.startswith(self.cfg.base_url):
            await self._open_home(page)
            await self._wait_cloudflare(page)
        args = {"method": "POST", "path": path, "form": form}
        res = await page.evaluate(REQUEST_JS, args)
        if not res["csrf"]:
            # Сторінка без токена (челендж, помилка) — одна спроба оновитись.
            await self._open_home(page)
            await self._wait_cloudflare(page)
            res = await page.evaluate(REQUEST_JS, args)
        text = res["text"] or ""
        if res["http"] in (401, 403) or "just a moment" in text[:3000].lower():
            raise TgstatAuthError(
                f"tgstat відповів {res['http']} на {path}: сесія непридатна, "
                "потрібен вхід (docs/tgstat-service.md)")
        if res["http"] >= 400 and res["http"] != 429:
            raise RuntimeError(f"tgstat {res['http']} на POST {path}: {text[:200]}")
        return text
