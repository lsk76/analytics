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
    """AnalysisTask за id / slug / частиною назви."""
    from analysis.models import AnalysisTask
    ref = str(ref).strip()
    if ref.lstrip("#").isdigit():
        t = AnalysisTask.objects.filter(pk=int(ref.lstrip("#"))).first()
        if not t:
            raise ToolError(f"задачі #{ref} немає")
        return t
    exact = AnalysisTask.objects.filter(slug=ref).first()
    if exact:
        return exact
    qs = AnalysisTask.objects.filter(name__icontains=ref).order_by("id")
    return _pick(qs, ref, "задачу", lambda t: f"#{t.id} {t.slug}")


def resolve_account(ref):
    """TelegramAccount за id / номером / частиною назви."""
    from accounts.models import TelegramAccount
    ref = str(ref).strip()
    if ref.lstrip("#").isdigit():
        a = TelegramAccount.objects.filter(pk=int(ref.lstrip("#"))).first()
        if not a:
            raise ToolError(f"акаунта #{ref} немає")
        return a
    qs = (TelegramAccount.objects.filter(phone_number__icontains=ref)
          | TelegramAccount.objects.filter(name__icontains=ref)).order_by("id")
    return _pick(qs, ref, "акаунт", lambda a: f"#{a.id} {a.name} {a.phone_number}")


def resolve_accounts(ref):
    """Список акаунтів: `all` / `active` / `problem` або один конкретний."""
    from accounts.models import TelegramAccount
    ref = str(ref).strip().lower()
    if ref in ("all", "усі", "все", "*"):
        return list(TelegramAccount.objects.order_by("id"))
    if ref in ("active", "активні"):
        return list(TelegramAccount.objects.filter(is_active=True).order_by("id"))
    if ref in ("problem", "проблемні"):
        return list(TelegramAccount.objects
                    .filter(is_active=True)
                    .exclude(is_authenticated=True, spam_status="free")
                    .order_by("id"))
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
    qs = (Source.objects.filter(url__icontains=ref)
          | Source.objects.filter(name__icontains=ref)).order_by("id")
    return _pick(qs, ref, "джерело", lambda s: f"#{s.id} {s.name} ({s.url})")


def resolve_proxy(ref):
    from accounts.models import Proxy
    ref = str(ref).strip()
    if ref.lstrip("#").isdigit():
        p = Proxy.objects.filter(pk=int(ref.lstrip("#"))).first()
        if not p:
            raise ToolError(f"проксі #{ref} немає")
        return p
    qs = Proxy.objects.filter(proxy_string__icontains=ref).order_by("id")
    return _pick(qs, ref, "проксі", lambda p: f"#{p.id} {p.proxy_string}")


def resolve_chat(ref):
    """MonitorChat за id рядка whitelist або за @username чату (якщо однозначно)."""
    from analysis.models import MonitorChat
    ref = str(ref).strip()
    if ref.lstrip("#").isdigit():
        c = MonitorChat.objects.filter(pk=int(ref.lstrip("#"))).first()
        if not c:
            raise ToolError(f"чату моніторингу #{ref} немає")
        return c
    qs = (MonitorChat.objects.filter(channel__username__iexact=ref.lstrip("@"))
          .select_related("task", "channel").order_by("id"))
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
