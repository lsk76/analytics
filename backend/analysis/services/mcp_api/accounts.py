"""Telegram-акаунти, проксі й черги їхніх завдань.

Мережеві інструменти (`account_check`, `account_spam_check`, `proxy_check`,
`account_dialogs`) ходять у Telegram ЗВІДСИ, з контейнера — тобто тією самою
проксі, що й воркери. Тому їхній вердикт збігається з тим, що бачать стадії.
"""
import random
import time

from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import AccountTag, Proxy, TelegramAccount, TestBotJob, WarmUpJob
from accounts.services import registry
from accounts.services.managed import gw_result
from analysis.services.mcp_api import common, fmt
from analysis.services.mcp_api.registry import SCOPE_CREATE, ToolError, actor, tool

SPAM_ICON = {"free": "✓", "limited": "⚠", "frozen": "🧊", "unknown": "?"}


def _proxy_cell(a):
    if not a.proxy_id:
        return "НЕМА"
    p = a.proxy
    return f"#{p.id}{fmt.flag(p.is_working and p.is_active, '', '✗')}"


@tool("accounts_list", group="accounts", params={
      "query": "Частина назви, номера або тега акаунта. Порожньо = усі видимі.",
      "problems_only": "true — лише ті, що потребують уваги: не авторизовані, обмежені SpamBot, без проксі або з мертвою проксі.",
      "limit": "Скільки акаунтів показати."})
def accounts_list(query: str = "", problems_only: bool = False, limit: int = 100):
    """Усі Telegram-акаунти: авторизація, статус SpamBot, проксі, навантаження.

    query — частина назви/номера/тега; problems_only — лише ті, що потребують уваги
    (не авторизовані, обмежені SpamBot-ом, без проксі або з мертвою проксі).
    «чати»/«джерела» — скільки моніторингів читає саме цей акаунт.
    """
    qs = common.scope_accounts(TelegramAccount.objects).select_related("proxy")
    qs = (qs.prefetch_related("tags")
          .annotate(n_chats=Count("monitor_chats", distinct=True),
                    n_sources=Count("sources", distinct=True))
          .order_by("-is_active", "id"))
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(phone_number__icontains=query)
                       | Q(tags__name__icontains=query)).distinct()
    if problems_only:
        qs = qs.filter(is_active=True).filter(
            Q(is_authenticated=False) | Q(spam_status__in=["limited", "frozen"])
            | Q(proxy__isnull=True) | Q(proxy__is_working=False))
    rows = []
    for a in qs[:limit]:
        rows.append([f"#{a.id}", fmt.flag(a.is_active), fmt.flag(a.is_authenticated, "✓", "○"),
                     fmt.trunc(a.name, 22), a.phone_number,
                     SPAM_ICON.get(a.spam_status, "?") + " " + a.spam_status,
                     _proxy_cell(a), a.n_chats or "", a.n_sources or "",
                     fmt.ago(a.last_used_at),
                     fmt.trunc(", ".join(t.name for t in a.tags.all()), 24)])
    if not rows:
        return "акаунтів за цим фільтром немає"
    head = ["id", "акт", "авт", "назва", "номер", "SpamBot", "проксі",
            "чати", "джер", "викор.", "теги"]
    total = common.scope_accounts(TelegramAccount.objects).count()
    return (fmt.table(head, rows)
            + f"\n\nпоказано {len(rows)} із {total} акаунтів у базі")


@tool("account_show", group="accounts", params={"ref": 'Акаунт: числовий id, номер телефону або частина назви — ЛИШЕ ОДИН. Групові ключові слова (all/active/problem) тут не працюють: «all» шукатиметься як частина назви. Неоднозначність — відповість списком кандидатів.'})
def account_show(ref: str):
    """Картка акаунта: конфіг, проксі, що він обслуговує, останні завдання й боти."""
    a = common.resolve_account(ref)
    parts = [fmt.section(f"Акаунт #{a.id} {a.name}", fmt.kv([
        ("номер", a.phone_number),
        ("активний", fmt.flag(a.is_active)),
        ("авторизований", fmt.flag(a.is_authenticated)),
        ("власник", a.user.username if a.user_id else "спільний"),
        ("SpamBot", f"{a.get_spam_status_display()} — {fmt.trunc(a.spam_status_detail, 160)}"
                    f" (перевірено {fmt.ago(a.spam_status_checked_at)})"),
        ("проксі", f"#{a.proxy_id} {common.mask_proxy(a.proxy.proxy_string)} "
                   f"({'працює' if a.proxy.is_working else 'МЕРТВА'}, "
                   f"збоїв {a.proxy.fail_count})" if a.proxy_id else "НЕ ПРИЗНАЧЕНА"),
        ("2FA-пароль", "збережено" if a.two_fa_password else "—"),
        ("пристрій", " · ".join(filter(None, [a.device_model, a.system_version,
                                              a.app_version])) or "дефолт Telethon"),
        ("сесія", f"{len(a.session_string)} симв." if a.session_string else "ПОРОЖНЯ"),
        ("теги", ", ".join(t.name for t in a.tags.all())),
        ("востаннє використано", fmt.ago(a.last_used_at)),
        ("створено", fmt.ago(a.created_at)),
    ]))]

    chats = list(a.monitor_chats.select_related("task", "channel")[:20])
    if chats:
        parts.append(fmt.section(f"Чати моніторингу ({a.monitor_chats.count()})", fmt.table(
            ["chat", "задача", "чат", "стрім", "ост. стрім"],
            [[f"#{c.id}", c.task.slug, f"@{c.channel.username or c.channel_id}",
              fmt.flag(c.stream_enabled, "стрім", "пошук"), fmt.ago(c.last_streamed_at)]
             for c in chats])))
    srcs = list(a.sources.all()[:20])
    if srcs:
        parts.append(fmt.section(f"Джерела інформпростору ({a.sources.count()})", fmt.table(
            ["src", "назва", "інтервал", "ост. успіх", "збоїв"],
            [[f"#{s.id}", fmt.trunc(s.name, 32), f"{s.poll_interval_sec}с",
              fmt.ago(s.last_ok_at), s.consecutive_failures] for s in srcs])))

    bots = list(a.bots.all())
    if bots:
        parts.append(fmt.section("Боти", ", ".join(
            f"@{b.username}{' (токен є)' if b.token else ''}" for b in bots)))

    jobs = list(a.warm_up_jobs.order_by("-created_at")[:5])
    if jobs:
        parts.append(fmt.section("Прогріви", fmt.table(
            ["job", "статус", "каналів", "результат", "коли"],
            [[f"#{j.id}", j.status, len(j.handles or []),
              fmt.trunc(j.error or (j.result or ""), 60), fmt.ago(j.created_at)]
             for j in jobs])))
    tb = list(a.test_bot_jobs.order_by("-created_at")[:5])
    if tb:
        parts.append(fmt.section("Тестові прогони бота", fmt.table(
            ["job", "бот", "статус", "коли"],
            [[f"#{j.id}", f"@{j.bot_username}", j.status, fmt.ago(j.created_at)]
             for j in tb])))
    return fmt.joinsec(*parts)


@tool("account_check", group="accounts", params={
      "ref": 'Акаунт: числовий id, номер телефону або частина назви. Для групових дій — ключові слова: all (усі), active (активні), problem (не авторизовані, обмежені SpamBot, без проксі або з мертвою).',
      "pause": "Пауза між акаунтами в секундах, щоб не бити всі одночасно. Перевірка одного акаунта — 5-20 с, тож групу більшу за 25 акаунтів інструмент не візьме (для `all` на великому пулі бери `problem`)."})
def account_check(ref: str, pause: float = 2.0):
    """Жива перевірка акаунта (connect+get_me через його проксі, нічого не надсилає).

    ref — id/номер/назва, або `all` / `active` / `problem` для групи.

    Позначений як такий, що лише читає: єдиний запис у БД — позначка «востаннє
    використано» для живих акаунтів, вона ні на що в роботі стадій не впливає.
    (На відміну від `proxy_check`, чий вердикт лягає в `is_working`/`fail_count`,
    звідки воркери й беруть проксі.)
    """
    accounts = common.resolve_accounts(ref)
    if len(accounts) > 25:
        raise ToolError(f"{len(accounts)} акаунтів за раз — забагато (перевірка ~5-20с "
                        "на акаунт). Звузь ref або став `problem`.")
    rows, alive = [], 0
    for a in accounts:
        res = registry.get(a.id).check_alive()
        if res.get("ok"):
            alive += 1
            a.last_used_at = timezone.now()
            a.save(update_fields=["last_used_at"])
        rows.append([f"#{a.id}", fmt.trunc(a.name, 20), a.phone_number,
                     fmt.flag(bool(res.get("ok"))), res.get("state", "?"),
                     fmt.trunc(res.get("detail"), 60),
                     f"#{a.proxy_id}" if a.proxy_id else "НЕМА"])
        if pause and a is not accounts[-1]:
            time.sleep(pause)
    return (fmt.table(["id", "назва", "номер", "ок", "стан", "деталі", "проксі"], rows)
            + f"\n\nживих {alive} із {len(accounts)}")


@tool("account_spam_check", group="accounts", mutates=True, params={
      "ref": 'Акаунт: числовий id, номер телефону або частина назви. Для групових дій — ключові слова: all (усі), active (активні), problem (не авторизовані, обмежені SpamBot, без проксі або з мертвою).',
      "pause": "Пауза між акаунтами в секундах. Це НАДСИЛАННЯ повідомлення боту @SpamBot від імені акаунта, тож група обмежена 15."})
def account_spam_check(ref: str, pause: float = 2.0):
    """Запитати @SpamBot про обмеження акаунта (пише боту /start) і зберегти статус.

    Саме обмеження SpamBot-ом, а не мертва проксі, валить резолв юзернеймів.
    ref — id/номер/назва, або `all` / `active` / `problem`.
    """
    accounts = common.resolve_accounts(ref)
    if len(accounts) > 15:
        raise ToolError(f"{len(accounts)} акаунтів за раз — забагато; воркер "
                        "`spam-status` і так перевіряє пул раз на добу.")
    rows = []
    for a in accounts:
        res = gw_result(lambda: registry.get(a.id).spam_status(), status="unknown")
        a.spam_status = res.get("status", "unknown")
        a.spam_status_detail = (res.get("detail") or "")[:300]
        a.spam_status_checked_at = timezone.now()
        a.save(update_fields=["spam_status", "spam_status_detail", "spam_status_checked_at"])
        rows.append([f"#{a.id}", fmt.trunc(a.name, 20),
                     SPAM_ICON.get(a.spam_status, "?") + " " + a.get_spam_status_display(),
                     fmt.trunc(a.spam_status_detail, 90)])
        if pause and a is not accounts[-1]:
            time.sleep(pause)
    return fmt.table(["id", "назва", "статус", "відповідь SpamBot"], rows)


@tool("account_update", group="accounts", mutates=True, params={
      "ref": 'Акаунт: числовий id, номер телефону або частина назви — ЛИШЕ ОДИН. Групові ключові слова (all/active/problem) тут не працюють: «all» шукатиметься як частина назви. Неоднозначність — відповість списком кандидатів.',
      "is_active": "Увімкнути/вимкнути акаунт для роботи стадій.",
      "proxy": "Проксі: числовий id або частина рядка; '-' щоб відвʼязати.",
      "add_tags": "Теги через кому — додати (створюються за потреби).",
      "remove_tags": "Теги через кому — зняти."})
def account_update(ref: str, is_active: bool = None, proxy: str = "",
                   add_tags: str = "", remove_tags: str = ""):
    """Змінити акаунт: активність, проксі, теги (теги — через кому).

    proxy: id/частина рядка проксі, або `-` щоб відв'язати.
    """
    a = common.resolve_account(ref)
    changed = []
    if is_active is not None:
        a.is_active = bool(is_active)
        changed.append(f"активний={fmt.flag(a.is_active)}")
    if proxy:
        if proxy.strip() in ("-", "none", "нема"):
            a.proxy = None
            changed.append("проксі відв'язано")
        else:
            p = common.resolve_proxy(proxy)
            a.proxy = p
            changed.append(f"проксі=#{p.id} {common.mask_proxy(p.proxy_string)}")
    a.save()
    if proxy:
        registry.get(a.id).invalidate()   # gateway перебудує клієнт під нову проксі
    for name in [t.strip() for t in add_tags.split(",") if t.strip()]:
        tag, _ = AccountTag.objects.get_or_create(name=name)
        a.tags.add(tag)
        changed.append(f"+тег «{name}»")
    for name in [t.strip() for t in remove_tags.split(",") if t.strip()]:
        tag = AccountTag.objects.filter(name=name).first()
        if tag:
            a.tags.remove(tag)
            changed.append(f"−тег «{name}»")
    if not changed:
        return f"#{a.id} {a.name}: нічого не змінено (жоден параметр не передано)"
    return f"#{a.id} {a.name}: " + "; ".join(changed)


@tool("account_import", group="accounts", mutates=True, scope=SCOPE_CREATE, params={
      "meta_json": "ВМІСТ файлу <phone>.json з tdata-експорту (JSON-текст як є: phone, app_id, app_hash, device, sdk, app_version, lang_pack, twoFA…).",
      "session_b64": "ВМІСТ файлу <phone>.session (Telethon SQLiteSession) у base64. Альтернатива — session_path.",
      "session_path": "Шлях до .session-файлу ВСЕРЕДИНІ контейнера web (напр. /app/backend/media/import/79990000000.session), якщо файл уже лежить на сервері. Альтернатива — session_b64.",
      "tags": "Теги акаунта через кому (створяться, якщо немає).",
      "proxy": "Проксі: id або частина рядка. Порожньо = призначить воркер/gateway. Без проксі акаунт не працює.",
      "shared": "true — спільний акаунт (бачать усі); false = власник — ти. Суперюзер за замовчуванням додає спільний, решта — свій."})
def account_import(meta_json: str, session_b64: str = "", session_path: str = "",
                   tags: str = "", proxy: str = "", shared: bool = None):
    """Додати Telegram-акаунт із tdata-експорту: <phone>.json + <phone>.session.

    Те саме, що адмінка «Додати акаунт через файли»: SQLiteSession → StringSession,
    device-відбиток переноситься, 2FA — з JSON. Сесія одразу вважається
    авторизованою; далі — `account_check` (жива?) і `account_spam_check`.
    Секрети (session, 2FA) у відповідь не потрапляють.
    """
    import base64
    import binascii
    import json
    import os
    import tempfile
    from django.db import IntegrityError
    from accounts.services.tdata_import import import_tdata_account

    if bool(session_b64) == bool(session_path):
        raise ToolError("дай рівно одне: session_b64 (вміст .session у base64) "
                        "або session_path (шлях у контейнері)")
    try:
        meta = json.loads(meta_json)
    except (TypeError, ValueError) as e:
        raise ToolError(f"meta_json не розбирається як JSON: {e}")
    if not isinstance(meta, dict) or not str(meta.get("phone") or "").strip():
        raise ToolError("meta_json має бути обʼєктом із полем phone (tdata-експорт)")

    who = actor()
    if shared is None:
        shared = who.is_superuser
    owner = None if shared else who.user
    if owner is None and not who.is_superuser:
        raise ToolError("спільний акаунт (shared=true) може додати лише суперюзер")
    tag_names = [t.strip() for t in (tags or "").split(",") if t.strip()]
    p = common.resolve_proxy(proxy) if proxy else None

    tmp_path = ""
    try:
        if session_b64:
            try:
                blob = base64.b64decode(session_b64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise ToolError(f"session_b64 — не base64: {e}")
            if not blob.startswith(b"SQLite format 3"):
                raise ToolError("session_b64 не схожий на .session (SQLite Telethon)")
            with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as tmp:
                tmp.write(blob)
                tmp_path = tmp.name
            path = tmp_path
        else:
            path = session_path
            if not os.path.isfile(path):
                raise ToolError(f"файлу {path} у контейнері немає")
        try:
            a = import_tdata_account(meta, path, owner, tag_names)
        except IntegrityError:
            raise ToolError(f"акаунт із номером {meta.get('phone')} уже є в базі")
        except Exception as e:  # noqa: BLE001 — модель має бачити причину
            raise ToolError(f"не вдалось імпортувати: {type(e).__name__}: {e}")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    if p is not None:
        a.proxy = p
        a.save(update_fields=["proxy"])
    return fmt.joinsec(
        # назва без імені в JSON = номер телефону — у заголовок він не йде
        fmt.section(f"Акаунт #{a.id} {'' if a.name == a.phone_number else a.name} додано", fmt.kv([
            ("телефон", common.mask_secret(a.phone_number, 5)),
            ("власник", "спільний" if a.user_id is None else a.user.username),
            ("проксі", f"#{p.id} {common.mask_proxy(p.proxy_string)}" if p else "— (призначить gateway)"),
            ("теги", ", ".join(tag_names) or "—"),
            ("пристрій", f"{a.device_model} / {a.system_version} / {a.app_version}"),
            ("2FA", "є" if a.two_fa_password else "нема"),
        ])),
        f"Далі: account_check ref={a.id} → account_spam_check ref={a.id}")


@tool("account_warm_up", group="accounts", mutates=True, params={
      "ref": 'Акаунт: числовий id, номер телефону або частина назви. Для групових дій — ключові слова: all (усі), active (активні), problem (не авторизовані, обмежені SpamBot, без проксі або з мертвою).',
      "channels": "На скільки каналів підписати. 0 = випадково 5-10."})
def account_warm_up(ref: str, channels: int = 0):
    """Поставити акаунт(и) у чергу прогріву — підписка на випадкові канали з довідника.

    Непрогрітий акаунт не резолвить чужі юзернейми. Виконує воркер `warm-up`
    (join кожного каналу — секунди, тому це черга, а не синхронна дія).
    channels — скільки каналів (0 = випадково 5-10).
    """
    from analysis.models import Channel
    pool = list(Channel.objects.exclude(username="").order_by()
                .values_list("username", flat=True).distinct())
    if not pool:
        raise ToolError("у довіднику Channel немає каналів з username — нічим гріти")
    out = []
    for a in common.resolve_accounts(ref):
        n = channels or random.randint(5, 10)
        handles = random.sample(pool, min(n, len(pool)))
        job = WarmUpJob.objects.create(account=a, handles=handles)
        out.append(f"#{a.id} {a.name}: job #{job.id}, {len(handles)} каналів")
    return ("Поставлено в чергу (виконує worker-warm-up):\n" + "\n".join(out)
            + "\nПрогрес: account_jobs kind=warm_up")


@tool("account_repair", group="accounts", mutates=True, params={
      "ref": "id/номер/назва, або `problem` — усі в cooldown / needs_proxy."})
def account_repair(ref: str):
    """Полагодити акаунт через gateway: живість → регенерація session-id проксі →
    стан. Те саме робить фоновий ремонт gateway після транспортних збоїв."""
    ref_l = str(ref).strip().lower()
    if ref_l in ("problem", "проблемні"):
        accounts = list(common.scope_accounts(TelegramAccount.objects).filter(
            is_active=True, state__in=["cooldown", "needs_proxy"]).order_by("id"))
    else:
        accounts = common.resolve_accounts(ref)
    if not accounts:
        return "нема кого лагодити"
    if len(accounts) > 15:
        raise ToolError(f"{len(accounts)} акаунтів за раз — забагато (до 60с на акаунт)")
    rows = []
    for a in accounts:
        res = gw_result(lambda: registry.get(a.id).repair())
        a.refresh_from_db()
        rows.append([f"#{a.id}", fmt.trunc(a.name, 20), fmt.flag(bool(res.get("ok"))),
                     res.get("action") or "", a.get_state_display(),
                     fmt.trunc(res.get("error") or res.get("reason") or "", 60)])
    return fmt.table(["id", "акаунт", "ok", "дія", "стан", "деталь"], rows)


@tool("account_dialogs", group="accounts", params={
      "ref": "Акаунт: id, номер або частина назви (лише один, не група).",
      "limit": "Скільки діалогів показати.",
      "kind": "Фільтр типу: канал | група | приват. Порожньо = усі."})
def account_dialogs(ref: str, limit: int = 40, kind: str = ""):
    """Діалоги акаунта наживо (на що підписаний) — перевірка «прогрітості».

    kind — фільтр `канал` / `група` / `приват`.
    """
    a = common.resolve_account(ref)
    res = gw_result(lambda: {"dialogs": registry.get(a.id).dialogs()}, dialogs=[])
    if not res.get("ok"):
        raise ToolError(f"#{a.id} {a.name}: {res.get('error', 'невідома помилка')}")
    dialogs = res.get("dialogs", [])
    if kind:
        dialogs = [d for d in dialogs if d["kind"] == kind]
    by_kind = {}
    for d in dialogs:
        by_kind[d["kind"]] = by_kind.get(d["kind"], 0) + 1
    rows = [[d["kind"], fmt.trunc(d["name"], 44),
             f"@{d['username']}" if d.get("username") else "—"] for d in dialogs[:limit]]
    return (fmt.section(f"Діалоги #{a.id} {a.name}",
                        ", ".join(f"{k}: {v}" for k, v in sorted(by_kind.items())) or "порожньо")
            + "\n\n" + fmt.table(["тип", "назва", "username"], rows))


@tool("account_jobs", group="accounts", params={
      "kind": "Черга: all | warm_up (прогрів) | test_bot (тестовий прогін бота).",
      "status": "Статус завдання. Прогрів: pending | running | done | failed. Тестовий прогін ще має queued і cancelled. Порожньо = усі.",
      "limit": "Скільки завдань показати."})
def account_jobs(kind: str = "all", status: str = "", limit: int = 20):
    """Черги завдань акаунтів: `warm_up` (прогрів) і `test_bot` (тестовий прогін бота)."""
    parts = []
    if kind in ("all", "warm_up"):
        qs = common.scope_jobs(WarmUpJob.objects).select_related("account") \
            .order_by("-created_at")
        if status:
            qs = qs.filter(status=status)
        rows = [[f"#{j.id}", f"#{j.account_id} {fmt.trunc(j.account.name, 18)}", j.status,
                 len(j.handles or []), j.attempts,
                 fmt.trunc(j.error or (j.result or ""), 56), fmt.ago(j.created_at)]
                for j in qs[:limit]]
        parts.append(fmt.section("Прогрів акаунтів", fmt.table(
            ["job", "акаунт", "статус", "каналів", "спроб", "результат", "коли"], rows)
            if rows else "завдань немає"))
    if kind in ("all", "test_bot"):
        qs = common.scope_jobs(TestBotJob.objects).select_related("account") \
            .order_by("-created_at")
        if status:
            qs = qs.filter(status=status)
        rows = [[f"#{j.id}", j.batch_id, f"#{j.account_id} {fmt.trunc(j.account.name, 16)}",
                 f"@{j.bot_username}", j.status, fmt.ago(j.scheduled_at),
                 fmt.trunc(j.error, 40)] for j in qs[:limit]]
        parts.append(fmt.section("Тестовий прогін бота", fmt.table(
            ["job", "запуск", "акаунт", "бот", "статус", "не раніше", "помилка"], rows)
            if rows else "завдань немає"))
    return fmt.joinsec(*parts)


@tool("proxies_list", group="accounts", params={
      "problems_only": "true — лише мертві або зі збоями.",
      "limit": "Скільки проксі показати."})
def proxies_list(problems_only: bool = False, limit: int = 60):
    """Пул проксі: хто живий, скільки збоїв, кому призначені."""
    qs = (common.scope_proxies(Proxy.objects).annotate(n_acc=Count("accounts"))
          .order_by("-is_active", "fail_count", "id"))
    if problems_only:
        qs = qs.filter(Q(is_working=False) | Q(fail_count__gt=0))
    rows = [[f"#{p.id}", fmt.flag(p.is_active), fmt.flag(p.is_working), p.proxy_type,
             fmt.trunc(common.mask_proxy(p.proxy_string), 46), p.fail_count, p.n_acc,
             fmt.ago(p.last_tested_at)] for p in qs[:limit]]
    if not rows:
        return "проксі за цим фільтром немає"
    free = common.scope_proxies(Proxy.objects.filter(is_active=True, accounts__isnull=True)).count()
    return (fmt.table(["id", "акт", "жива", "тип", "рядок", "збоїв", "акаунтів", "перевірено"],
                      rows) + f"\n\nвільних (без акаунтів): {free}")


@tool("proxy_check", group="accounts", mutates=True, params={
      "ref": "Проксі: числовий id або частина рядка; ключові слова all (усі активні) чи broken (лише мертві). Більше за 30 проксі за раз інструмент не візьме — до 12 с на перевірку.",
      "repair": "true — при збої спробувати нову sticky-сесію і зберегти її. false — лише діагноз, але результат перевірки (жива/мертва, лічильник збоїв) усе одно записується, тому інструмент позначений як такий, що змінює стан."})
def proxy_check(ref: str, repair: bool = True):
    """Перевірити проксі наживо; при збої — спробувати нову sticky-сесію (як воркер).

    ref — id/частина рядка, або `all` / `broken`. repair=False — лише діагноз.
    """
    ref_l = str(ref).strip().lower()
    pool = common.scope_proxies(Proxy.objects.filter(is_active=True))
    if ref_l in ("all", "усі", "*"):
        proxies = list(pool.order_by("id"))
    elif ref_l in ("broken", "мертві"):
        proxies = list(pool.filter(is_working=False).order_by("id"))
    else:
        proxies = [common.resolve_proxy(ref)]
    if len(proxies) > 30:
        raise ToolError(f"{len(proxies)} проксі за раз — забагато (до 30с на перевірку)")
    rows = []
    for p in proxies:
        # перевірка йде РЕАЛЬНОЮ сесією акаунта через gateway (repair): проксі без
        # акаунта перевірити нічим — так і кажемо
        acc = common.scope_accounts(
            TelegramAccount.objects.filter(proxy=p, is_active=True, is_authenticated=True)) \
            .order_by("id").first()
        if acc is None:
            p.refresh_from_db()
            ok, note = p.is_working, "немає акаунта на цій проксі — не перевіряли"
        else:
            res = gw_result(lambda: registry.get(acc.id).repair())
            p.refresh_from_db()
            ok = bool(res.get("ok"))
            note = {"alive": "жива", "regenerated": "нова sticky-сесія",
                    "failed": "ремонт не вдався", "skipped": "акаунт пропущено"}.get(
                        res.get("action"), res.get("error") or "")
            if not repair and res.get("action") == "regenerated":
                note += " (repair=false ігнорується: gateway лагодить сам)"
        rows.append([f"#{p.id}", fmt.trunc(common.mask_proxy(p.proxy_string), 46),
                     fmt.flag(ok),
                     p.fail_count, note])
    return fmt.table(["id", "проксі", "жива", "збоїв", "нотатка"], rows)
