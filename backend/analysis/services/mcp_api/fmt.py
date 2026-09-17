"""Форматери відповідей MCP: компактний текст, а не сирий JSON.

Відповідь інструмента читає МОДЕЛЬ, а не браузер: таблиця з вирівняними
колонками коштує втричі менше токенів, ніж той самий зріз у JSON, і не
провокує «переказ структури» замість відповіді по суті.
"""
from datetime import date, datetime

from django.utils import timezone


def ago(dt, *, empty: str = "—") -> str:
    """«Скільки минуло» одним словом: 40с / 12хв / 3год / 5д / 2тиж."""
    if not dt:
        return empty
    if isinstance(dt, datetime) and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    if isinstance(dt, date) and not isinstance(dt, datetime):
        dt = timezone.make_aware(datetime(dt.year, dt.month, dt.day))
    sec = (timezone.now() - dt).total_seconds()
    sign = "" if sec >= 0 else "-"
    sec = abs(sec)
    if sec < 90:
        return f"{sign}{int(sec)}с"
    if sec < 5400:
        return f"{sign}{int(sec // 60)}хв"
    if sec < 172800:
        return f"{sign}{int(sec // 3600)}год"
    if sec < 1209600:
        return f"{sign}{int(sec // 86400)}д"
    return f"{sign}{int(sec // 604800)}тиж"


def trunc(value, width: int) -> str:
    s = "" if value is None else str(value).replace("\n", " ").strip()
    return s if len(s) <= width else s[: width - 1] + "…"


def table(headers, rows, widths=None) -> str:
    """Вирівняна таблиця. `widths` — обмеження на колонку (None = без обрізки)."""
    headers = [str(h) for h in headers]
    widths = widths or [None] * len(headers)
    cells = [[trunc(c, widths[i]) if widths[i] else ("" if c is None else str(c))
              for i, c in enumerate(r)] for r in rows]
    cols = []
    for i, h in enumerate(headers):
        cols.append(max([len(h)] + [len(r[i]) for r in cells]) if cells else len(h))
    line = "  ".join(h.ljust(cols[i]) for i, h in enumerate(headers)).rstrip()
    out = [line, "  ".join("─" * cols[i] for i in range(len(headers)))]
    for r in cells:
        out.append("  ".join(r[i].ljust(cols[i]) for i in range(len(headers))).rstrip())
    return "\n".join(out)


def kv(pairs, width: int = 0) -> str:
    """Блок «ключ: значення» з вирівняними ключами (порожні значення пропускаємо)."""
    items = [(k, v) for k, v in pairs if v not in (None, "", [], {})]
    if not items:
        return ""
    width = width or max(len(k) for k, _ in items)
    return "\n".join(f"{k.ljust(width)} : {v}" for k, v in items)


def section(title: str, body: str) -> str:
    body = (body or "").rstrip()
    return f"## {title}\n{body}" if body else f"## {title}\n(порожньо)"


def joinsec(*parts) -> str:
    return "\n\n".join(p for p in parts if p)


def flag(ok: bool, good: str = "✓", bad: str = "✗") -> str:
    return good if ok else bad


def pct(part: int, total: int) -> str:
    return f"{round(100 * part / total)}%" if total else "—"
