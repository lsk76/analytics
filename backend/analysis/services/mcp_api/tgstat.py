"""TGStat (tgstat.ru) з чату: канали, підбірки (єдиний шлях до ЧАТІВ), картка, публікації.

Логіка тут лише транспортна: уся робота з сайтом — у сервісі `tgstat`
(`tgstat_service/`, `docs/tgstat-service.md`), бо там живе ОДИН залогінений
headed Chrome, прив'язаний до профілю+IP+збірки браузера, і там же стережеться
темп запитів. Ці інструменти — HTTP до `settings.TGSTAT_API_URL`
(`http://tgstat:8020` у compose), той самий API, що й у окремого stdio-сервера
`tgstat_service/app/mcp_server.py`.

Чому дубль: окремий сервер треба реєструвати кожному клієнту руками і він не
знає ні про права Django, ні про аудит. Тут інструменти йдуть тим самим шляхом,
що решта MCP (`perms.py` → право розділу адмінки, `registry.call` → аудит), і
видні всім, кому вже видно `tg-analytics`.

Запитів до tgstat НЕ безліч: сайт на частоту відповідає капчею «Подозрение на
робота», і тоді стоять ВСІ запити, доки людина не пройде її у VNC. Тому
`max_pages` малий, а `tgstat_links` взагалі не ходить у tgstat.
"""
from urllib.parse import quote

from django.conf import settings

from analysis.models import Setting
from analysis.services.mcp_api import fmt
from analysis.services.mcp_api.registry import SCOPE_ADMIN, ToolError, tool

GROUP = "tgstat"

# Готові поради на машинні стани сервісу: капча і зламана сесія лікуються лише
# руками людини у VNC, тож інструмент має сказати це, а не «500 Internal».
CAPTCHA_HINT = ("tgstat просить капчу («Подозрение на робота») — запити стоять. "
                "Людина: tgstat_manual_login → у VNC пройти капчу на будь-якій "
                "сторінці даних (напр. tgstat.ru/channel/@rian_ru/stat) → закрити "
                "вкладку. Не повторюй запит до того.")


def _base() -> str:
    return str(getattr(settings, "TGSTAT_API_URL", "") or "http://tgstat:8020").rstrip("/")


def _explain(status: int, body: dict, path: str = "") -> str:
    if status == 404 and path.startswith("/selftest"):
        # Саме так виглядає «контейнер старіший за код»: маршруту ще немає.
        return ("сервіс tgstat не знає про /selftest — контейнер старіший за цю "
                "перевірку. Пересоздати його з поточного образу: "
                "`docker compose -f docker-compose.prod.yml "
                "up -d --force-recreate tgstat`.")
    state = (body or {}).get("state")
    msg = (body or {}).get("error") or str(body)[:300]
    if state == "captcha":
        return CAPTCHA_HINT
    if state == "login_required":
        return f"сесія tgstat непридатна: {msg}. Людина: tgstat_manual_login і вхід у VNC."
    if state == "manual":
        return "іде ручний вхід у VNC — дочекайся tgstat_manual_finish."
    return f"{status}: {msg}"


def _api(path: str, params: dict | None = None, method: str = "GET"):
    """Запит до сервісу tgstat. Помилки — `ToolError` з готовою порадою."""
    import httpx

    clean = {k: (str(v).lower() if isinstance(v, bool) else str(v))
             for k, v in (params or {}).items() if v not in (None, "")}
    # Читати можна довго (сервіс сам тримає паузи між сторінками), а ось
    # З'ЄДНАННЯ або є, або немає: без короткого connect-тайм-ауту виклик із
    # мережі, де контейнера tgstat не видно, висить усі десять хвилин замість
    # того, щоб одразу сказати, що сервісу немає.
    timeout = httpx.Timeout(float(getattr(settings, "TGSTAT_API_TIMEOUT", 600) or 600),
                            connect=5.0)
    try:
        r = httpx.request(method, _base() + path, params=clean, timeout=timeout)
    except httpx.HTTPError as e:
        raise ToolError(
            f"сервіс tgstat не відповідає ({_base()}): {type(e).__name__}. "
            "Контейнер живий? `service_ps` / `service_logs tgstat`. У dev-стеку "
            "його взагалі немає — tgstat крутиться лише на проді "
            "(профіль tgstat у docker-compose.prod.yml).") from e
    try:
        body = r.json()
    except ValueError:
        body = {"error": (r.text or "")[:300]}
    if r.status_code >= 400:
        raise ToolError(_explain(r.status_code, body, path))
    return body


def _n(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:g}"
    return f"{int(value):,}".replace(",", " ")


def _peer_line(i: int, p: dict) -> str:
    head = f"{i}. {p.get('title') or p.get('ref')} — {p.get('ref')}"
    bits = [f"{_n(p.get('subscribers'))} підп."]
    if p.get("avg_post_reach") is not None:
        bits.append(f"охоплення {_n(p['avg_post_reach'])}")
    if p.get("ci") is not None:
        bits.append(f"ІЦ {_n(p['ci'])}")
    if p.get("category"):
        bits.append(p["category"])
    if p.get("last_message"):
        bits.append(f"ост. повідомлення {p['last_message']} тому")
    out = [head, "   " + " · ".join(bits)]
    if p.get("description"):
        out.append("   " + fmt.trunc(p["description"], 200))
    out.append(f"   {p.get('tgstat_url', '')}"
               + (f" | {p['tme_url']}" if p.get("tme_url") else ""))
    return "\n".join(out)


def _peers(res: dict, title: str) -> str:
    items = res.get("items") or []
    tail = (f"\n\n…є ще (сторінок прочитано {res.get('pages')}); збільш max_pages/limit, "
            "якщо справді треба" if res.get("has_more") else "")
    if not items:
        return f"{title}: нічого не знайдено (сторінок {res.get('pages')})"
    body = "\n".join(_peer_line(i, p) for i, p in enumerate(items, 1))
    return f"{title}: {res.get('count', len(items))}\n\n{body}{tail}"


@tool("tgstat_status", group=GROUP, params={
      "reload": "true — перезайти на сайт і перепитати, хто залогінений (1 запит до tgstat)"})
def tgstat_status(reload: bool = False):
    """Стан сесії tgstat: залогінений акаунт і тариф, капча/Cloudflare, ручний вхід.

    З цього починають: усі інші tgstat-інструменти працюють лише поки сесія
    придатна (`usable`), а зламану сесію чи капчу лікує ЛИШЕ людина у VNC.
    """
    h = _api("/health")
    s = _api("/auth/status", {"reload": "1" if reload else ""})
    rows = [
        ("стан", f"{s.get('state')}"
                 + (f" · {s['user']} ({s.get('plan') or '—'})" if s.get("user") else "")),
        ("браузер", ("є" if h.get("browser") else "немає")
                    + (" · іде ручний вхід" if h.get("manual") else "")),
        ("перевірено", s.get("checked_at")),
        ("деталі", s.get("detail")),
    ]
    out = fmt.kv(rows)
    if not s.get("usable"):
        out += "\n\nщо робити: " + _explain(503, {"state": s.get("state"),
                                                  "error": s.get("detail")})
    return out


@tool("tgstat_channels_search", group=GROUP, params={
      "q": "слова для пошуку в назві каналу, напр. «Бурятия»",
      "in_about": "шукати й в описі каналу",
      "min_subs": "мінімум підписників", "max_subs": "максимум підписників",
      "country": "країна (назва рос. або id); '' — будь-яка",
      "category": "категорія tgstat рос., напр. «Политика», «Новости и СМИ»",
      "language": "мова рос., напр. «Русский», «Башкирский»",
      "sort": "participants | avg_reach | ci_index | members_7d | members_30d",
      "limit": "скільки каналів повернути",
      "max_pages": "скільки сторінок (запитів до tgstat) читати, 1–20; тримай малим"})
def tgstat_channels_search(q: str, in_about: bool = False, min_subs: int = 0,
                           max_subs: int = 0, country: str = "Россия", category: str = "",
                           language: str = "", sort: str = "participants",
                           limit: int = 50, max_pages: int = 2):
    """Пошук КАНАЛІВ tgstat за словами в назві (in_about=true — і в описі).

    Кожна сторінка (~30 каналів) — окремий запит до tgstat. ЧАТИ цим не
    шукаються: пошуку чатів у tgstat немає, для них — `tgstat_catalog`.
    """
    res = _api("/channels/search", dict(
        q=q, in_about=in_about, min_subs=min_subs or None, max_subs=max_subs or None,
        country=country, category=category, language=language, sort=sort,
        limit=limit, max_pages=max_pages))
    return _peers(res, f"Канали за «{q}»")


@tool("tgstat_catalog_tags", group=GROUP, params={
      "kind": "geo — регіональні підбірки, theme — тематичні"})
def tgstat_catalog_tags(kind: str = "geo"):
    """Список підбірок tgstat: slug звідси йде в `tgstat_catalog`.

    geo — регіональні (напр. buratia-region), theme — тематичні.
    """
    res = _api("/catalog/tags", {"kind": kind})
    if not res.get("items"):
        return (f"підбірок {kind} не знайдено — схоже, змінилась розмітка tgstat "
                f"(перевірка руками: /raw?path=/tags/{kind})")
    return (f"Підбірки {kind}: {res.get('count')}\n\n"
            + "\n".join(f"{t['slug']} — {t['title']}" for t in res["items"]))


@tool("tgstat_catalog", group=GROUP, params={
      "tag": "slug підбірки з tgstat_catalog_tags, напр. buratia-region",
      "kind": "channel або chat",
      "limit": "скільки записів повернути",
      "max_pages": "скільки сторінок (запитів до tgstat) читати; тримай малим"})
def tgstat_catalog(tag: str, kind: str = "chat", limit: int = 100, max_pages: int = 3):
    """Канали або ЧАТИ підбірки tgstat (регіональної/тематичної).

    Єдиний спосіб знайти ЧАТИ через tgstat: пошуку чатів там немає, лише
    підбірки.
    """
    res = _api(f"/catalog/{quote(tag)}", {"kind": kind, "limit": limit,
                                          "max_pages": max_pages})
    what = "Чати" if kind == "chat" else "Канали"
    return _peers(res, f"{what} підбірки {tag} ({res.get('tgstat_url', '')})")


_STAT_LABELS = {
    "subscribers": "підписники", "ci": "індекс цитування",
    "avg_post_reach": "середнє охоплення 1 поста", "avg_ad_reach": "рекламне охоплення",
    "err_percent": "ERR", "er_percent": "ER", "age": "вік", "posts": "публікацій",
}


@tool("tgstat_channel", group=GROUP, params={
      "ref": "@handle, handle, t.me/handle або URL tgstat",
      "kind": "channel або chat; порожнє — визначити з посилання (дефолт channel)"})
def tgstat_channel(ref: str, kind: str = ""):
    """Картка каналу/чату з tgstat: підписники і приріст, ІЦ, охоплення, ERR/ER,
    вік, кількість публікацій, категорія, гео/мова, РКН + посилання. 1 запит.
    """
    c = _api(f"/channel/{quote(ref, safe='@')}", {"kind": kind})
    out = [f"{c.get('title')} — {c.get('ref')}" + (" ✔" if c.get("verified") else ""),
           f"категорія: {c.get('category') or '—'} · гео/мова: {c.get('geo_lang') or '—'}"
           + (" · зареєстрований у РКН" if c.get("rkn_registered") else "")]
    if c.get("description"):
        out.append(f"опис: {fmt.trunc(c['description'], 400)}")
    for key, st in (c.get("stats") or {}).items():
        val = st.get("value")
        line = f"{_STAT_LABELS.get(key, key)}: {val if key == 'age' else _n(val)}"
        det = st.get("details") or {}
        if det:
            line += " (" + ", ".join(f"{k} {v if isinstance(v, str) else _n(v)}"
                                     for k, v in det.items()) + ")"
        out.append(line)
    out.append(f"{c.get('tgstat_stat_url', '')}"
               + (f" | {c['tme_url']}" if c.get("tme_url") else ""))
    return "\n".join(out)


@tool("tgstat_posts_search", group=GROUP, params={
      "q": "запит, як у пошуку tgstat",
      "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD",
      "peer_type": "all | channel | chat",
      "sort": "date | views",
      "hide_forwards": "не показувати репости",
      "strong": "точний збіг форми слова",
      "extended": "розширений синтаксис tgstat",
      "minus_words": "слова-виключення",
      "limit": "скільки постів повернути",
      "max_pages": "скільки сторінок (запитів до tgstat) читати; тримай малим"})
def tgstat_posts_search(q: str, date_from: str = "", date_to: str = "",
                        peer_type: str = "all", sort: str = "date",
                        hide_forwards: bool = False, strong: bool = False,
                        extended: bool = False, minus_words: str = "",
                        limit: int = 40, max_pages: int = 2):
    """Пошук ПУБЛІКАЦІЙ у tgstat (потрібен Premium) — у каналах і чатах.

    Перша сторінка ~20 постів, кожна наступна — ще один запит до tgstat. Це
    розвідка, а не збір: у БД нічого не осідає.
    """
    res = _api("/posts/search", {
        "q": q, "from": date_from, "to": date_to, "peer_type": peer_type,
        "sort": sort, "hide_forwards": hide_forwards, "strong": strong,
        "extended": extended, "minus_words": minus_words, "limit": limit,
        "max_pages": max_pages})
    items = res.get("items") or []
    head = f"Публікації за «{q}»: знайдено {_n(res.get('total'))}, показано {len(items)}"
    if not items:
        return head
    rows = []
    for i, p in enumerate(items, 1):
        rows.append(
            f"{i}. {p.get('date')} · {p.get('channel_title')} ({p.get('ref')}) · "
            f"👁 {_n(p.get('views'))}\n   {fmt.trunc(p.get('text'), 300)}\n"
            f"   {p.get('tme_post_url') or ''} | {p.get('tgstat_post_url', '')}")
    tail = ("\n\n…є ще сторінки; збільш max_pages, якщо справді треба"
            if res.get("has_more") else "")
    return head + "\n\n" + "\n".join(rows) + tail


@tool("tgstat_links", group=GROUP, params={
      "ref": "@handle, handle, t.me/handle або URL tgstat",
      "post_id": "номер поста — тоді ще й посилання на сам пост",
      "kind": "channel або chat; порожнє — визначити з посилання"})
def tgstat_links(ref: str, post_id: int = 0, kind: str = ""):
    """Посилання на tgstat (сторінка, статистика, пост) і t.me — БЕЗ запиту до tgstat.

    Безкоштовно й без ризику капчі: лише складає URL за правилами сайту.
    """
    res = _api(f"/links/{quote(ref, safe='@')}", {"post_id": post_id or None, "kind": kind})
    return "\n".join(f"{k}: {v}" for k, v in res.items() if v)


@tool("tgstat_manual_login", group=GROUP, mutates=True, scope=SCOPE_ADMIN)
def tgstat_manual_login():
    """Ручний вхід/капча: сервіс відпускає свій браузер і відкриває звичайний Chrome у VNC.

    Далі працює ЛЮДИНА: інші запити до tgstat до завершення стоять, тож не
    запускай це «про запас».
    """
    _api("/auth/manual", method="POST")
    return ("Звичайний Chrome відкрито у VNC. Людині: ssh -N -L 6080:127.0.0.1:6080 "
            "tg-analytics → http://localhost:6080/vnc.html → увійти / пройти капчу → "
            "закрити вкладку (або tgstat_manual_finish).")


@tool("tgstat_manual_finish", group=GROUP, mutates=True, scope=SCOPE_ADMIN)
def tgstat_manual_finish():
    """Завершити ручний вхід: закрити звичайний Chrome (cookies лишаються) і
    повернути браузер сервісу. Повертає стан сесії.
    """
    s = _api("/auth/manual/finish", method="POST")
    out = f"стан: {s.get('state')}" + (f" · {s['user']} ({s.get('plan') or '—'})"
                                       if s.get("user") else "")
    return out + (f"\n{s['detail']}" if s.get("detail") else "")


# Останній прогін живого самоконтролю — щоб його було видно і в адмінці, і в
# service_health, а не лише в логу cron (`deploy/tgstat-canary.sh`).
SELFTEST_SETTING = "tgstat_selftest_last"


def selftest_remember(res: dict) -> None:
    """Зберегти підсумок прогону в key-value (рядок створюється сам)."""
    line = (f"{res.get('checked_at') or ''} {res.get('verdict')}: "
            f"{res.get('summary') or res.get('detail') or ''}").strip()
    Setting.objects.update_or_create(key=SELFTEST_SETTING, defaults={
        "value": line,
        "description": "Останній живий самоконтроль розбору tgstat (пише tgstat_selftest)"})


def selftest_last() -> str:
    return Setting.get(SELFTEST_SETTING, "ще не запускався")


# mutates: ходить у tgstat 5 разів (ризик капчі для всіх) і пише Setting — у
# режимі лише-читання такому запуску не місце.
@tool("tgstat_selftest", group=GROUP, mutates=True, params={
      "only": "звузити до перевірок через кому: channels_search, catalog_tags, "
              "catalog_chats, channel_card, posts_search (порожнє — усі)"})
def tgstat_selftest(only: str = ""):
    """Чи ще працює розбір ЖИВОГО tgstat — ловить зміну розмітки, а не наші баги.

    Тести на заготовках стережуть наш код і лишаються зеленими в день, коли
    tgstat перевіршує сторінки. Цей прогін іде на живий сайт і перевіряє, що
    поля, на яких тримаються інструменти, досі розбираються: канали знаходяться
    і мають підписників, підбірки не спорожніли, картка великого каналу дає
    мільйони й показники, пости мають дати й перегляди.

    УВАГА: 5 запитів до tgstat за прогін. Частіше за раз на добу не ганяти —
    сам прогін накличе капчу, і сервіс стане для всіх (щоденний cron —
    `deploy/tgstat-canary.sh`). `verdict`: ok — розмітка на місці, broken —
    щось розбирається порожньо, unverified — сесія непридатна, тобто НЕ
    перевірено (це не поломка розбору).
    """
    res = _api("/selftest", {"only": only})
    selftest_remember(res)
    verdict = res.get("verdict")
    head = fmt.kv([
        ("вердикт", {"ok": "✓ розмітка на місці", "broken": "✗ РОЗБІР ЗЛАМАВСЯ",
                     "unverified": "— не перевірено"}.get(verdict, verdict)),
        ("підсумок", res.get("summary") or res.get("detail")),
        ("стан сесії", res.get("state")),
        ("запитів до tgstat", res.get("requests")),
        ("перевірено", res.get("checked_at")),
    ])
    rows = [[c["name"], "✓" if c["ok"] else "✗", "; ".join(c.get("problems") or []) or "—"]
            for c in (res.get("checks") or [])]
    body = fmt.table(["перевірка", "", "що не так"], rows, [20, 1, 110]) if rows else ""
    tail = ""
    if verdict == "broken":
        tail = ("\n\nЩо робити: глянути сиру сторінку (`/raw?path=…` із колонки `raw` "
                "у відповіді сервісу) і правити парсер у `tgstat_service/app/parse.py`; "
                "заготовки тестів — `tgstat_service/tests/fixtures/`.")
    return fmt.joinsec(head, body) + tail
