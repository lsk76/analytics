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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from .config import Config
from .session import ERROR, SNAPSHOT_JS, SessionState, classify

log = logging.getLogger(__name__)

# Cloudflare челенджить голий захід на сторінку, але пропускає візит «з пошуку».
REFERER = "https://www.google.com/"
_PROFILE_LOCKS = ("SingletonLock", "SingletonCookie", "SingletonSocket",
                  "DevToolsActivePort")


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

    @property
    def running(self) -> bool:
        return self._ctx is not None

    async def start(self) -> None:
        profile = Path(self.cfg.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        # Локи лишаються після kill контейнера — з ними Chrome не стартує.
        for name in _PROFILE_LOCKS:
            (profile / name).unlink(missing_ok=True)
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
        if self._ctx:
            await self._ctx.close()
        self._ctx = None
        if self._pw:
            await self._pw.stop()
        self._pw = None

    async def ensure(self) -> None:
        if self._ctx is None:
            if self._pw:
                await self._pw.stop()
                self._pw = None
            await self.start()

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
