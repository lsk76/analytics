"""Живий самоконтроль розбору tgstat: «чи ще працюють парсери на справжньому сайті».

Навіщо окремо від тестів: тести на заготовках (`tests/`) стережуть НАШ код — вони
й далі зелені в день, коли tgstat перевіршує розмітку. Зловити саме це можна лише
сходивши на живий сайт і подивившись, чи на місці поля, на яких тримаються
інструменти: якщо парсер мовчки віддає 0 каналів або канал без підписників —
це вже сталось, і краще дізнатись із логу, ніж із порожнього дослідження.

Інваріанти навмисно грубі (є дані / є поле / їх більше за дрібницю), бо точні
числа на живому сайті змінюються щодня. Вони ловлять не «цифра інша», а
«розмітка поїхала, розбір дає порожнє».

ЦІНА — КАПЧА. Один прогін = 5 запитів до tgstat; частіше за раз на добу не
ганяти (`deploy/tgstat-canary.sh`), інакше сам прогін і накличе «Подозрение на
робота», і сервіс стане для всіх.
"""
import asyncio
from datetime import date, timedelta

from . import ops

VERDICT_OK = "ok"                  # усі перевірки пройшли
VERDICT_BROKEN = "broken"          # щось розбирається порожньо — дивитись розмітку
VERDICT_UNVERIFIED = "unverified"  # капча/розлогін/ручний вхід: НЕ перевірено

# Орієнтири, які на tgstat не змінюються роками: якби канал зник чи підбірка
# спорожніла, це помітно й без нас, зате перевірка стає конкретною.
PROBE_CHANNEL = "@rian_ru"
PROBE_CHANNEL_MIN_SUBS = 1_000_000
PROBE_TAG = "buratia-region"
PROBE_WORD = "новости"
# Стелі для живого tgstat зі словом «новости» за три дні: там таких постів
# десятки тисяч, тож 5 розібраних і сотня в «Найдено» — це «розбір працює», а не
# «мало даних». На заготовках у тестах їх занижують навмисно.
MIN_POSTS = 5
MIN_TOTAL = 100


def _problems_peers(items: list, *, need: int, what: str, stats: bool) -> list:
    """Спільні інваріанти списку каналів/чатів: є записи, у них є ідентифікація."""
    out = []
    if len(items) < need:
        out.append(f"{what}: розібрано {len(items)}, а було хоча б {need} — "
                   "схоже, картки більше не парсяться")
        return out
    for field in ("ref", "title", "tgstat_url"):
        empty = [i for i in items if not i.get(field)]
        if empty:
            out.append(f"{what}: у {len(empty)} з {len(items)} записів порожнє «{field}»")
    no_subs = [i for i in items if not i.get("subscribers")]
    if len(no_subs) > len(items) // 2:
        out.append(f"{what}: підписники не розібрались у {len(no_subs)} з {len(items)} "
                   "записів (у tgstat вони є завжди)")
    if stats:
        rich = [i for i in items if i.get("avg_post_reach") or i.get("ci")]
        if not rich:
            out.append(f"{what}: ні в одного запису немає ні охоплення, ні ІЦ — "
                       "колонки статистики в картці змінились")
    return out


async def check_channels_search(browser) -> tuple[list, str]:
    res = await ops.search_channels(browser, PROBE_WORD, limit=30, max_pages=1)
    return (_problems_peers(res.get("items") or [], need=10,
                            what="пошук каналів", stats=True),
            "POST /channels/search (форма — у ops.search_channels)")


async def check_catalog_tags(browser) -> tuple[list, str]:
    res = await ops.tags(browser, "geo")
    items = res.get("items") or []
    out = []
    if len(items) < 40:
        out.append(f"підбірки geo: розібрано {len(items)}, а регіонів у tgstat "
                   "десятки — список більше не парсяться")
    elif PROBE_TAG not in {i["slug"] for i in items}:
        out.append(f"підбірки geo: серед {len(items)} немає відомого «{PROBE_TAG}» — "
                   "змінились slug-и")
    return out, f"GET /tags/geo"


async def check_catalog_chats(browser) -> tuple[list, str]:
    res = await ops.catalog(browser, PROBE_TAG, kind="chat", limit=30, max_pages=1)
    return (_problems_peers(res.get("items") or [], need=5,
                            what="чати підбірки", stats=False),
            f"POST /tag/{PROBE_TAG}/items (peerType=chat)")


async def check_channel_card(browser) -> tuple[list, str]:
    res = await ops.channel(browser, PROBE_CHANNEL)
    out = []
    if not res.get("title"):
        out.append(f"картка {PROBE_CHANNEL}: немає назви")
    stats = res.get("stats") or {}
    subs = (stats.get("subscribers") or {}).get("value") or 0
    if subs < PROBE_CHANNEL_MIN_SUBS:
        out.append(f"картка {PROBE_CHANNEL}: підписників {subs}, а має бути понад "
                   f"{PROBE_CHANNEL_MIN_SUBS} — число розібралось неправильно")
    for key in ("ci", "avg_post_reach"):
        if key not in stats:
            out.append(f"картка {PROBE_CHANNEL}: немає показника «{key}» — "
                       "блок статистики переїхав")
    if not res.get("tme_url"):
        out.append(f"картка {PROBE_CHANNEL}: не склалось посилання на t.me")
    return out, f"GET /channel/{PROBE_CHANNEL}/stat"


def _problems_posts(items: list, total) -> list:
    """Інваріанти пошуку публікацій — окремо від запиту, щоб перевірятись тестом."""
    out = []
    if (total or 0) < MIN_TOTAL:
        out.append(f"пошук публікацій: «Найдено» = {total}, а за «{PROBE_WORD}» "
                   f"їх мають бути тисячі — лічильник не розібрався")
    if len(items) < MIN_POSTS:
        out.append(f"пошук публікацій: розібрано {len(items)} постів за «{PROBE_WORD}» "
                   "— пости більше не парсяться або зник Premium")
        return out
    for field in ("post_id", "ref", "date"):
        empty = [i for i in items if not i.get(field)]
        if empty:
            out.append(f"пошук публікацій: у {len(empty)} з {len(items)} постів "
                       f"порожнє «{field}»")
    # Посилання на t.me є ЛИШЕ в публічних каналів і чатів: у закритого чату
    # замість @username хеш (напр. 6mfyXsNMdTI0Yjgy), і публічного посилання на
    # його пост не існує. Вимагати його від усіх — хибна тривога, яку перший же
    # живий прогін і спіймав.
    public = [i for i in items if str(i.get("ref") or "").startswith("@")]
    no_link = [i for i in public if not i.get("tme_post_url")]
    if no_link:
        out.append(f"пошук публікацій: у {len(no_link)} з {len(public)} ПУБЛІЧНИХ "
                   "постів не склалось посилання на t.me")
    if not any(i.get("text") for i in items):
        out.append("пошук публікацій: ні в одного поста немає тексту")
    # Перегляди бувають не скрізь (у чатах їх немає) — тому «хоч в одного»,
    # а не «в усіх»: живі дані дають приблизно 7 із 20.
    if not any(i.get("views") for i in items):
        out.append("пошук публікацій: ні в одного поста немає переглядів")
    return out


async def check_posts_search(browser) -> tuple[list, str]:
    """Пошук публікацій — Premium: тут ламається і розмітка, і тариф."""
    since = (date.today() - timedelta(days=3)).isoformat()
    res = await ops.search_posts(browser, PROBE_WORD, date_from=since, limit=20,
                                 max_pages=1)
    return _problems_posts(res.get("items") or [], res.get("total")), "POST /search"


# Порядок має значення: найдешевша й найпоказовіша перевірка перша, щоб у разі
# «усе лягло» звіт не чекав решти запитів.
CHECKS = (
    ("channels_search", check_channels_search),
    ("catalog_tags", check_catalog_tags),
    ("catalog_chats", check_catalog_chats),
    ("channel_card", check_channel_card),
    ("posts_search", check_posts_search),
)


async def run(browser, only: str = "") -> dict:
    """Пройти перевірки по живому tgstat. `only` — через кому, щоб звузити.

    Кожна перевірка — один запит до tgstat. Падіння однієї не спиняє решту (крім
    капчі: вона летить нагору, бо далі всі запити однаково марні).
    """
    picked = [c for c in CHECKS if not only or c[0] in only.replace(" ", "").split(",")]
    if only and not picked:
        raise ValueError(f"only: невідомі перевірки {only!r}. "
                         f"Є: {', '.join(n for n, _ in CHECKS)}")
    state = await browser.check(reload=False)
    if not state.usable:
        return {"verdict": VERDICT_UNVERIFIED, "state": state.state,
                "detail": state.detail, "checks": [], "requests": 0,
                "checked_at": state.checked_at}

    results, requests = [], 0
    for name, fn in picked:
        requests += 1
        try:
            problems, raw_hint = await fn(browser)
            results.append({"name": name, "ok": not problems, "problems": problems,
                            "raw": raw_hint})
        except asyncio.CancelledError:
            raise
        except (ValueError, LookupError, RuntimeError) as e:
            # Помилка самої перевірки — теж сигнал «розбір не працює», але
            # відрізняється від «поле порожнє», тож і в звіті окремо.
            results.append({"name": name, "ok": False, "error": f"{type(e).__name__}: {e}",
                            "problems": [f"перевірка впала: {e}"], "raw": ""})

    broken = [r for r in results if not r["ok"]]
    return {"verdict": VERDICT_BROKEN if broken else VERDICT_OK,
            "state": state.state, "checks": results, "requests": requests,
            "checked_at": state.checked_at,
            "summary": (f"зламалось {len(broken)} з {len(results)}: "
                        + ", ".join(r["name"] for r in broken)) if broken
                       else f"усі {len(results)} перевірки пройшли"}
