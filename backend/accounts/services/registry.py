"""Реєстр акаунтів — єдина точка, де вирішується, ЯКИЙ акаунт дати споживачу
(docs/tg-gateway-plan.md §3.3). Повертає ManagedAccount; сам стан і Telegram —
у gateway.

Ролі:
  collector — полінг джерел infospace з пулу; НЕ дає акаунти, привʼязані до
              стріму (MonitorChat.tg_account) чи публікації
              (PublishConfig.forward_account): той самий auth key у двох
              споживачів — це і є вбиті сесії
  stream    — читання чатів tgsearch; пул без публікаторів
  publisher — лише привʼязаний (PublishConfig.forward_account), з пулу не дається
  service   — warm-up/spam-status/test-bot/адмінка/MCP: будь-який конкретний

Setting `registry_roles_json` (необовʼязково):
  {"collector": {"only_tags": ["збирач"], "exclude_tags": ["кампанія"]}, ...}
"""
from __future__ import annotations

import json
import logging

from django.db.models import Q
from django.utils import timezone as djtz

from .managed import ManagedAccount

logger = logging.getLogger("accounts.registry")

ROLES = ("collector", "stream", "publisher", "service")


class NoAccountAvailable(Exception):
    pass


def get(account_id: int) -> ManagedAccount:
    """Конкретний акаунт без перевірки доступності: адмінка/MCP мають бачити й
    «мертвий», щоб полагодити його через repair()."""
    return ManagedAccount(int(account_id))


def _role_cfg(role: str) -> dict:
    from analysis.models import Setting
    raw = Setting.get("registry_roles_json", "")
    if not raw:
        return {}
    try:
        return (json.loads(raw) or {}).get(role) or {}
    except ValueError:
        logger.error("registry_roles_json: не JSON — ігнорую")
        return {}


def _busy_ids(role: str) -> set[int]:
    from analysis.models import MonitorChat, PublishConfig
    busy: set[int] = set(PublishConfig.objects.exclude(forward_account=None)
                         .values_list("forward_account_id", flat=True))
    if role == "collector":
        busy |= set(MonitorChat.objects.exclude(tg_account=None)
                    .values_list("tg_account_id", flat=True))
    return busy


def candidates(role: str, need_resolve: bool = False) -> list[int]:
    """id акаунтів, придатних для ролі, у стабільному порядку (за id)."""
    from accounts.models import TelegramAccount as A
    if role not in ROLES:
        raise ValueError(f"невідома роль {role!r}")
    if role == "publisher":
        return []
    now = djtz.now()
    qs = (A.objects.filter(is_active=True, is_authenticated=True, state=A.STATE_READY)
          .filter(Q(cooldown_until__isnull=True) | Q(cooldown_until__lte=now))
          .filter(proxy__is_active=True, proxy__is_working=True))
    if need_resolve:
        qs = qs.filter(Q(resolve_exhausted_until__isnull=True)
                       | Q(resolve_exhausted_until__lte=now))
    cfg = _role_cfg(role)
    if cfg.get("only_tags"):
        qs = qs.filter(tags__name__in=cfg["only_tags"])
    if cfg.get("exclude_tags"):
        qs = qs.exclude(tags__name__in=cfg["exclude_tags"])
    busy = _busy_ids(role)
    return [i for i in qs.order_by("id").distinct().values_list("id", flat=True)
            if i not in busy]


def pick(role: str, key: int, shift: int = 0, need_resolve: bool = False) -> ManagedAccount:
    """Стабільний вибір із пулу: (key + shift) % N. Той самий key тримається
    свого акаунта (прогріта сесія, кеш peer); shift збільшує споживач, коли
    акаунт йому відмовив (AccountUnavailable/RateLimited), щоб перескочити."""
    pool = candidates(role, need_resolve=need_resolve)
    if not pool:
        raise NoAccountAvailable(f"роль {role}: немає придатних акаунтів")
    return ManagedAccount(pool[(int(key or 0) + int(shift or 0)) % len(pool)])


def pinned_for(obj) -> ManagedAccount | None:
    """Привʼязаний акаунт обʼєкта (Source.tg_account / MonitorChat.tg_account /
    PublishConfig.forward_account), якщо він є і не деавторизований."""
    acc_id = (getattr(obj, "tg_account_id", None)
              or getattr(obj, "forward_account_id", None))
    if not acc_id:
        return None
    acc = ManagedAccount(acc_id)
    if acc.row.state in (acc.row.STATE_DEAUTHORIZED, acc.row.STATE_BANNED):
        return None
    return acc
