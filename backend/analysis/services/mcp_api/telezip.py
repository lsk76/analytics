"""TeleZip з чату: весь пошуковий API — режими запиту, фільтри, статистика, розвідка.

Досі TeleZip був доступний лише конвеєру (`task.telezip_query` → воркер collect) і
лише в одному режимі: `text=` по всьому індексу. Решта API — пошук без відмінків
і по regex, фільтр по опису каналу, по автору, по тегах і медіа, статистика без
викачування, пошук каналів і юзерів, контекст навколо повідомлення — жила в
офіційному гайді й ніде більше.

Ці інструменти дають повний контракт з чату і НЕ пишуть у БД, доки про це явно не
попросять (`tz_ingest`). Плановий збір лишається за `run_create`: там чанки,
ретраї й резюмабельність, яких у разового пошуку немає.

Ендпоінти: v4 (`/v4/messages`, `/v4/messages/stats`, `/v4/channels`, `/v4/users`,
`/v4/messages/context`, `/v4/stats`) + v3 `/SearchMacros`. Конвеєр далі ходить у
перевірений v3 `/Find` — його не чіпаємо.
"""
import asyncio
import json
import socket
from datetime import datetime, time as dtime, timedelta, timezone as dt_timezone
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone

from analysis.models import AnalysisTask, Channel, CollectChunk, Post, TelezipSlot
from analysis.services.mcp_api import common, fmt
from analysis.services.mcp_api.registry import SCOPE_ADMIN, ToolError, tool
from analysis.services.telezip import TelezipClient

MAX_WINDOW_DAYS = 62

# КОЖЕН запит до TeleZip коштує ~$0.10 — незалежно від того, чи він повернув
# мільйон повідомлень, чи нуль. Тому ціна залежить від КІЛЬКОСТІ викликів, а не
# від обсягу даних, і головна економія — питати статистику (1 виклик) замість
# перебору пошуками, а період збирати більшими чанками. Інструменти показують
# вартість у відповіді, щоб рішення ухвалювалось із нею перед очима.
REQUEST_COST_USD = 0.10

UNREACHABLE_HINT = (
    "Перевір `tz_status`. Найчастіша причина — впав VPN до api.telezip.net "
    "(77.88.192.66): з контейнера порт 443 просто не відповідає. Після рестарту "
    "контейнера злітає й запис у hosts: "
    '`docker compose exec web sh -c \'echo "77.88.192.66 api.telezip.net" >> /etc/hosts\'`.'
)


# --------------------------------------------------------------------------- описи
# Це ЄДИНЕ, що модель бачить про кожен аргумент у JSON-схемі. Формулювання —
# з офіційного гайда TeleZip; наголос на пастках, бо синтаксис не збігається з
# гугловим: ПРОБІЛ ТУТ = АБО (пошук на Elasticsearch simple_query_string).

_Q = ('ПОШУК ІЗ ВІДМІНКАМИ. ПРОБІЛ = АБО (не І!): "мигрант драка" знайде будь-яке '
      'зі слів. Для І став + перед словом/групою: "мигрант +(драка избил)". '
      'Відмінки самі: "дрон" ловить дрона/дрону/дронах — перелічувати не треба. '
      'Префікс: "мобилиз*" → мобилизация/мобилизовать. Фраза: "\"сбор денег\"". '
      'Відстань: "\"снаряд пушка\"~5" (тильда ЗЛИТНО). Виключення: "-mavic" або '
      '"-(всу фронт)" — мінус ЗЛИТНО, без пробілу. Макроси кластерів лише в '
      'дужках: "+(##бавовна) -(##спам)". Робоча форма: ТЕМА +(ДІЯ) -(ШУМ).')

_EXACT = ('ДОСЛІВНО, без відмінків — для абревіатур, марок, серійних номерів: '
          'exact="сво" не зачепить "свой"/"своего", а text="сво" зачепить. '
          'Оператори ті самі, що в text. Можна разом із text: умови '
          'комбінуються (exact="сво" + text="бригада").')

_REGEX = ('Регулярний вираз по тексту повідомлення: картки, IBAN, телефони, '
          'крипто-гаманці, координати. Дорогий режим — звужуй вікно й канали.')

_CHTERM = ('Фільтр ПО КАНАЛУ, а не по повідомленню: спершу відбираються канали, '
           'у назві/описі яких є ці слова, і лише в них шукається текст. '
           'Оператори як у text. Приклад: channeltext="крым крим АРК". '
           'Не став багато поширених слів: якщо під критерій підпаде понад '
           '20 000 каналів, /FIND відлупить запит (у самого /CHANNELS межа '
           'інша — 10 000 знайдених каналів).')

_CHANNELS = ('Обмежити пошук цими каналами: @ім\'я або числовий id, через кому. '
             'Порожньо = весь індекс (3.5 млн каналів). Щоб узяти ВСЕ з каналу, '
             'лишай text="*" і став сюди канал.')

_USERS = ('Обмежити пошук дописами цих авторів: @ім\'я або числовий TelegramID, '
          'через кому. Так дивляться "усе, що писала ця людина".')

_LANGS = ('Мови повідомлень через кому, двобуквені коди як у гайді TeleZip: '
          'ru, uk, en, pl, ro… (мову визначає FastText-176). Порожньо = усі '
          'мови, і в вибірку лізе чуже. Для російського Telegram став "ru".')

_DAYS = ('Скільки останніх діб узяти, включно з сьогоднішньою. Ігнорується, '
         'якщо задані date_from/date_to.')
_FROM = 'Початок періоду YYYY-MM-DD. Треба разом із date_to.'
_TO = 'Кінець періоду YYYY-MM-DD (включно). Треба разом із date_from.'

_UNIQUE = ('true згортає репости (лишає одну копію): менше даних, але ЗАНИЖЕНЕ '
           'охоплення. false віддає все, включно з репостами — тим самим запитом, '
           'другого виклику для цього не треба.')

_SOURCE = ('Тип даних: telegram-канали (telezip, дефолт), зливи баз (darkzip) '
           'або обидва (all).')

_TAGS = 'Шукати лише в каналах із цими тегами (теги каналів TeleZip, через кому).'
_XTAGS = 'Виключити канали з цими тегами. Працює лише разом із tags.'
_MEDIA = 'true — лише повідомлення з фото/відео/документом; false — лише без медіа.'
_THREAD = ('id top-повідомлення: звузити до ОДНІЄЇ гілки коментарів під постом.')
_EXTRA = ('JSON із додатковими полями тіла запиту — запасний хід для того, що '
          'зʼявилось в API і ще не має власного параметра. Поля — з контракту '
          '/FIND (див. docs/telezip-api.md); неіснуюче поле API просто відкине.')
_TIMEOUT = 'Скільки секунд чекати відповідь TeleZip (важкі запити — до 180+).'

# Ключі — під ОБИДВА іменування: діалект бота (`text=`, `channeltext=`, …), яким
# названі параметри інструментів, і внутрішні імена, що лишились у tz_ingest.
CRITERIA_DOCS = {
    "text": _Q, "query": _Q, "exact": _EXACT, "regex": _REGEX,
    "channeltext": _CHTERM, "channel_term": _CHTERM,
    "channel": _CHANNELS, "channels": _CHANNELS,
    "user": _USERS, "users": _USERS,
    "lang": _LANGS, "languages": _LANGS,
    "hasmedia": _MEDIA, "has_media": _MEDIA,
    "days": _DAYS, "date_from": _FROM, "date_to": _TO, "tags": _TAGS,
    "exclude_tags": _XTAGS, "source": _SOURCE, "thread": _THREAD,
    "extra": _EXTRA, "timeout": _TIMEOUT, "unique": _UNIQUE,
}


# --------------------------------------------------------------------------- утиліти

def _client(timeout: int = 180) -> TelezipClient:
    if not settings.TELEZIP_API_KEY:
        raise ToolError("TELEZIP_API_KEY не заданий у .env — запити неможливі")
    return TelezipClient(settings.TELEZIP_API_KEY, settings.TELEZIP_BASE_URL,
                         timeout=timeout)


def _run(coro):
    """Виконати async-виклик і перекласти падіння на людську мову."""
    try:
        return asyncio.run(coro)
    except RuntimeError as e:
        text, low = str(e), str(e).lower()
        if any(k in low for k in ("timeout", "timed out", "cannot connect",
                                  "connection", "dns")):
            raise ToolError(f"TeleZip не відповідає: {fmt.trunc(text, 200)}\n{UNREACHABLE_HINT}")
        if "429" in text:
            raise ToolError("TeleZip 429 (ліміт запитів) — зменш вікно або слоти "
                            "(`tz_status` → `tz_slots`).")
        if "403" in text:
            raise ToolError(f"TeleZip 403: ключ не має прав на цей виклик. {fmt.trunc(text, 200)}")
        raise ToolError(f"TeleZip: {fmt.trunc(text, 400)}")


def _run_soft(coro):
    """Як `_run`, але 404 = «нічого не знайшли», а не помилка.

    TeleZip відповідає 404 і на «маршруту нема», і на «за цим фільтром порожньо»
    (`/v4/users?usernames=…`). Для довідників друге — нормальний результат.
    """
    try:
        return _run(coro)
    except ToolError as e:
        if "404" in str(e):
            return None
        raise


def _window(days: int, date_from: str, date_to: str, allow_long: bool = False):
    if date_from or date_to:
        if not (date_from and date_to):
            raise ToolError("вкажи ОБИДВІ дати (date_from і date_to) або жодної + days")
        d_from = common.parse_date(date_from, "date_from")
        d_to = common.parse_date(date_to, "date_to")
    else:
        d_to = timezone.now().date()
        d_from = d_to - timedelta(days=max(1, int(days)) - 1)
    if d_to < d_from:
        raise ToolError("date_to раніше за date_from")
    span = (d_to - d_from).days + 1
    if span > MAX_WINDOW_DAYS and not allow_long:
        raise ToolError(f"вікно {span} днів завелике для викачування "
                        f"(межа {MAX_WINDOW_DAYS}). Обсяг за довгий період дивись "
                        "`tz_stats` (він не тягне повідомлення), а збір роби "
                        "`run_create` — там чанки й резюмабельність.")
    return (datetime.combine(d_from, dtime.min, tzinfo=dt_timezone.utc),
            datetime.combine(d_to, dtime.max, tzinfo=dt_timezone.utc), span)


def _csv(spec: str):
    return [x.strip() for x in str(spec or "").replace(",", " ").split() if x.strip()]


def _split_refs(spec: str):
    """`@a, b, 12345` → (імена, числові id)."""
    names, ids = [], []
    for raw in _csv(spec):
        raw = raw.lstrip("@")
        (ids if raw.lstrip("-").isdigit() else names).append(
            int(raw) if raw.lstrip("-").isdigit() else raw)
    return names, ids


def _json_arg(spec: str, name: str):
    if not spec:
        return None
    try:
        data = json.loads(spec)
    except json.JSONDecodeError as e:
        raise ToolError(f"{name} не JSON: {e}")
    if not isinstance(data, dict):
        raise ToolError(f"{name} має бути JSON-об'єктом")
    return data


def _criteria(*, query="", exact="", regex="", channel_term="", channels="",
              users="", languages="", tags="", exclude_tags="", has_media=None,
              unique=None, source="", thread=0, extra="", d_from=None, d_to=None):
    """Спільний набір критеріїв пошуку (контракт v4 MessageSearchRequest)."""
    if not any([query, exact, regex, channel_term, channels, users]):
        raise ToolError("потрібен хоча б один критерій: query / exact / regex / "
                        "channel_term / channels / users. Для «все з каналу» — "
                        "query='*' + channels=@ім'я.")
    ch_names, ch_ids = _split_refs(channels)
    u_names, u_ids = _split_refs(users)
    if (ch_ids or ch_names or u_ids or u_names) and not (query or exact or regex):
        query = "*"      # вимога API: фільтр по каналу/юзеру без тексту не проходить
    if source and source.lower() not in ("all", "telezip", "darkzip"):
        raise ToolError("source: all | telezip | darkzip")
    return TelezipClient.build_criteria(
        date_from=d_from, date_to=d_to, term=query, exact=exact, regex=regex,
        channel_term=channel_term, channel_ids=ch_ids, channel_names=ch_names,
        user_ids=u_ids, user_names=u_names, languages=_csv(languages),
        required_tags=_csv(tags), excluded_tags=_csv(exclude_tags),
        has_media=has_media, unique=unique,
        source={"all": "All", "telezip": "TeleZip", "darkzip": "DarkZip"}.get(
            (source or "").lower(), ""),
        top_message_id=thread or None, extra=_json_arg(extra, "extra"))


def _crit_line(c):
    """Однорядковий опис критеріїв — щоб було видно, ЩО саме питали."""
    out = []
    for k, v in c.items():
        if k in ("fromDate", "toDate"):
            continue
        text = json.dumps(v, ensure_ascii=False)
        if len(text) > 1 and text[0] == '"' and text[-1] == '"':
            text = text[1:-1]
        out.append(f"{k}={fmt.trunc(text, 120)}")
    return ", ".join(out) or "—"


def _warnings(c, span):
    out = []
    term = c.get("searchTerm", "")
    scoped = any(c.get(k) for k in ("channelIds", "channelNames", "channelTerm",
                                    "fromUserId", "fromUserName"))
    if "-(" in term or term.strip().startswith("-"):
        out.append("негація `-(…)` на широкому вікні = 500/timeout/429 (~68× повільніше); "
                   "краще позитивний `+(…)`, сміття відсіє класифікатор")
    if term.strip() == "*" and not scoped:
        out.append("`*` без каналу/юзера — весь індекс, гарантований відлуп")
    if c.get("channelTerm") and len(_csv(c["channelTerm"])) > 6:
        out.append("багато слів у channeltext: якщо під критерій підпаде понад "
                   "20 000 каналів, /FIND відлупить запит")
    if span > 7 and not scoped:
        out.append(f"{span} днів по всьому індексу — ризик відлупу; спершу `tz_stats`")
    return out


def _cost(n_requests, note: str = "") -> str:
    """Рядок вартості: скільки викликів пішло і скільки це коштувало."""
    total = n_requests * REQUEST_COST_USD
    return (f"{n_requests} запит{'и' if 2 <= n_requests <= 4 else ('' if n_requests == 1 else 'ів')}"
            f" до TeleZip ≈ ${total:.2f}" + (f" ({note})" if note else ""))


def _by_day(rows):
    b = {}
    for r in rows:
        d = (r.get("date") or "")[:10]
        if d:
            b[d] = b.get(d, 0) + 1
    return sorted(b.items())


def _by_channel(rows, top=15):
    b = {}
    for r in rows:
        b[r.get("channel_name") or f"id{r.get('channel_id')}"] = \
            b.get(r.get("channel_name") or f"id{r.get('channel_id')}", 0) + 1
    return sorted(b.items(), key=lambda kv: -kv[1])[:top]


def _samples(rows, n, chars):
    out = []
    for r in sorted(rows, key=lambda r: r.get("date") or "")[-n:] if n else []:
        who = f" ← {r['from_user_name']}" if r.get("from_user_name") else ""
        out.append(f"{(r.get('date') or '')[:16]}  @{r.get('channel_name') or '?'}{who}  "
                   f"{r.get('message_url') or ''}\n    {fmt.trunc(r.get('content'), chars)}")
    return "\n".join(out) or "—"


# --------------------------------------------------------------------------- стан

@tool("tz_status", group="telezip", params={
      "deep": "true — додатково сходити в API по статистику індексу (глибина, лаг). Один безкоштовний службовий виклик."})
def tz_status(deep: bool = True):
    """Чи живий TeleZip: мережа, ключ, ГЛИБИНА індексу й лаг, слоти, свіжість збору.

    «Глибина» — найдавніша дата, яку взагалі можна шукати (searchDateLimit):
    глибше неї збір поверне порожньо не через помилку, а бо даних немає.
    """
    base = settings.TELEZIP_BASE_URL or ""
    host = urlparse(base).hostname or "api.telezip.net"
    parts, ip, dns_err, tcp, tcp_err = [], None, "", False, ""
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:  # noqa: BLE001
        dns_err = f"{type(e).__name__}: {e}"
    if ip:
        try:
            socket.create_connection((host, 443), timeout=8).close()
            tcp = True
        except Exception as e:  # noqa: BLE001
            tcp_err = f"{type(e).__name__}: {e}"
    parts.append(fmt.section("Мережа", fmt.kv([
        ("база", base), ("DNS", f"{host} → {ip}" if ip else f"✗ {dns_err}"),
        ("TCP :443", "✓" if tcp else f"✗ {tcp_err or 'немає маршруту'}"),
        ("ключ", "заданий" if settings.TELEZIP_API_KEY else "✗ ПОРОЖНІЙ"),
    ])) + ("" if tcp else f"\n\n⚠ {UNREACHABLE_HINT}"))

    if deep and tcp:
        try:
            st = _run(_index_stats())
            lim = st.get("searchDateLimit") or {}
            parts.append(fmt.section("Індекс", fmt.kv([
                ("повідомлень", f"{st.get('messageCount', 0):,}".replace(",", " ")),
                ("каналів", f"{st.get('channelCount', 0):,}".replace(",", " ")),
                ("лаг індексації", f"{st.get('delaySeconds', 0):.0f} с "
                                   "(наскільки свіжі дані відстають від Telegram)"),
                ("глибина пошуку", f"з {(lim.get('minSearchDate') or '?')[:10]} "
                                   f"({lim.get('maxDaysBack')} днів назад) — "
                                   "старіше НЕ шукається взагалі"),
            ])))
        except ToolError as e:
            parts.append(fmt.section("Індекс", f"✗ {e}"))

    now = timezone.now()
    slots = list(TelezipSlot.objects.order_by("slot"))
    busy = [s for s in slots if s.leased_until and s.leased_until > now]
    parts.append(fmt.section("Глобальні слоти (паралельні запити)", fmt.kv([
        ("слотів", len(slots) or "порожньо (засіється з TELEZIP_MAX_CONCURRENCY)"),
        ("TELEZIP_MAX_CONCURRENCY", getattr(settings, "TELEZIP_MAX_CONCURRENCY", 2)),
        ("зайнято", len(busy) or "0"),
    ])) + "\n(змінити наживо: tz_slots_set)")

    last = (CollectChunk.objects.filter(status="done").order_by("-finished_at")
            .select_related("task").first())
    parts.append(fmt.section("Збір через TeleZip", fmt.kv([
        ("останній чанк", f"{last.task.slug} {last.date_from} ({fmt.ago(last.finished_at)}, "
                          f"{last.posts_collected} постів)" if last else "жодного"),
        ("у черзі", CollectChunk.objects.filter(status="pending").count() or "—"),
        ("зі збоєм", CollectChunk.objects.filter(status="failed").count() or "—"),
    ])))
    return fmt.joinsec(*parts)


async def _index_stats():
    async with _client(60) as tz:
        return await tz.index_stats()


# --------------------------------------------------------------------------- API
# Три ендпоінти TeleZip — три інструменти, один до одного. Імена параметрів
# повторюють діалект бота (`text=`, `exact=`, `channeltext=`, `channel=`,
# `user=`, `lang=`), щоб те, що написано в гайді, працювало тут без перекладу.


@tool("tz_find", group="telezip", params={**CRITERIA_DOCS,
      "stats": "true — повернути ЛІЧИЛЬНИКИ (скільки повідомлень, каналів, авторів + динаміка) замість самих повідомлень. Ендпоінт /FindStats: працює там, де звичайний пошук відлупило б за обсягом, і не тягне тексти. УВАГА: у цьому режимі `unique` не діє — API його не приймає.",
      "by": "Для stats=true: гранулярність динаміки — day або hour.",
      "limit": "Стеля викачування повідомлень, до 10000.",
      "sample": "true — випадкова вибірка до limit замість перших N (швидше на важких запитах, результат не відтворюваний).",
      "page_size": "Посторінково замість limit (взаємовиключні); наступну сторінку бери з page_token у відповіді.",
      "page_token": "Токен наступної сторінки з попередньої відповіді.",
      "samples": "Скільки повідомлень показати текстом.",
      "chars": "Обрізати текст кожного прикладу до N символів.",
      "top": "Скільки каналів показати в топі."})
def tz_find(text: str = "", days: int = 1, date_from: str = "", date_to: str = "",
            exact: str = "", regex: str = "", channeltext: str = "", channel: str = "",
            user: str = "", lang: str = "ru", hasmedia: bool = None, unique: bool = True,
            source: str = "", thread: int = 0, extra: str = "", stats: bool = False,
            by: str = "day", limit: int = 200, sample: bool = False, page_size: int = 0,
            page_token: str = "", samples: int = 5, chars: int = 220, top: int = 15,
            timeout: int = 180):
    """/FIND — пошук у текстах повідомлень Telegram (217 млрд повідомлень, 3.5 млн каналів).

    Потрібен хоча б один критерій: text, exact, regex, channel або user.

    ПРОБІЛ = АБО, не І. `text="мигрант драка"` дасть усе про мігрантів ПЛЮС усе
    про бійки. Потрібне І — `text="мигрант +(драка избил)"`. Мінус ЗЛИТНО:
    `-mavic`. Відмінки застосовуються самі, перелічувати не треба.

    Два режими виводу:
      * `stats=false` (типово) — самі повідомлення: тексти, автори, посилання.
        Лічильник «віддано» = розмір ВИБІРКИ, а не скільки всього збігів;
      * `stats=true` — лише цифри: скільки повідомлень, каналів, авторів,
        динаміка по днях/годинах, топ каналів. Питай так обсяг ПЕРЕД тим, як
        качати: працює на довгих вікнах, де видача відлупилась би.

    Кожен виклик ≈ $0.10 (платиться за виклик, не за обсяг).
    """
    d_from, d_to, span = _window(days, date_from, date_to, allow_long=bool(stats))
    crit = _criteria(query=text, exact=exact, regex=regex, channel_term=channeltext,
                     channels=channel, users=user, languages=lang, has_media=hasmedia,
                     unique=None if stats else unique, source=source, thread=thread,
                     extra=extra, d_from=d_from, d_to=d_to)
    head_common = [("критерії", _crit_line(crit)),
                   ("вікно", f"{d_from:%Y-%m-%d} … {d_to:%Y-%m-%d} ({span} дн)")]
    warn = _warnings(crit, span)

    if stats:
        async def go_stats():
            async with _client(timeout) as tz:
                return await tz.search_stats(crit)
        st = _run(go_stats())
        total = st.get("messageCount", 0)
        buckets = {}
        for ts, n in (st.get("messagesPerHour") or {}).items():
            key = ts[:10] if by == "day" else ts[:13].replace("T", " ")
            buckets[key] = buckets.get(key, 0) + n
        chans = sorted(st.get("channels") or [],
                       key=lambda c: -c.get("messageCount", 0))[:top]
        parts = [fmt.section("/FIND — статистика", fmt.kv(head_common + [
            ("повідомлень", f"{total} ({total / span:.0f}/добу)" if span else total),
            ("каналів", st.get("channelCount", 0)),
            ("авторів", st.get("userCount", 0)),
            ("перше/останнє", f"{(st.get('firstMessageDate') or '—')[:16]} … "
                              f"{(st.get('lastMessageDate') or '—')[:16]}"),
            ("ціна", _cost(1)),
        ]))]
        if buckets:
            parts.append(fmt.section(f"Динаміка (по {'днях' if by == 'day' else 'годинах'})",
                                     "  ".join(f"{k[5:] if by == 'day' else k[8:]}:{v}"
                                               for k, v in sorted(buckets.items()))))
        if chans:
            parts.append(fmt.section("Топ каналів", fmt.table(
                ["канал", "повідомлень", "частка"],
                [[f"@{c.get('name') or c.get('id')}", c.get("messageCount", 0),
                  fmt.pct(c.get("messageCount", 0), total)] for c in chans])))
        if warn:
            parts.append("⚠ " + "\n⚠ ".join(warn))
        return fmt.joinsec(*parts)

    async def go():
        async with _client(timeout) as tz:
            return await tz.search(crit, limit=0 if page_size else max(1, int(limit)),
                                   page_size=int(page_size), page_token=page_token,
                                   sample_only=bool(sample))
    res = _run(go())
    rows = res["messages"]
    parts = [fmt.section("/FIND — повідомлення", fmt.kv(head_common + [
        ("віддано", f"{len(rows)}" + (f" (стеля {limit}{', випадкова вибірка' if sample else ''})"
                                      if not page_size else f" (сторінка {page_size})")),
        ("каналів у вибірці", len({r.get("channel_id") for r in rows})),
        ("ще є сторінка", f"page_token={res['next_page_token']}"
         if res.get("next_page_token") else "—"),
        ("ціна", _cost(1, "обсяг за період — той самий виклик зі stats=true")),
    ]))]
    if warn:
        parts.append("⚠ " + "\n⚠ ".join(warn))
    if not rows:
        parts.append("Порожньо: або справді нема збігів, або запит відлупило за "
                     "лімітами, або період глибший за індекс (`tz_status`).")
        return fmt.joinsec(*parts)
    parts.append(fmt.section("По днях", "  ".join(f"{d[5:]}:{n}" for d, n in _by_day(rows))))
    parts.append(fmt.section("Топ каналів у вибірці", fmt.table(
        ["канал", "постів", "частка"],
        [[f"@{n}", c, fmt.pct(c, len(rows))] for n, c in _by_channel(rows, top)])))
    if samples:
        parts.append(fmt.section(f"Приклади (останні {min(samples, len(rows))})",
                                 _samples(rows, samples, chars)))
    return fmt.joinsec(*parts)


@tool("tz_channels", group="telezip", params={
      "term": "Вільний пошук по назві, опису й юзернейму разом. Синтаксис як у tz_find: пробіл = АБО, + = І. Приклад: \"Якутия +(новости чат)\".",
      "name": "Юзернейм каналу, точний збіг, без @ (напр. sakhaday).",
      "title": "Пошук лише по НАЗВІ каналу (з відмінками, оператори працюють).",
      "about": "Пошук лише по ОПИСУ каналу.",
      "id": "Числові TelegramID каналів через кому.",
      "source": _SOURCE,
      "page_size": "Скільки каналів на сторінку.",
      "page_token": "Токен наступної сторінки з попередньої відповіді.",
      "timeout": _TIMEOUT})
def tz_channels(term: str = "", name: str = "", title: str = "", about: str = "",
                id: str = "", source: str = "", page_size: int = 30,
                page_token: str = "", timeout: int = 120):
    """/CHANNELS — пошук каналів і чатів у базі TeleZip (не в Телеграмі загалом).

    Канали, яких TeleZip не вантажить, тут не знайдуться. Межа саме цього
    ендпоінта — 10 000 знайдених каналів; більше = відлуп, треба звужувати.
    (Не плутай із межею 20 000 для `channeltext` у tz_find — там канали лише
    відбираються, а шукається все одно по повідомленнях.)

    Віддає: id, юзернейм, назву, опис, підписників, скільки повідомлень
    збережено, мову, канал це чи чат, і чи він ЗАРАЗ у моніторингу (якщо ні —
    доступна лише збережена історія). Плюс позначку, чи є він у нашому довіднику.

    Звідси беруть `id`/`name` для `tz_find(channel=...)`.
    """
    if not any([term, name, title, about, id]):
        raise ToolError("дай критерій: term / name / title / about / id")
    names, ids = _split_refs(", ".join(x for x in (name, id) if x))

    async def go():
        async with _client(timeout) as tz:
            return await tz.search_channels(
                channel_ids=ids, channel_names=names, title=title, about=about,
                channel_term=term, source=source, page_size=int(page_size),
                page_token=page_token)
    data = _run(go())
    rows = data.get("channels") or []
    known = {c.tg_id: c for c in Channel.objects.filter(
        tg_id__in=[r.get("id") for r in rows if r.get("id")])}
    if not rows:
        return "За цим критерієм каналів немає. Точний збіг — у `name`; за описом — `term`/`about`."
    return fmt.joinsec(
        fmt.section("/CHANNELS", fmt.kv([
            ("критерій", term or name or title or about or id),
            ("показано", f"{len(rows)}" + (" (є наступна сторінка)"
                                           if data.get("nextPageToken") else "")),
            ("ще є сторінка", f"page_token={data['nextPageToken']}"
             if data.get("nextPageToken") else "—"),
            ("ціна", _cost(1)),
        ])),
        fmt.table(["id", "username", "назва", "підп.", "повідом.", "мова", "тип",
                   "у моніторингу", "у нас"],
                  [[r.get("id"), f"@{r.get('name')}" if r.get("name") else "—",
                    fmt.trunc(r.get("title"), 30), r.get("userCount") or 0,
                    r.get("messageCount") or 0, r.get("language") or "—",
                    "канал" if r.get("isChannel") else "чат",
                    fmt.flag(bool(r.get("isActive"))),
                    f"#{known[r['id']].id}" if r.get("id") in known else "—"]
                   for r in rows]),
        "«у нас» — рядок у довіднику Channel; підключити до моніторингу: chats_list/chat_update.")


@tool("tz_users", group="telezip", params={
      "username": "Юзернейми через кому, без @ — точний збіг. Віддає TelegramID навіть тоді, коли профілю в базі немає.",
      "id": "Числові TelegramID через кому.",
      "term": "Вільний пошук по UserName / FirstName / LastName (дослівно, без відмінків).",
      "is_bot": "Лише боти (true) чи лише люди (false).",
      "is_active": "Фільтр за ознакою активності профілю.",
      "page_size": "Скільки профілів на сторінку.",
      "timeout": _TIMEOUT})
def tz_users(username: str = "", id: str = "", term: str = "", is_bot: bool = None,
             is_active: bool = None, page_size: int = 20, timeout: int = 120):
    """/USERS — пошук людей за профілем: юзернейм, ім'я, прізвище, TelegramID.

    Два різні питання:
      * `username` / `id` — точний збіг («хто такий @X», «чий це id»);
      * `term` — вільний пошук по імені й прізвищу («усі Durov»).

    Щоб побачити, ЩО людина писала, візьми знайдений id у
    `tz_find(user="<id>", text="*")`.
    """
    if not any([username, id, term]):
        raise ToolError("дай критерій: username / id / term")
    names = _csv(username)
    ids = [int(x) for x in _csv(id) if x.lstrip("-").isdigit()]
    parts = []

    if names:
        async def by_name():
            async with _client(timeout) as tz:
                return await tz.users_by_username(names)
        found = _run_soft(by_name()) or {}
        parts.append(fmt.section("Юзернейм → TelegramID", fmt.table(
            ["username", "id"],
            [[n, ", ".join(str(i) for i in (found.get(n) or [])) or "не знайдено"]
             for n in names])))
        ids += [i for v in found.values() for i in v]

    async def profiles():
        async with _client(timeout) as tz:
            return await tz.search_users(user_ids=ids, term=term, is_bot=is_bot,
                                         is_active=is_active, page_size=int(page_size))
    data = _run_soft(profiles()) if (ids or term) else None
    users = (data or {}).get("users") or []
    if users:
        parts.append(fmt.section(
            f"Профілі ({(data or {}).get('totalUsers', len(users))} збігів)", fmt.table(
                ["id", "username", "ім'я", "телефон", "бот", "активний", "оновлено"],
                [[u.get("userId"), f"@{u.get('userName')}" if u.get("userName") else "—",
                  fmt.trunc(" ".join(filter(None, [u.get("firstName"), u.get("lastName")])), 28),
                  u.get("phone") or "—", fmt.flag(bool(u.get("isBot")), "бот", ""),
                  fmt.flag(bool(u.get("isActive"))),
                  (u.get("lastBasicProfileUpdate") or "")[:10]] for u in users])))
    elif ids or term:
        parts.append("Профілів за цим критерієм немає (404 від TeleZip = порожньо).")
    parts.append("Що ця людина писала: tz_find(user=\"<id>\", text=\"*\").")
    return fmt.joinsec(*parts)
