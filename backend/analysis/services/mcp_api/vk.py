"""VK з чату: пошук постів, спільноти, стіна, коментарі.

Транспорт і токен — `analysis/services/vk.py`; тут лише параметри «як каже
людина» і текст відповіді. Інструменти НЕ пишуть у БД: щоб матеріал осів у
базі, потрібен або підписаний на задачу Source (`source_add kind=vk` →
конвеєр інформпростору), або збір коментарів monitor-задачі
(`mon_collect_source=vk_comments` → «Збори»).

Чим VK відрізняється від TeleZip — тримай у голові, бо звички звідти тут шкодять:
  * запити БЕЗКОШТОВНІ (ліміт — темп, ~3/сек), згоди людини не потребують;
  * мова запиту проста: кілька слів = І, лапки = фраза; ні `|`, ні `+`, ні
    негації немає. Потрібне АБО — це кілька викликів;
  * `vk_find` (newsfeed.search) бачить лише кілька останніх тижнів і віддає до
    ~1000 записів. Історія глибша є лише у стіни конкретної спільноти —
    `vk_wall` (wall.search/wall.get), там межі глибини немає.
"""
from datetime import datetime, timedelta, timezone

from analysis.services import vk
from analysis.services.mcp_api import fmt
from analysis.services.mcp_api.registry import ToolError, tool

GROUP = "vk"

NOT_CONFIGURED_HINT = (
    "VK не налаштований: немає токена користувача. Людина: «Налаштування» в "
    "адмінці → ключ vk_api_token (або VK_API_TOKEN у .env і рестарт web/воркерів). "
    "Сервісний токен застосунку не підходить — ні пошуку, ні коментарів він не бачить.")


def _guard(fn, *a, **kw):
    """Виклик VK із перекладом помилок клієнта на мову інструмента."""
    try:
        return fn(*a, **kw)
    except vk.VkNotConfigured as e:
        raise ToolError(NOT_CONFIGURED_HINT) from e
    except vk.VkRateLimited as e:
        raise ToolError(f"VK просить зачекати ({e}). Повтори через "
                        f"{int(e.retry_after)}с і не став запити підряд.") from e
    except vk.VkAccessDenied as e:
        raise ToolError(f"{e}. Закрита спільнота або вимкнені коментарі — "
                        "обходу немає, бери інше джерело.") from e
    except vk.VkError as e:
        raise ToolError(str(e)) from e


def _window(days: int, date_from: str, date_to: str) -> tuple[datetime, datetime]:
    """days / date_from / date_to → (since, until) в UTC. Явні дати важливіші."""
    now = datetime.now(timezone.utc)

    def _d(s, end=False):
        try:
            d = datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise ToolError(f"дата «{s}» не схожа на YYYY-MM-DD") from None
        return d + timedelta(hours=23, minutes=59, seconds=59) if end else d

    until = _d(date_to, end=True) if date_to else now
    since = _d(date_from) if date_from else until - timedelta(days=max(1, int(days or 1)))
    if since > until:
        raise ToolError(f"порожній період: {since:%Y-%m-%d} пізніше за {until:%Y-%m-%d}")
    return since, until


def _n(value) -> str:
    if value in (None, ""):
        return "—"
    return f"{int(value):,}".replace(",", " ")


def _owner_names(res: dict) -> dict[int, str]:
    """extended-відповідь VK → {owner_id: назва} (спільноти від'ємні)."""
    names = {}
    for g in (res.get("groups") or []):
        names[-int(g["id"])] = g.get("name") or g.get("screen_name") or ""
    for p in (res.get("profiles") or []):
        names[int(p["id"])] = vk.author_name(p)
    return names


def _posts_block(items, names, chars: int, limit: int) -> str:
    rows = []
    for it in items[:limit]:
        oid, pid = int(it.get("owner_id") or 0), int(it.get("id") or 0)
        when = vk.ts_to_dt(it.get("date"))
        rows.append("\n".join([
            f"{when:%Y-%m-%d %H:%M} · {names.get(oid) or vk.owner_url(oid)}"
            + (f" · {_n((it.get('views') or {}).get('count'))} перегл." if it.get("views") else "")
            + (" · репост" if it.get("copy_history") else ""),
            f"   {fmt.trunc(vk.post_text(it), chars)}",
            f"   {vk.post_url(oid, pid)}",
        ]))
    return "\n".join(rows)


@tool("vk_status", group=GROUP, params={})
def vk_status():
    """Чи працює VK: токен, від чийого імені говоримо, версія API, межі.

    Безкоштовний (один службовий виклик users.get). З нього починають: без
    токена решта vk_* нічого не зробить, і це не баг, а ненастроєний стенд.
    """
    rows = [("токен", "є" if vk.is_configured() else "НЕМАЄ"),
            ("версія API", vk.api_version()),
            ("проксі", "є" if vk.proxy_url() else "—")]
    if not vk.is_configured():
        return fmt.joinsec(fmt.kv(rows), "що робити: " + NOT_CONFIGURED_HINT)
    who = _guard(vk.me)
    rows.append(("говоримо від імені",
                 f"{vk.author_name(who)} (@{who.get('screen_name') or who.get('id')})"))
    limits = fmt.kv([
        ("темп", "~3 запити/сек на токен; паузу тримає клієнт сам"),
        ("vk_find углиб", "кілька тижнів, до ~1000 записів на запит"),
        ("vk_wall углиб", "без межі — це стіна конкретної спільноти"),
        ("мова запиту", "кілька слів = І, лапки = фраза; | та + НЕ працюють"),
    ])
    return fmt.joinsec(fmt.kv(rows), fmt.section("Межі VK", limits))


@tool("vk_find", group=GROUP, params={
      "q": "Слова пошуку. Кілька слів = І (всі мають бути), лапки = точна фраза. "
           "АБО немає — для синонімів роби окремі виклики.",
      "days": "Глибина пошуку в днях від сьогодні (якщо не задані дати).",
      "date_from": "Початок періоду YYYY-MM-DD (перебиває days).",
      "date_to": "Кінець періоду YYYY-MM-DD включно.",
      "limit": "Скільки постів викачати, до 200 за виклик.",
      "show": "Скільки з них надрукувати текстом.",
      "chars": "Обрізати текст кожного прикладу до N символів."})
def vk_find(q: str, days: int = 7, date_from: str = "", date_to: str = "",
            limit: int = 100, show: int = 10, chars: int = 220):
    """Пошук постів по всьому відкритому VK (`newsfeed.search`). Безкоштовно.

    УВАГА ПРО ГЛИБИНУ: VK віддає лише кілька останніх тижнів і не більше
    ~1000 записів на запит — це інструмент «що зараз пишуть», а не архів.
    Історію по конкретній спільноті шукай `vk_wall` (там межі глибини немає).
    """
    if not (q or "").strip():
        raise ToolError("vk_find без слів пошуку: передай q")
    since, until = _window(days, date_from, date_to)
    res = _guard(vk.newsfeed_search, q.strip(), start_time=since, end_time=until,
                 count=min(max(int(limit), 1), 200))
    items = res.get("items") or []
    names = _owner_names(res)
    head = fmt.kv([
        ("запит", q.strip()),
        ("період", f"{since:%Y-%m-%d} … {until:%Y-%m-%d}"),
        ("знайдено (оцінка VK)", _n(res.get("total_count"))),
        ("викачано", len(items)),
        ("ще є", "так (звузь період або додай слово)" if res.get("next_from") else "ні"),
    ])
    if not items:
        return fmt.joinsec(head, "Нічого не знайдено. VK шукає з І між словами — "
                                 "спробуй одне слово або коротший період.")
    by_owner: dict[int, int] = {}
    for it in items:
        by_owner[int(it.get("owner_id") or 0)] = by_owner.get(int(it.get("owner_id") or 0), 0) + 1
    top = fmt.table(["спільнота", "постів", "посилання"],
                    [[names.get(o) or "—", n, vk.owner_url(o)]
                     for o, n in sorted(by_owner.items(), key=lambda kv: -kv[1])[:15]],
                    [40, None, None])
    return fmt.joinsec(head, fmt.section("Хто пише", top),
                       fmt.section("Пости", _posts_block(items, names, chars, int(show))))


@tool("vk_groups", group=GROUP, params={
      "q": "Слова для пошуку спільнот за назвою й описом (напр. «Якутия новости»).",
      "refs": "Через кому — конкретні спільноти (vk.com/xxx, club123, коротке імʼя). "
              "Задано — пошуку немає, перевіряються саме ці.",
      "limit": "Скільки спільнот повернути (пошук).",
      "sort": "0 — за релевантністю, 6 — за кількістю учасників."})
def vk_groups(q: str = "", refs: str = "", limit: int = 30, sort: int = 0):
    """Знайти спільноти VK за словами або перевірити конкретні за посиланнями.

    Це довідкове читання: у базу нічого не кладе. Щоб спільноту почали
    опитувати — `source_add kind=vk url=https://vk.com/…` і підписка на задачу
    (інформпростір) або `chat_add` у monitor-задачу зі збором коментарів VK.
    """
    if refs.strip():
        got = _guard(vk.groups_get_by_id, [r for r in refs.replace(" ", ",").split(",") if r])
        title = f"Спільноти за посиланнями ({len(got)})"
    elif q.strip():
        res = _guard(vk.groups_search, q.strip(), count=min(max(int(limit), 1), 100),
                     sort=int(sort))
        got = res.get("items") or []
        title = f"Пошук «{q.strip()}»: знайдено {_n(res.get('count'))}, показано {len(got)}"
    else:
        raise ToolError("vk_groups: передай q (пошук) або refs (конкретні спільноти)")
    if not got:
        return f"{title}\n(нічого)"
    rows = [[g.get("name") or "—",
             _n(g.get("members_count")),
             "закрита" if g.get("is_closed") else "відкрита",
             f"https://vk.com/{g.get('screen_name') or 'club' + str(g.get('id'))}"]
            for g in got]
    return fmt.joinsec(title, fmt.table(["назва", "учасників", "доступ", "посилання"],
                                        rows, [45, None, None, None]))


@tool("vk_wall", group=GROUP, params={
      "group": "Спільнота: vk.com/xxx, club123 або коротке імʼя.",
      "q": "Слова пошуку в межах цієї стіни. Порожньо = просто останні пости.",
      "days": "Глибина в днях (лише коли q порожнє).",
      "date_from": "Початок періоду YYYY-MM-DD (лише коли q порожнє).",
      "date_to": "Кінець періоду YYYY-MM-DD включно (лише коли q порожнє).",
      "limit": "Скільки постів узяти.",
      "show": "Скільки надрукувати текстом.",
      "chars": "Обрізати текст прикладу до N символів."})
def vk_wall(group: str, q: str = "", days: int = 7, date_from: str = "", date_to: str = "",
            limit: int = 50, show: int = 10, chars: int = 220):
    """Пости однієї спільноти: пошук у її стіні або період цілком. Безкоштовно.

    На відміну від `vk_find`, глибина НЕ обмежена — так дивляться історію
    конкретної спільноти перед тим, як брати її в джерела.
    """
    owner_id, kind = _guard(vk.resolve_owner, group)
    limit = min(max(int(limit), 1), 300)
    if q.strip():
        res = _guard(vk.wall_search, owner_id, q.strip(), count=min(limit, 100))
        items, total, scope = res.get("items") or [], res.get("count"), f"пошук «{q.strip()}»"
    else:
        since, until = _window(days, date_from, date_to)
        items = _guard(vk.wall_posts_between, owner_id, since, until, max_posts=limit)
        total, scope = len(items), f"{since:%Y-%m-%d} … {until:%Y-%m-%d}"
    names = {owner_id: group}
    head = fmt.kv([("спільнота", f"{group} ({vk.owner_url(owner_id)}, {kind})"),
                   ("зріз", scope), ("знайдено", _n(total)), ("показано", min(len(items), int(show)))])
    if not items:
        return fmt.joinsec(head, "Нічого немає за цим зрізом.")
    return fmt.joinsec(head, fmt.section("Пости", _posts_block(items, names, chars, int(show))))


@tool("vk_comments", group=GROUP, params={
      "post": "Посилання на пост: https://vk.com/wall-123_456 (або «-123_456»).",
      "limit": "Скільки коментарів прочитати (разом із гілками).",
      "show": "Скільки надрукувати текстом.",
      "chars": "Обрізати текст коментаря до N символів."})
def vk_comments(post: str, limit: int = 100, show: int = 20, chars: int = 200):
    """Коментарі під постом VK — чим живиться збір критики (mon_collect_source=vk_comments).

    Так перевіряють спільноту ПЕРЕД тим, як ставити збір: чи відкриті в неї
    коментарі і чи це взагалі думки людей, а не реакції-смайли.
    """
    raw = (post or "").strip()
    tail = raw.split("wall", 1)[1] if "wall" in raw else raw
    tail = tail.split("?", 1)[0]
    try:
        owner_id, post_id = (int(x) for x in tail.split("_", 1))
    except ValueError:
        raise ToolError(f"не схоже на посилання на пост VK: «{post}». "
                        "Треба https://vk.com/wall-123_456") from None
    items, authors = _guard(vk.all_comments, owner_id, post_id,
                            limit=min(max(int(limit), 1), 500))
    head = fmt.kv([("пост", vk.post_url(owner_id, post_id)),
                   ("коментарів прочитано", len(items)),
                   ("унікальних авторів",
                    len({c.get("from_id") for c in items if c.get("from_id")}))])
    if not items:
        return fmt.joinsec(head, "Коментарів немає або вони вимкнені власником.")
    body = "\n".join(
        f"{vk.ts_to_dt(c.get('date')):%Y-%m-%d %H:%M} · "
        f"{vk.author_name(authors.get(int(c.get('from_id') or 0))) or c.get('from_id')}: "
        f"{fmt.trunc(c.get('text'), chars)}"
        for c in items[:int(show)] if (c.get("text") or "").strip())
    return fmt.joinsec(head, fmt.section("Коментарі", body))
