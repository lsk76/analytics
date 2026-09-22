"""Спільне для інструментів: розбір «людських» посилань на об'єкти.

Модель шле те, що бачила у списку: `7`, `#7`, `ethnic-clashes`, `+380…`,
«Дагестан». Тому кожен resolve приймає все це, а на неоднозначності — падає
списком кандидатів, а не мовчазним «першим-ліпшим».
"""
from analysis.services.mcp_api.registry import ToolError


def _pick(qs, ref, label, describe):
    items = list(qs[:12])
    if not items:
        raise ToolError(f"{label} не знайдено за «{ref}»")
    if len(items) > 1:
        opts = "; ".join(describe(o) for o in items)
        raise ToolError(f"«{ref}» неоднозначне ({label}): {opts}")
    return items[0]


def as_int(value, name):
    try:
        return int(str(value).lstrip("#"))
    except (TypeError, ValueError):
        raise ToolError(f"{name}: очікується число, отримано «{value}»")


def resolve_task(ref):
    """AnalysisTask за id / slug / частиною назви (лише видимі цьому користувачу)."""
    from analysis.models import AnalysisTask
    ref = str(ref).strip()
    if ref.lstrip("#").isdigit():
        t = scope_tasks(AnalysisTask.objects).filter(pk=int(ref.lstrip("#"))).first()
        if not t:
            raise ToolError(f"задачі #{ref} немає")
        return t
    exact = scope_tasks(AnalysisTask.objects).filter(slug=ref).first()
    if exact:
        return exact
    qs = scope_tasks(AnalysisTask.objects).filter(name__icontains=ref).order_by("id")
    return _pick(qs, ref, "задачу", lambda t: f"#{t.id} {t.slug}")


def resolve_account(ref):
    """TelegramAccount за id / номером / частиною назви."""
    from accounts.models import TelegramAccount
    ref = str(ref).strip()
    if ref.lstrip("#").isdigit():
        a = scope_accounts(TelegramAccount.objects).filter(pk=int(ref.lstrip("#"))).first()
        if not a:
            raise ToolError(f"акаунта #{ref} немає")
        return a
    qs = scope_accounts(
        TelegramAccount.objects.filter(phone_number__icontains=ref)
        | TelegramAccount.objects.filter(name__icontains=ref)).order_by("id")
    return _pick(qs, ref, "акаунт", lambda a: f"#{a.id} {a.name} {a.phone_number}")


def resolve_accounts(ref):
    """Список акаунтів: `all` / `active` / `problem` або один конкретний."""
    from accounts.models import TelegramAccount
    ref = str(ref).strip().lower()
    if ref in ("all", "усі", "все", "*"):
        return list(scope_accounts(TelegramAccount.objects).order_by("id"))
    if ref in ("active", "активні"):
        return list(scope_accounts(TelegramAccount.objects.filter(is_active=True))
                    .order_by("id"))
    if ref in ("problem", "проблемні"):
        return list(scope_accounts(
            TelegramAccount.objects.filter(is_active=True)
            .exclude(is_authenticated=True, spam_status="free")).order_by("id"))
    return [resolve_account(ref)]


def resolve_source(ref):
    """Source (інформпростір) за id / url / частиною назви."""
    from analysis.models import Source
    ref = str(ref).strip()
    if ref.lstrip("#").isdigit():
        s = Source.objects.filter(pk=int(ref.lstrip("#"))).first()
        if not s:
            raise ToolError(f"джерела #{ref} немає")
        return s
    qs = (Source.objects.filter(channel__url__icontains=ref)
          | Source.objects.filter(channel__title__icontains=ref)).order_by("id")
    return _pick(qs, ref, "джерело", lambda s: f"#{s.id} {s.name} ({s.url})")


def resolve_proxy(ref):
    """Proxy за id / частиною рядка — лише з видимого пулу (`scope_proxies`)."""
    from accounts.models import Proxy
    ref = str(ref).strip()
    if ref.lstrip("#").isdigit():
        p = scope_proxies(Proxy.objects).filter(pk=int(ref.lstrip("#"))).first()
        if not p:
            raise ToolError(f"проксі #{ref} немає")
        return p
    qs = scope_proxies(Proxy.objects.filter(proxy_string__icontains=ref)).order_by("id")
    return _pick(qs, ref, "проксі", lambda p: f"#{p.id} {mask_proxy(p.proxy_string)}")


def resolve_channel(ref):
    """Рядок довідника `Channel` за id / @username / посиланням / частиною назви."""
    from analysis.models import Channel
    from analysis.services.directory import normalize_url
    ref = str(ref).strip()
    if ref.lstrip("#").isdigit():
        c = Channel.objects.filter(pk=int(ref.lstrip("#"))).first()
        if not c:
            raise ToolError(f"каналу #{ref} у довіднику немає")
        return c
    if ref.startswith("@") or "://" in ref or "t.me/" in ref:
        _, norm = normalize_url(ref)
        c = Channel.objects.filter(url=norm).order_by("-fetched_at", "-id").first() if norm else None
        if c is None and norm.startswith("https://t.me/"):
            c = Channel.objects.filter(username__iexact=norm.removeprefix("https://t.me/")) \
                .order_by("-fetched_at", "-id").first()
        if not c:
            raise ToolError(f"каналу «{ref}» у довіднику немає — додати: channel_add")
        return c
    qs = Channel.objects.filter(title__icontains=ref).order_by("-subscribers", "id")
    return _pick(qs, ref, "канал довідника",
                 lambda c: f"#{c.id} @{c.username or '—'} {c.title}")


def resolve_region(ref):
    """Region за канонічною назвою / аліасом (без LLM — лише те, що вже в довіднику)."""
    from analysis.models import Region, RegionAlias
    ref = str(ref).strip()
    if not ref:
        return None
    r = Region.objects.filter(name__iexact=ref).first()
    if r:
        return r
    alias = RegionAlias.objects.filter(raw__iexact=ref).select_related("region").first()
    if alias:
        return alias.region
    qs = Region.objects.filter(name__icontains=ref).order_by("name")
    return _pick(qs, ref, "регіон", lambda r: r.name)


def resolve_chat(ref):
    """MonitorChat за id рядка whitelist або за @username чату (якщо однозначно)."""
    from analysis.models import MonitorChat
    ref = str(ref).strip()
    if ref.lstrip("#").isdigit():
        c = scope_by_task(MonitorChat.objects).filter(pk=int(ref.lstrip("#"))).first()
        if not c:
            raise ToolError(f"чату моніторингу #{ref} немає")
        return c
    qs = scope_by_task(
        MonitorChat.objects.filter(channel__username__iexact=ref.lstrip("@"))
        .select_related("task", "channel")).order_by("id")
    return _pick(qs, ref, "чат моніторингу",
                 lambda c: f"#{c.id} {c.task.slug}/@{c.channel.username}")


def parse_date(value, name):
    from datetime import date, datetime
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ToolError(f"{name}: очікується дата YYYY-MM-DD, отримано «{value}»")


def pollable_source_ids():
    """Підзапит «джерела, які стадія info_collect реально бере в роботу».

    Дзеркало фільтра `infospace/stages.py::_claim_source`: ≥1 активна підписка
    активної infospace-задачі. Без нього «прострочений полінг» рахує й ті
    джерела, які ніхто не передплатив, — їхній next_poll_at лежить у минулому
    вічно, бо воркер їх просто не бере (і діагностика кричала б вовк).
    """
    from analysis.models import SourceSubscription
    return SourceSubscription.objects.filter(
        is_active=True, task__is_active=True,
        task__pipeline="infospace").values("source_id")


# --------------------------------------------------------------------------- видимість
# Те саме розмежування, що й в адмінці: суперюзер бачить усе, решта — свої
# задачі (`owner`) і свої або спільні Telegram-акаунти (`visible_to`). Інакше
# мережевий MCP став би дірою в обхід адмінки.

def scope_tasks(qs):
    from analysis.services.mcp_api.registry import actor
    who = actor()
    return qs if who.is_superuser else qs.filter(owner=who.user)


def scope_by_task(qs, field="task"):
    """Вибірка об'єктів, що належать задачам (чати, підписки, збори, події)."""
    from analysis.services.mcp_api.registry import actor
    who = actor()
    return qs if who.is_superuser else qs.filter(**{f"{field}__owner": who.user})


def scope_accounts(qs):
    from analysis.services.mcp_api.registry import actor
    who = actor()
    return qs if who.is_superuser else qs.visible_to(who.user)


def scope_proxies(qs):
    """Проксі, з якими користувач має справу: вільні або призначені його акаунтам.

    Чужу проксі не можна ні побачити, ні «полагодити» (`proxy_check` лагодить
    сесії реальних акаунтів на ній), а вільну — можна призначити своєму акаунту.
    """
    from django.db.models import Q
    from accounts.models import TelegramAccount
    from analysis.services.mcp_api.registry import actor
    who = actor()
    if who.is_superuser:
        return qs
    mine = TelegramAccount.objects.visible_to(who.user).values("proxy_id")
    return qs.filter(Q(accounts__isnull=True) | Q(id__in=mine)).distinct()


def scope_jobs(qs):
    """Завдання акаунтів (прогрів, тест-бот) — лише по видимих акаунтах."""
    from accounts.models import TelegramAccount
    return qs.filter(account__in=scope_accounts(TelegramAccount.objects))


def mask_proxy(proxy_string: str) -> str:
    """`host:port:user:pass` → пароль сховано для не-адміна.

    Оператор має бачити, ЯКА проксі призначена (хост і логін), але пароль
    лягав би в чат і їхав до провайдера моделі разом із рештою виводу.
    """
    from analysis.services.mcp_api.registry import SCOPE_ADMIN, actor
    text = str(proxy_string or "")
    if not text or actor().can(SCOPE_ADMIN):
        return text
    parts = text.split(":")
    if len(parts) >= 4:
        parts[3] = "***"
    return ":".join(parts)


_SECRET_KEY_HINT = ("token", "secret", "password", "passwd", "api_key", "apikey", "key")


def mask_setting(key: str, value: str) -> str:
    """Значення `Setting` для не-адміна: креденшали в URL і секретні ключі сховано.

    Промпти й прапорці — відкриті, а `infospace_proxy_url` виду
    `http://user:pass@host` без цього віддавав пароль будь-якому читачу.
    """
    import re
    from analysis.services.mcp_api.registry import SCOPE_ADMIN, actor
    text = str(value or "")
    if not text or actor().can(SCOPE_ADMIN):
        return text
    if any(h in key.lower() for h in _SECRET_KEY_HINT):
        return mask_secret(text)
    return re.sub(r"(://[^/@:\s]+:)[^/@\s]+@", r"\1***@", text)


def mask_secret(value: str, keep: int = 6) -> str:
    """Сховати хвіст рядка від не-адміна (проксі з паролем, номер телефону).

    Оператор має бачити, ЯКА проксі призначена, але не її пароль: вивід
    інструмента лягає в чат і їде до провайдера моделі.
    """
    from analysis.services.mcp_api.registry import actor
    from analysis.services.mcp_api.registry import SCOPE_ADMIN
    text = str(value or "")
    if not text or actor().can(SCOPE_ADMIN):
        return text
    return text[:keep] + "***" if len(text) > keep else "***"
