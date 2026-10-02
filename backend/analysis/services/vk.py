"""Клієнт VK API (api.vk.com) — ЄДИНЕ місце, звідки застосунок ходить у VK.

Те саме рішення, що й з Telethon: мережа з VK живе в одному модулі, а споживачі
(адаптер інформпростору `infospace/adapters/vk.py`, збір коментарів
`monitor_stages`, інструменти MCP `mcp_api/vk.py`) кличуть лише функції звідси.
Інакше токен, темп запитів і розбір помилок доведеться повторювати тричі.

ТОКЕН. Беремо `Setting vk_api_token` (оператор міняє з адмінки, без деплою),
інакше змінну оточення `VK_API_TOKEN`. Потрібен токен КОРИСТУВАЧА (Kate Mobile
тощо): сервісний токен застосунку не бачить ні `newsfeed.search`, ні
`wall.getComments`. Версія API — `Setting vk_api_version` (дефолт нижче).

ЦІНА І МЕЖІ (чим VK відрізняється від TeleZip — не плутати):
  * запити БЕЗКОШТОВНІ, але темп обмежений: ~3 запити/сек на токен
    (`_throttle` тримає паузу сам), інакше код помилки 6;
  * `newsfeed.search` віддає не більше ~1000 результатів на запит і залазить
    углиб лише на кілька тижнів — для історії є лише стіна конкретної
    спільноти (`wall.get`), вона без обмеження глибини;
  * пошук у VK розуміє прості слова й лапки, АЛЕ не має ні `|`, ні `+`
    TeleZip-діалектів: кілька слів = І. Складне АБО — це кілька запитів.

ПОМИЛКИ. Усе мережеве й усе, що VK вертає в полі `error`, піднімається як
`VkError` (підкласи: `VkNotConfigured`, `VkRateLimited`, `VkAccessDenied`).
Споживач вирішує сам: адаптер інформпростору перетворює `VkRateLimited` на
паузу джерела, збір коментарів — на ретрай чанка.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/"
DEFAULT_VERSION = "5.199"
DEFAULT_MIN_INTERVAL = 0.34   # ~3 запити/сек — ліміт токена користувача
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3               # на коди 6/10 (темп і тимчасовий збій VK)

# Коди помилок VK, які нас цікавлять окремо (повний список — vk.com/dev/errors)
ERR_AUTH = 5             # токен протух / відкликаний
ERR_TOO_MANY = 6         # занадто часто
ERR_INTERNAL = 10        # внутрішня помилка VK (буває транзієнтною)
ERR_PERMISSION = 7
ERR_ACCESS_DENIED = 15
ERR_WALL_ACCESS = 212    # доступ до коментарів заборонено власником
ERR_PRIVATE_PROFILE = 30
ERR_RATE_LIMIT = 29      # ліміт методу вичерпано
_ACCESS_CODES = {ERR_PERMISSION, ERR_ACCESS_DENIED, ERR_WALL_ACCESS, ERR_PRIVATE_PROFILE}


class VkError(Exception):
    """Помилка VK API (або мережі до нього)."""

    def __init__(self, message: str, code: int = 0):
        self.code = int(code or 0)
        super().__init__(message)


class VkNotConfigured(VkError):
    """Немає токена: VK не налаштований (dev-стенд, свіжий сервер)."""


class VkRateLimited(VkError):
    """VK просить зачекати (код 6/29). Не збій джерела — пауза."""

    def __init__(self, message: str, code: int = ERR_TOO_MANY, retry_after: float = 5.0):
        self.retry_after = float(retry_after)
        super().__init__(message, code)


class VkAccessDenied(VkError):
    """Закрита спільнота/профіль або вимкнені коментарі — провина цілі, не наша."""


# --------------------------------------------------------------------- конфіг

def _setting(key: str, env_key: str, default: str = "") -> str:
    """Налаштування з адмінки, інакше змінна оточення, інакше дефолт коду."""
    from django.conf import settings as dj_settings
    env_default = str(getattr(dj_settings, env_key, "") or "")
    try:
        from analysis.models import Setting
        return Setting.get(key, env_default) or default
    except Exception:  # noqa: BLE001 — немає БД (юніт-тести) → лишається env
        return env_default or default


def token() -> str:
    return _setting("vk_api_token", "VK_API_TOKEN").strip()


def is_configured() -> bool:
    return bool(token())


def api_version() -> str:
    return _setting("vk_api_version", "VK_API_VERSION", DEFAULT_VERSION)


def proxy_url():
    """Проксі для VK (якщо з сервера напряму не видно); порожньо = напряму."""
    return _setting("vk_proxy_url", "VK_PROXY_URL") or None


def _min_interval() -> float:
    try:
        return float(_setting("vk_min_interval_sec", "VK_MIN_INTERVAL_SEC",
                              str(DEFAULT_MIN_INTERVAL)))
    except ValueError:
        return DEFAULT_MIN_INTERVAL


# ------------------------------------------------------------------- транспорт

_lock = threading.Lock()
_last_call_at = 0.0


def _throttle() -> None:
    """Пауза між запитами в межах процесу: VK рахує темп на токен, а не на
    виклик, тож стримувати треба тут, а не в кожному споживачі."""
    global _last_call_at
    with _lock:
        gap = _min_interval() - (time.monotonic() - _last_call_at)
        if gap > 0:
            time.sleep(gap)
        _last_call_at = time.monotonic()


def _explain(code: int, msg: str) -> VkError:
    if code == ERR_AUTH:
        return VkError(
            f"VK не приймає токен ({msg}). Новий токен користувача → "
            "«Налаштування» в адмінці, ключ vk_api_token (або VK_API_TOKEN у .env).",
            code)
    if code in (ERR_TOO_MANY, ERR_RATE_LIMIT):
        return VkRateLimited(f"VK просить зачекати: {msg}", code,
                             retry_after=5.0 if code == ERR_TOO_MANY else 60.0)
    if code in _ACCESS_CODES:
        return VkAccessDenied(f"VK закрив доступ: {msg}", code)
    return VkError(f"VK API помилка {code}: {msg}", code)


def call(method: str, **params):
    """Один виклик методу VK API → розпаковане поле `response`.

    Пауза між запитами, ретраї на темп і внутрішню помилку VK — тут; усе інше
    піднімається як `VkError` (див. модульний докстринг).
    """
    import httpx

    tok = token()
    if not tok:
        raise VkNotConfigured(
            "VK не налаштований: немає токена. Поклади токен користувача в "
            "«Налаштування» (ключ vk_api_token) або в .env (VK_API_TOKEN). "
            "Сервісний токен застосунку не підходить — ні пошуку, ні коментарів "
            "він не бачить.")
    payload = {k: _flat(v) for k, v in params.items() if v not in (None, "")}
    payload["access_token"] = tok
    payload["v"] = api_version()

    last: VkError | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            r = httpx.post(API_URL + method, data=payload, timeout=DEFAULT_TIMEOUT,
                           proxy=proxy_url())
            r.raise_for_status()
            body = r.json()
        except httpx.HTTPError as e:
            last = VkError(f"VK недосяжний ({type(e).__name__}): {e}")
        except ValueError as e:
            last = VkError(f"VK віддав не JSON: {e}")
        else:
            err = body.get("error")
            if not err:
                return body.get("response")
            code = int(err.get("error_code") or 0)
            exc = _explain(code, err.get("error_msg") or "")
            if code not in (ERR_TOO_MANY, ERR_INTERNAL):
                raise exc
            last = exc
        delay = getattr(last, "retry_after", None) or min(2 ** attempt, 8)
        if attempt < MAX_RETRIES:
            logger.info("vk %s: %s — спроба %d/%d через %.1fс",
                        method, last, attempt, MAX_RETRIES, delay)
            time.sleep(delay)
    raise last or VkError(f"VK {method}: невідомий збій")


def _flat(value):
    """Списки VK приймає як «1,2,3»; булеві — як 1/0."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(v) for v in value)
    return value


# ---------------------------------------------------------------- хелпери даних

def screen_name(url_or_name: str) -> str:
    """`https://vk.com/club123` / `@club123` / `club123` → `club123`."""
    s = (url_or_name or "").strip().lstrip("@")
    if "vk.com/" in s:
        s = s.split("vk.com/", 1)[1]
    return s.split("?", 1)[0].strip("/").strip()


def resolve_owner(ref: str) -> tuple[int, str]:
    """Коротке ім'я спільноти/людини → (owner_id, тип). Спільнота — від'ємний id
    (так її адресують усі методи стіни), людина — додатний.

    `club123`/`public123`/`id123` розбираються без запиту до VK.
    """
    name = screen_name(ref)
    if not name:
        raise VkError(f"порожнє посилання на спільноту: {ref!r}")
    low = name.lower()
    for prefix, sign, kind in (("club", -1, "group"), ("public", -1, "group"),
                               ("id", 1, "user")):
        if low.startswith(prefix) and low[len(prefix):].isdigit():
            return sign * int(low[len(prefix):]), kind
    res = call("utils.resolveScreenName", screen_name=name) or {}
    kind, oid = res.get("type"), res.get("object_id")
    if not oid:
        raise VkError(f"VK не знає спільноти/людини «{name}»")
    return (-int(oid) if kind in ("group", "page", "event") else int(oid),
            "group" if kind in ("group", "page", "event") else "user")


def post_url(owner_id: int, post_id: int) -> str:
    return f"https://vk.com/wall{int(owner_id)}_{int(post_id)}"


def comment_url(owner_id: int, post_id: int, comment_id: int) -> str:
    """Канонічне посилання на коментар — воно ж ключ `Post.url` (unique task+url)."""
    return f"{post_url(owner_id, post_id)}?reply={int(comment_id)}"


def owner_url(owner_id: int) -> str:
    oid = int(owner_id)
    return f"https://vk.com/{'club' if oid < 0 else 'id'}{abs(oid)}"


def ts_to_dt(value) -> datetime | None:
    """unixtime VK → aware UTC datetime."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def post_text(item: dict) -> str:
    """Текст поста разом із текстом репоста (`copy_history`): у спільнотах
    половина матеріалу — репости з порожнім власним текстом."""
    parts = [(item.get("text") or "").strip()]
    for src in item.get("copy_history") or []:
        t = (src.get("text") or "").strip()
        if t:
            parts.append(t)
    return "\n\n".join(p for p in parts if p)


# -------------------------------------------------------------------- методи

def wall_get(owner_id: int, count: int = 100, offset: int = 0, extended: bool = False):
    """Стіна спільноти/людини, найновіші спершу. Закріплений пост приходить
    першим незалежно від дати — споживач це враховує (`is_pinned`)."""
    return call("wall.get", owner_id=int(owner_id), count=min(int(count), 100),
                offset=int(offset), extended=1 if extended else 0) or {}


def wall_posts_between(owner_id: int, since: datetime, until: datetime,
                       max_posts: int = 1000) -> list[dict]:
    """Пости стіни з вікна [since, until] — гортаємо від найновіших, доки не
    провалимось раніше `since`. Кожні 100 постів — один запит, тож довге вікно
    жвавої спільноти коштує часу (не грошей)."""
    s, u = int(since.timestamp()), int(until.timestamp())
    out, offset = [], 0
    while offset < max_posts:
        batch = (wall_get(owner_id, count=100, offset=offset) or {}).get("items") or []
        if not batch:
            break
        for it in batch:
            if it.get("is_pinned"):
                continue          # закріплений — поза хронологією, не межа вікна
            dt = int(it.get("date") or 0)
            if dt > u:
                continue
            if dt < s:
                return out
            out.append(it)
        offset += len(batch)
    return out


def wall_comments(owner_id: int, post_id: int, count: int = 100, offset: int = 0,
                  thread_items: int = 10):
    """Коментарі під постом (разом із гілками відповідей: `thread_items`)."""
    return call("wall.getComments", owner_id=int(owner_id), post_id=int(post_id),
                count=min(int(count), 100), offset=int(offset), sort="asc",
                need_likes=0, extended=1, thread_items_count=min(int(thread_items), 10),
                fields="first_name,last_name,screen_name") or {}


def all_comments(owner_id: int, post_id: int, limit: int = 300) -> tuple[list[dict], dict]:
    """Усі коментарі поста (з гілками) → (коментарі, автори за id).

    Гілки відповідей VK віддає вкладеними у `thread.items` — розгортаємо, бо для
    аналітики коментар у гілці такий самий голос людини, як кореневий.
    """
    items: list[dict] = []
    authors: dict[int, dict] = {}
    offset = 0
    while len(items) < limit:
        res = wall_comments(owner_id, post_id, count=100, offset=offset)
        batch = res.get("items") or []
        for prof in (res.get("profiles") or []):
            authors[int(prof["id"])] = prof
        for grp in (res.get("groups") or []):
            authors[-int(grp["id"])] = grp
        if not batch:
            break
        for c in batch:
            items.append(c)
            items.extend((c.get("thread") or {}).get("items") or [])
        offset += len(batch)
        if offset >= int(res.get("count") or 0):
            break
    return items[:limit], authors


def author_name(author: dict | None) -> str:
    if not author:
        return ""
    if author.get("name"):                      # спільнота
        return str(author["name"])
    return " ".join(x for x in (author.get("first_name"), author.get("last_name")) if x)


def newsfeed_search(q: str, start_time: datetime | None = None,
                    end_time: datetime | None = None, count: int = 100,
                    start_from: str = "", extended: bool = True):
    """Пошук постів по всьому відкритому VK. Глибина — кілька тижнів, стеля
    видачі ~1000 записів на запит; далі лише `wall.get` конкретних спільнот."""
    params = {"q": q, "count": min(int(count), 200),
              "extended": 1 if extended else 0}
    if start_time:
        params["start_time"] = int(start_time.timestamp())
    if end_time:
        params["end_time"] = int(end_time.timestamp())
    if start_from:
        params["start_from"] = start_from
    return call("newsfeed.search", **params) or {}


def wall_search(owner_id: int, q: str, count: int = 100, offset: int = 0,
                owners_only: bool = True):
    """Пошук у межах ОДНІЄЇ стіни — на відміну від newsfeed.search, без межі
    глибини: так шукають історію в конкретній спільноті."""
    return call("wall.search", owner_id=int(owner_id), query=q,
                count=min(int(count), 100), offset=int(offset),
                owners_only=1 if owners_only else 0, extended=0) or {}


def groups_search(q: str, count: int = 50, country_id: int = 0, city_id: int = 0,
                  sort: int = 0):
    """Пошук спільнот за словами (sort: 0 — за релевантністю, 6 — за учасниками)."""
    params = {"q": q, "count": min(int(count), 100), "sort": int(sort)}
    if country_id:
        params["country_id"] = int(country_id)
    if city_id:
        params["city_id"] = int(city_id)
    return call("groups.search", **params) or {}


def groups_get_by_id(refs, fields: str = "members_count,description,city,country,screen_name"):
    """Картки конкретних спільнот за короткими іменами/id (один запит на пачку
    до 500) — дешевий спосіб перевірити список перед підпискою."""
    names = [screen_name(r) for r in refs if screen_name(r)]
    res = call("groups.getById", group_ids=names, fields=fields) or {}
    # v5.199 віддає {"groups": [...]}, старіші — просто список
    return res.get("groups", res) if isinstance(res, dict) else res


def me():
    """Хто саме говорить із VK цим токеном (перевірка життєздатності)."""
    res = call("users.get", fields="screen_name") or []
    return res[0] if res else {}
