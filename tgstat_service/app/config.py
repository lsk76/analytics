import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    base_url: str = field(default_factory=lambda: _env(
        "TGSTAT_BASE_URL", "https://tgstat.ru").rstrip("/"))
    profile_dir: str = field(default_factory=lambda: _env(
        "TGSTAT_PROFILE", "/data/profile"))
    # "chrome" — Google Chrome (є в образі лише на amd64), "" — вбудований
    # Chromium. Зміна збірки = новий TLS-відбиток = перелогін.
    channel: str = field(default_factory=lambda: _env("TGSTAT_CHANNEL", "chrome"))
    proxy: str = field(default_factory=lambda: _env("TGSTAT_PROXY", ""))
    port: int = field(default_factory=lambda: int(_env("TGSTAT_PORT", "8020")))
    timeout: int = field(default_factory=lambda: int(_env("TGSTAT_TIMEOUT", "60")))
    cloudflare_wait: int = field(default_factory=lambda: int(_env(
        "TGSTAT_CLOUDFLARE_WAIT", "30")))
    # Раз на стільки секунд перезаходимо на сайт, щоб кліренс Cloudflare не
    # протух у простої. 0 — вимкнено.
    keepalive: int = field(default_factory=lambda: int(_env(
        "TGSTAT_KEEPALIVE", "1800")))
    # Мінімальна пауза між запитами до tgstat, с (+ до 50% випадково). При 1.5 с
    # tgstat уже за ~10 запитів відповів 429 «Подозрение на робота».
    request_delay: float = field(default_factory=lambda: float(_env(
        "TGSTAT_REQUEST_DELAY", "4")))
    screen_w: int = field(default_factory=lambda: int(_env("SCREEN_W", "1366")))
    screen_h: int = field(default_factory=lambda: int(_env("SCREEN_H", "900")))
