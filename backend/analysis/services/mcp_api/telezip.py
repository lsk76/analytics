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
from analysis.services.mcp_api.registry import ToolError, tool
from analysis.services.telezip import TelezipClient

MAX_WINDOW_DAYS = 62
UNREACHABLE_HINT = (
    "Перевір `tz_status`. Найчастіша причина — впав VPN до api.telezip.net "
    "(77.88.192.66): з контейнера порт 443 просто не відповідає. Після рестарту "
    "контейнера злітає й запис у hosts: "
    '`docker compose exec web sh -c \'echo "77.88.192.66 api.telezip.net" >> /etc/hosts\'`.'
)


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
                            "(`tz_status` → `tz_slots_set`).")
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
        out.append("багато слів у channel_term: понад 20 000 знайдених каналів = відлуп")
    if span > 7 and not scoped:
        out.append(f"{span} днів по всьому індексу — ризик відлупу; спершу `tz_stats`")
    return out


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

@tool("tz_status", group="telezip")
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


@tool("tz_syntax", group="telezip")
def tz_syntax():
    """Шпаргалка TeleZip: режими запиту, фільтри, оператори, ліміти, граблі.

    Викликай ПЕРЕД складанням запиту: діалект свій (`/find text=`), і половина
    «очевидних» звичок (негація, перелік відмінків) тут шкідлива.
    """
    return """## Режими запиту (треба хоча б один)
query        — `searchTerm`: з відмінками. `дрон` → дрона/дрону/дронах
exact        — `exactTerm`: ДОСЛІВНО, без відмінків. Для абревіатур: exact="сво"
               (query="сво" зачепить «свой», «своего»)
regex        — `regexPattern`: патерни (картки, IBAN, телефони, крипто, координати)
channel_term — фільтр по НАЗВІ/ОПИСУ каналу: спершу відбираються канали, потім
               у них шукається текст. `channel_term="крым крим АРК"`
channels     — тільки ці канали (@ім'я або id)
users        — тільки ці автори (@ім'я або id) — «усі дописи людини»

## Фільтри
languages=ru   мови (FastText-176; порожньо = всі)
has_media      true/false — лише з медіа / лише без
unique         true згортає репости (менше даних, занижене охоплення)
source         telezip (Telegram, дефолт) | darkzip (зливи баз) | all
tags/exclude_tags  теги каналів (exclude лише разом із tags)
thread         id top-повідомлення — одна гілка коментарів

## Оператори в query/exact/channel_term
пробіл = АБО   `+` = І   `-слово` = НЕ (пишеться ЗЛИТНО)
`( … )` групування · `слово*` префікс · `"фраза"` · `"a b"~N` відстань N слів
Робоча форма: ТЕМА ∧ ДІЯ − ШУМ
  (мигрант диаспора) +(драка избил напал) -(всу фронт военкомат)

## Ліміти (за ними — «відлуп»: порожньо/500/таймаут)
пошук > 3 хв · збіг > 300 000 повідомлень · > 10 000 каналів ·
channel_term, що зачепив > 20 000 каналів · глибина: лише в межах
searchDateLimit (див. tz_status — зараз ~з 2025-10)

## Граблі
* Негація на широкому вікні — ~68× повільніше; краще позитивний `+(…)`.
* Фільтр по каналу/юзеру БЕЗ тексту API не приймає — інструменти самі
  підставляють query='*'.
* `limit` і посторінковість взаємовиключні.
* Обсяг за великий період питай `tz_stats` — він не тягне повідомлення.

## Маршрут роботи
tz_stats (скільки цього є) → tz_calibrate (ризик і дублі) → tz_search (тексти)
→ task_update (зафіксувати запит у задачі) → run_create (плановий збір)
Розвідка: tz_channels (хто пише про тему), tz_user (хто це написав),
tz_context (що було навколо), tz_macros (готові підзапити), tz_probe (сире API)."""


# --------------------------------------------------------------------------- пошук

@tool("tz_stats", group="telezip")
def tz_stats(query: str = "", days: int = 7, date_from: str = "", date_to: str = "",
             exact: str = "", regex: str = "", channel_term: str = "", channels: str = "",
             users: str = "", languages: str = "", tags: str = "", exclude_tags: str = "",
             has_media: bool = None, source: str = "", thread: int = 0,
             extra: str = "", top: int = 15, by: str = "day", timeout: int = 180):
    """Скільки цього є: повідомлення/автори/канали + динаміка — БЕЗ викачування.

    Найдешевший спосіб зрозуміти запит: рахує на боці TeleZip. Завжди роби це
    ПЕРЕД `tz_search` на широкому вікні. by=day|hour — гранулярність динаміки.
    """
    d_from, d_to, span = _window(days, date_from, date_to, allow_long=True)
    crit = _criteria(query=query, exact=exact, regex=regex, channel_term=channel_term,
                     channels=channels, users=users, languages=languages, tags=tags,
                     exclude_tags=exclude_tags, has_media=has_media, source=source,
                     thread=thread, extra=extra, d_from=d_from, d_to=d_to)

    async def go():
        async with _client(timeout) as tz:
            return await tz.search_stats(crit)
    st = _run(go())

    per_hour = st.get("messagesPerHour") or {}
    buckets = {}
    for ts, n in per_hour.items():
        key = ts[:10] if by == "day" else ts[:13].replace("T", " ")
        buckets[key] = buckets.get(key, 0) + n
    dyn = "  ".join(f"{k[5:] if by == 'day' else k[8:]}:{v}"
                    for k, v in sorted(buckets.items()))
    chans = sorted(st.get("channels") or [], key=lambda c: -c.get("messageCount", 0))[:top]
    total = st.get("messageCount", 0)
    parts = [fmt.section("Статистика запиту", fmt.kv([
        ("критерії", _crit_line(crit)),
        ("вікно", f"{d_from:%Y-%m-%d} … {d_to:%Y-%m-%d} ({span} дн)"),
        ("повідомлень", f"{total} ({total / span:.0f}/добу)" if span else total),
        ("каналів", st.get("channelCount", 0)),
        ("авторів", st.get("userCount", 0)),
        ("перше/останнє", f"{(st.get('firstMessageDate') or '—')[:16]} … "
                          f"{(st.get('lastMessageDate') or '—')[:16]}"),
    ]))]
    if dyn:
        parts.append(fmt.section(f"Динаміка (по {'днях' if by == 'day' else 'годинах'})", dyn))
    if chans:
        parts.append(fmt.section("Топ каналів", fmt.table(
            ["канал", "повідомлень", "частка"],
            [[f"@{c.get('name') or c.get('id')}", c.get("messageCount", 0),
              fmt.pct(c.get("messageCount", 0), total)] for c in chans])))
    warn = _warnings(crit, span)
    if warn:
        parts.append("⚠ " + "\n⚠ ".join(warn))
    return fmt.joinsec(*parts)


@tool("tz_search", group="telezip")
def tz_search(query: str = "", days: int = 1, date_from: str = "", date_to: str = "",
              exact: str = "", regex: str = "", channel_term: str = "", channels: str = "",
              users: str = "", languages: str = "", tags: str = "", exclude_tags: str = "",
              has_media: bool = None, unique: bool = True, source: str = "",
              thread: int = 0, extra: str = "", limit: int = 200, sample: bool = False,
              page_size: int = 0, page_token: str = "", samples: int = 5,
              chars: int = 220, top: int = 15, timeout: int = 180):
    """Разовий пошук у TeleZip — НІЧОГО не пише в БД. Усі режими й фільтри.

    limit — стеля викачування (до 10000); sample=true — випадкова вибірка замість
    перших N (швидше на важких запитах). page_size+page_token — посторінково
    (взаємовиключно з limit). Точний обсяг за період дає `tz_stats`.
    """
    d_from, d_to, span = _window(days, date_from, date_to)
    crit = _criteria(query=query, exact=exact, regex=regex, channel_term=channel_term,
                     channels=channels, users=users, languages=languages, tags=tags,
                     exclude_tags=exclude_tags, has_media=has_media, unique=unique,
                     source=source, thread=thread, extra=extra, d_from=d_from, d_to=d_to)

    async def go():
        async with _client(timeout) as tz:
            return await tz.search(crit, limit=0 if page_size else max(1, int(limit)),
                                   page_size=int(page_size), page_token=page_token,
                                   sample_only=bool(sample))
    res = _run(go())
    rows = res["messages"]
    parts = [fmt.section("Пошук", fmt.kv([
        ("критерії", _crit_line(crit)),
        ("вікно", f"{d_from:%Y-%m-%d} … {d_to:%Y-%m-%d} ({span} дн)"),
        ("віддано", f"{len(rows)}"
                    + (f" (стеля {limit}{', випадкова вибірка' if sample else ''})"
                       if not page_size else f" (сторінка {page_size})")),
        ("каналів у вибірці", len({r.get("channel_id") for r in rows})),
        ("ще є сторінка", f"page_token={res['next_page_token']}"
         if res.get("next_page_token") else "—"),
    ]))]
    warn = _warnings(crit, span)
    if warn:
        parts.append("⚠ " + "\n⚠ ".join(warn))
    if not rows:
        parts.append("Порожньо. Або справді нема збігів, або запит відлупило "
                     "(ліміти — `tz_syntax`), або період глибший за індекс (`tz_status`).")
        return fmt.joinsec(*parts)
    parts.append(fmt.section("По днях", "  ".join(f"{d[5:]}:{n}" for d, n in _by_day(rows))))
    parts.append(fmt.section("Топ каналів у вибірці", fmt.table(
        ["канал", "постів", "частка"],
        [[f"@{n}", c, fmt.pct(c, len(rows))] for n, c in _by_channel(rows, top)])))
    if samples:
        parts.append(fmt.section(f"Приклади (останні {min(samples, len(rows))})",
                                 _samples(rows, samples, chars)))
    return fmt.joinsec(*parts)


@tool("tz_calibrate", group="telezip")
def tz_calibrate(query: str = "", days: int = 3, exact: str = "", regex: str = "",
                 channel_term: str = "", channels: str = "", users: str = "",
                 languages: str = "ru", tags: str = "", project_days: int = 30,
                 timeout: int = 180):
    """Перевірити запит ПЕРЕД збором: обсяг, частка репостів, ризик відлупу.

    Два дешевих виклики статистики (unique on/off) на короткому вікні + проєкція
    на `project_days`. Дешевше, ніж дізнатися про потоп на 30-денному зборі.
    """
    d_from, d_to, span = _window(days, "", "")
    base = dict(query=query, exact=exact, regex=regex, channel_term=channel_term,
                channels=channels, users=users, languages=languages, tags=tags,
                d_from=d_from, d_to=d_to)

    async def go():
        async with _client(timeout) as tz:
            uniq = await tz.search_stats(_criteria(**base, unique=True))
            allm = await tz.search_stats(_criteria(**base, unique=False))
            return uniq, allm
    uniq, allm = _run(go())

    n_uniq, n_all = uniq.get("messageCount", 0), allm.get("messageCount", 0)
    per_day = n_uniq / span if span else 0
    projected = int(per_day * max(1, int(project_days)))
    risk = []
    if projected > 300_000:
        risk.append(f"проєкція {projected} > 300k — TeleZip відлупить вікно; збирай "
                    "по днях (`run_create` так і робить) і звужуй запит")
    elif projected > 50_000:
        risk.append(f"проєкція {projected} — важко для одного вікна, конвеєр має різати по днях")
    if uniq.get("channelCount", 0) > 3000:
        risk.append(f"{uniq['channelCount']} каналів уже за {span} дн — межа 10k близько")
    risk += _warnings(_criteria(**base), span)
    chans = sorted(uniq.get("channels") or [], key=lambda c: -c.get("messageCount", 0))[:10]
    return fmt.joinsec(
        fmt.section("Калібрування", fmt.kv([
            ("критерії", _crit_line(_criteria(**base))),
            ("проба", f"{d_from:%Y-%m-%d} … {d_to:%Y-%m-%d} ({span} дн)"),
            ("unique=true", f"{n_uniq} ({per_day:.0f}/добу, {uniq.get('channelCount', 0)} каналів, "
                            f"{uniq.get('userCount', 0)} авторів)"),
            ("unique=false", f"{n_all} — репости ×{(n_all / n_uniq) if n_uniq else 0:.1f}"),
            (f"проєкція на {project_days} дн", f"~{projected} (unique)"),
        ])),
        fmt.section("Топ каналів проби", fmt.table(
            ["канал", "повідомлень"],
            [[f"@{c.get('name') or c.get('id')}", c.get("messageCount", 0)] for c in chans])),
        ("⚠ " + "\n⚠ ".join(risk)) if risk else "✓ ризиків відлупу не видно",
        "Далі: tz_search (тексти) → task_update (зафіксувати запит) → run_create (збір).")


@tool("tz_channel_posts", group="telezip")
def tz_channel_posts(channel: str, days: int = 1, date_from: str = "", date_to: str = "",
                     query: str = "*", users: str = "", thread: int = 0,
                     limit: int = 300, samples: int = 10, chars: int = 220,
                     unique: bool = True, timeout: int = 180):
    """Усе з ОДНОГО каналу/чату за період (канон: `*` + фільтр каналу).

    Так збираються ad-hoc дослідження, щоб не ганяти широкий запит по індексу.
    thread — лише одна гілка коментарів (id top-повідомлення).
    """
    if not channel.strip():
        raise ToolError("вкажи канал: @ім'я або числовий id")
    return tz_search(query=query, days=days, date_from=date_from, date_to=date_to,
                     channels=channel, users=users, thread=thread, limit=limit,
                     samples=samples, chars=chars, unique=unique, timeout=timeout)


# --------------------------------------------------------------------------- довідники

@tool("tz_channels", group="telezip")
def tz_channels(term: str = "", title: str = "", about: str = "", names: str = "",
                source: str = "", page_size: int = 30, page_token: str = "",
                timeout: int = 120):
    """Пошук КАНАЛІВ у базі TeleZip за назвою/описом — «хто взагалі пише про X».

    term — вільний пошук по Title+About+юзернейму (той самий синтаксис, що й
    у query). Так знаходять регіональні канали для whitelist моніторингу.
    """
    if not any([term, title, about, names]):
        raise ToolError("дай критерій: term / title / about / names")
    ch_names, ch_ids = _split_refs(names)

    async def go():
        async with _client(timeout) as tz:
            return await tz.search_channels(
                channel_ids=ch_ids, channel_names=ch_names, title=title, about=about,
                channel_term=term, source=source, page_size=int(page_size),
                page_token=page_token)
    data = _run(go())
    rows = data.get("channels") or []
    known = {c.tg_id: c for c in Channel.objects.filter(
        tg_id__in=[r.get("id") for r in rows if r.get("id")])}
    table = fmt.table(
        ["id", "username", "назва", "підп.", "повідом.", "мова", "тип", "актив", "у нас"],
        [[r.get("id"), f"@{r.get('name')}" if r.get("name") else "—",
          fmt.trunc(r.get("title"), 32), r.get("userCount") or 0,
          r.get("messageCount") or 0, r.get("language") or "—",
          "канал" if r.get("isChannel") else "чат",
          fmt.flag(bool(r.get("isActive"))),
          f"#{known[r['id']].id}" if r.get("id") in known else "—"]
         for r in rows])
    return fmt.joinsec(
        fmt.section("Канали TeleZip", fmt.kv([
            ("критерій", term or title or about or names),
            ("показано", f"{len(rows)}"
             + (" (є наступна сторінка)" if data.get("nextPageToken") else "")),
            ("ще є сторінка", f"page_token={data['nextPageToken']}"
             if data.get("nextPageToken") else "—"),
        ])),
        table,
        "«у нас» — рядок у довіднику Channel; додати в моніторинг: chats_list/chat_update.")


@tool("tz_channel", group="telezip")
def tz_channel(ref: str, timeout: int = 120):
    """Картка одного каналу (підписники, мова, активність) + чи він є в нас."""
    names, ids = _split_refs(ref)

    async def go():
        async with _client(timeout) as tz:
            return await tz.search_channels(channel_ids=ids, channel_names=names,
                                            page_size=5)
    rows = (_run(go()) or {}).get("channels") or []
    if not rows:
        raise ToolError(f"TeleZip не знає каналу «{ref}» (перевір написання; "
                        "пошук по імені — дослівний, по опису — `tz_channels`)")
    c = rows[0]
    local = Channel.objects.filter(tg_id=c.get("id")).first()
    return fmt.section(f"TeleZip: {ref}", fmt.kv([
        ("id", c.get("id")),
        ("username", f"@{c.get('name')}" if c.get("name") else "—"),
        ("назва", c.get("title")),
        ("підписників", c.get("userCount")),
        ("повідомлень у базі", c.get("messageCount")),
        ("мова", c.get("language") or "—"),
        ("тип", "канал" if c.get("isChannel") else "чат"),
        ("у моніторингу TeleZip", fmt.flag(bool(c.get("isActive")))
         + ("" if c.get("isActive") else " — лише збережена історія")),
        ("опис", fmt.trunc(c.get("about"), 300)),
        ("у нашому довіднику", f"Channel #{local.id}, регіон "
                               f"{local.region_subject.name if local.region_subject_id else '—'}, "
                               f"моніторингів: {local.enrolled_in.count()}" if local else "НЕМА"),
    ]))


@tool("tz_user", group="telezip")
def tz_user(ref: str = "", term: str = "", is_bot: bool = None, is_active: bool = None,
            posts_days: int = 0, posts_limit: int = 20, chars: int = 200,
            page_size: int = 20, timeout: int = 120):
    """Хто це написав: профіль автора за @ім'ям/id або вільний пошук по імені.

    ref — @юзернейм або числовий id (кілька через кому); term — пошук по
    UserName/FirstName/LastName (дослівно, без відмінків). posts_days>0 додає
    останні дописи автора — так з'ясовують, хто стоїть за коментарем у
    моніторингу критики (`Post.author_tg_id`).
    """
    if not (ref or term):
        raise ToolError("дай ref (@ім'я/id) або term (вільний пошук по імені)")
    names, ids = _split_refs(ref)
    parts = []

    if names:
        # окремий ендпоінт: юзернейм → TelegramID (масово, дослівно)
        async def by_name():
            async with _client(timeout) as tz:
                return await tz.users_by_username(names)
        found = _run_soft(by_name()) or {}
        rows = [[name, ", ".join(str(i) for i in (found.get(name) or [])) or "не знайдено"]
                for name in names]
        parts.append(fmt.section("Юзернейм → TelegramID",
                                 fmt.table(["username", "id"], rows)))
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

    if posts_days and (ids or names):
        who = ",".join(str(i) for i in ids) or ",".join(names)
        parts.append(fmt.section(f"Дописи автора за {posts_days} дн", tz_search(
            query="*", users=who, days=int(posts_days), limit=int(posts_limit),
            samples=int(posts_limit), chars=chars, unique=False, timeout=timeout)))
    return fmt.joinsec(*parts)


@tool("tz_context", group="telezip")
def tz_context(channel: str, message_id: int, before: int = 10, after: int = 10,
               anchor_date: str = "", chars: int = 200, timeout: int = 120):
    """Що було НАВКОЛО повідомлення: N сусідніх до і після (аудит знахідки).

    channel — id або @ім'я; message_id — номер із посилання t.me/<канал>/<номер>.
    Потрібно, щоб зрозуміти контекст коментаря, а не судити по вирваному рядку.
    """
    names, ids = _split_refs(channel)
    cid = ids[0] if ids else None
    if cid is None:
        async def resolve():
            async with _client(timeout) as tz:
                data = await tz.search_channels(channel_names=names, page_size=1)
                rows = (data or {}).get("channels") or []
                return rows[0]["id"] if rows else None
        cid = _run(resolve())
        if not cid:
            raise ToolError(f"не вдалось визначити id каналу «{channel}»")

    # anchorDate API вимагає завжди (без неї 400) і шукає якір поруч із цією
    # датою: сусідній день прощається, далекий — ANCHOR_NOT_FOUND. Тож дефолт —
    # сьогодні, а для старого повідомлення дату беруть із хіта tz_search.
    anchor = anchor_date or timezone.now().date().isoformat()

    async def go():
        async with _client(timeout) as tz:
            return await tz.message_context(cid, int(message_id), anchor,
                                            int(before), int(after))
    try:
        data = _run(go())
    except ToolError as e:
        if "404" in str(e) or "ANCHOR_NOT_FOUND" in str(e):
            raise ToolError(
                f"якір не знайдено біля дати {anchor}. Передай anchor_date = дату "
                "самого повідомлення (її показує tz_search у рядку прикладу), "
                "напр. anchor_date=2026-09-17.")
        raise

    def line(m, mark=""):
        who = f" ← {m.get('fromUserName')}" if m.get("fromUserName") else ""
        return (f"{mark}{(m.get('date') or '')[:16]} #{m.get('messageId')}{who}: "
                f"{fmt.trunc(m.get('content'), chars)}")
    anchor = data.get("anchor") or {}
    body = "\n".join(
        [line(m) for m in (data.get("before") or [])]
        + [line(anchor, "▶ ")]
        + [line(m) for m in (data.get("after") or [])])
    return fmt.section(f"Контекст {channel}/{message_id}", body or "порожньо")


@tool("tz_macros", group="telezip")
def tz_macros(filter: str = "", limit: int = 40, timeout: int = 60):
    """Серверні макроси пошуку (`##ім'я` → готовий підзапит) — готові набори термінів."""
    async def go():
        async with _client(timeout) as tz:
            return await tz.search_macros()
    rows = _run(go()) or []
    if filter:
        f = filter.lower()
        rows = [r for r in rows if f in (r.get("name", "") + r.get("value", "")).lower()]
    return fmt.joinsec(
        f"макросів: {len(rows)}" + (f" (фільтр «{filter}»)" if filter else ""),
        fmt.table(["макрос", "розкривається в"],
                  [[r.get("name"), fmt.trunc(r.get("value"), 110)] for r in rows[:limit]]),
        "Вживати просто в запиті: query=\"##ім'я +(додатковий термін)\".")


# --------------------------------------------------------------------------- дії

@tool("tz_ingest", group="telezip", mutates=True)
def tz_ingest(task: str, query: str = "", days: int = 1, date_from: str = "",
              date_to: str = "", channels: str = "", languages: str = "",
              unique: bool = None, dry_run: bool = True, timeout: int = 180):
    """Записати результат разового пошуку в задачу як Post-и (ad-hoc збір).

    dry_run=true (дефолт) лише показує, що потрапило б у базу. Порожній query =
    запит самої задачі. Пости лягають так само, як їх кладе воркер collect.

    Для monitor/infospace/tgsearch НЕ працює: там вставка своя (whitelist чатів,
    регіон на вставці, свої стадії) — користуйся `run_create`.
    """
    from analysis.services import stages
    t = common.resolve_task(task)
    if t.pipeline not in (AnalysisTask.PIPELINE_EVENTS, AnalysisTask.PIPELINE_RESEARCH):
        raise ToolError(f"задача #{t.id} — конвеєр «{t.pipeline}»: ad-hoc вставка зіпсує "
                        "його інваріанти. Збирай через run_create.")
    query = query or t.telezip_query
    if not query:
        raise ToolError("порожній запит і в задачі теж порожньо")
    d_from, d_to, span = _window(days, date_from, date_to)
    ch_names, ch_ids = _split_refs(channels)
    langs = _csv(languages) or (t.languages or [])
    uniq = t.telezip_unique if unique is None else bool(unique)

    async def go():
        async with _client(timeout) as tz:
            return await tz.find_posts_range(query, d_from, d_to, languages=langs or None,
                                             unique=uniq, channel_ids=ch_ids or None,
                                             channel_names=ch_names or None)
    rows = _run(go())
    urls = [r.get("message_url") for r in rows if r.get("message_url")]
    known = set(Post.objects.filter(task=t, url__in=urls).values_list("url", flat=True))
    fresh = [r for r in rows if r.get("message_url") and r["message_url"] not in known]
    head = fmt.kv([
        ("задача", f"#{t.id} {t.slug} ({t.pipeline})"),
        ("запит", fmt.trunc(query, 200) + (" (запит задачі)" if query == t.telezip_query else "")),
        ("вікно", f"{d_from:%Y-%m-%d} … {d_to:%Y-%m-%d} ({span} дн)"),
        ("знайдено", f"{len(rows)}; нових для задачі {len(fresh)}, вже є {len(rows) - len(fresh)}"),
    ])
    if dry_run:
        return fmt.joinsec(
            fmt.section("Пробний прогін (нічого не записано)", head),
            fmt.section("Топ каналів", fmt.table(
                ["канал", "постів"], [[f"@{n}", c] for n, c in _by_channel(fresh, 10)])),
            fmt.section("Приклади", _samples(fresh, 3, 200)),
            "Записати насправді: той самий виклик із dry_run=false.")
    n = stages.ingest_rows(t, rows)
    return fmt.joinsec(
        fmt.section("Записано", head),
        f"✓ у задачу лягло {n} постів (стадія «{Post.STAGE_COLLECTED}») — далі їх веде "
        f"конвеєр. Прогрес: service_queues task={t.slug}")


@tool("tz_slots_set", group="telezip", mutates=True)
def tz_slots_set(count: int):
    """Змінити глобальний ліміт паралельних запитів до TeleZip (таблиця слотів).

    Таблиця — джерело правди для УСІХ воркерів; міняється наживо (напр. 1 на час
    тротлінгу 429). Поточний стан — у `tz_status`.
    """
    count = int(count)
    if not 1 <= count <= 8:
        raise ToolError("розумний діапазон 1..8 (TeleZip швидко віддає 429)")
    had = TelezipSlot.objects.count()
    for i in range(count):
        TelezipSlot.objects.get_or_create(slot=i)
    removed = TelezipSlot.objects.filter(slot__gte=count).delete()[0]
    return (f"слотів було {had} → стало {count} (прибрано {removed}). Діє одразу для "
            "всіх воркерів; TELEZIP_MAX_CONCURRENCY у .env лише засіває таблицю вперше.")


async def _probe_call(method, endpoint, params, body, timeout):
    async with _client(timeout) as tz:
        return await tz.raw(method, endpoint, params=params, json_data=body)


@tool("tz_probe", group="telezip", mutates=True)
def tz_probe(endpoint: str, method: str = "GET", params: str = "", body: str = "",
             timeout: int = 60, chars: int = 1500):
    """Сирий виклик будь-якого ендпоінта TeleZip (v3 і `/v4/...`) — розвідка API.

    Інструменти покривають перевірений контракт; якщо в API з'явиться нове поле
    чи ендпоінт — спитай сервер напряму, а що спрацювало, вживай через `extra`
    у `tz_search`/`tz_stats`, не чекаючи правок коду.
    """
    method = method.upper()
    if method not in ("GET", "POST"):
        raise ToolError("дозволені лише GET і POST")
    if not endpoint.startswith("/"):
        raise ToolError("endpoint має починатись зі «/», напр. /v4/stats")
    data = _run(_probe_call(method, endpoint, _json_arg(params, "params"),
                            _json_arg(body, "body"), int(timeout)))
    if isinstance(data, list):
        keys = sorted({k for item in data[:5] if isinstance(item, dict) for k in item})
        shape = f"list[{len(data)}]" + (f" ключі: {', '.join(keys)}" if keys else "")
    elif isinstance(data, dict):
        shape = f"dict ключі: {', '.join(sorted(data))}"
    else:
        shape = type(data).__name__
    return fmt.joinsec(
        fmt.section(f"{method} {endpoint}", fmt.kv([
            ("params", params or "—"), ("body", fmt.trunc(body, 200) or "—"),
            ("відповідь", shape)])),
        fmt.section("Сирий JSON (обрізано)",
                    fmt.trunc(json.dumps(data, ensure_ascii=False, indent=1), chars)))
