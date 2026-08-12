"""Спільні утиліти infospace-конвеєра."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Клік-трекери поза utm_* (utm_* зрізаються за префіксом)
_DROP_PARAMS = {"yclid", "fbclid", "gclid", "ysclid", "_openstat"}

DEFAULT_USER_AGENT = "tg-event-analytics infospace monitor (+https://example.org/bot)"


def _resolve_proxy(value):
    """config.proxy → URL проксі або None.

    true / "default" / "env" → спільний проксі: рядок `infospace_proxy_url`
    у таблиці Setting (оператор міняє з адмінки, без деплою), інакше змінна
    оточення INFOSPACE_PROXY_URL. Явний рядок у config — як є (пер-джерело);
    порожньо/False — без проксі.
    """
    if not value:
        return None
    if value is True or (isinstance(value, str)
                         and value.strip().lower() in ("default", "env", "true")):
        # лінивий імпорт: адаптери мають лишатись тестованими без Django/БД
        from django.conf import settings
        env_default = getattr(settings, "INFOSPACE_PROXY_URL", "")
        try:
            from analysis.models import Setting
            return Setting.get("infospace_proxy_url", env_default) or None
        except Exception:  # noqa: BLE001 — нема БД (юніт-тести) → лишається env
            return env_default or None
    return str(value)


def http_options(source) -> dict:
    """httpx-опції джерела: {"headers": {...}, "proxy": url|None}.

    Керується з адмінки через Source.config, без деплою:
      user_agent — підмінити UA (сайти, що ріжуть ботів);
      headers    — довільні заголовки (напр. {"Cookie": "beget=begetok"});
      proxy      — див. _resolve_proxy (сайт блокує IP сервера / гео-фільтр).
    """
    cfg = getattr(source, "config", None) or {}
    headers = {"User-Agent": cfg.get("user_agent") or DEFAULT_USER_AGENT}
    headers.update(cfg.get("headers") or {})
    return {"headers": headers, "proxy": _resolve_proxy(cfg.get("proxy"))}


def canonical_url(url: str) -> str:
    """Канонізує посилання на матеріал, щоб той самий текст із RSS і зі
    скрапінгу сайту схлопнувся в один Post (unique (task, url)).

    Правила: хост у нижній регістр; #fragment геть; параметри utm_* і
    відомі клік-трекери геть; решта параметрів — як були (порядок зберігається).
    Не-URL ідентифікатори (@username тощо) повертаються як є.
    """
    url = (url or "").strip()
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        # биті href зі скрапінгу сайту (напр. «Invalid IPv6 URL») не мають
        # валити стадію збору — повертаємо як є, далі відсіє unique/скрін
        return url
    if not parts.scheme or not parts.netloc:
        return url  # tg-ідентифікатори і відносні шляхи не чіпаємо
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _DROP_PARAMS
    ]
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path,
        urlencode(query),
        "",  # fragment
    ))
