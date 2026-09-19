#!/usr/bin/env python
"""MCP-сервер керування tg-event-analytics: акаунти ТГ, моніторинги, конвеєри.

Інструменти НЕ дублюють логіку: предметні (`accounts_list`, `run_create`, …)
генеруються з маніфесту Django-шару (`analysis.services.mcp_api`) і виконуються
в контейнері; тут лишається транспорт і кілька суто інфраструктурних дій
(`service_ps`, `service_logs`, `service_restart`, `worker_once`), яким потрібен
сам docker, а не БД.

Запуск (stdio): mcp_server/.venv/bin/python mcp_server/server.py
Налаштування цілі — змінні середовища, див. README.md.
"""
from __future__ import annotations

import json
import sys
from typing import Annotated

from pydantic import Field

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import rpc  # noqa: E402

mcp = MCPServer(
    name="tg-analytics",
    instructions=(
        'Керування сервісом аналітики Telegram: акаунти й проксі, моніторинги '
        '(задачі/чати/джерела), збори, черги конвеєрів, TeleZip, публікація.\n\n'
        'Починай зі `service_health` — він показує, що стоїть і хто це розгрібає. '
        'Що саме тобі дозволено, видно в `tools_manifest`: у кожного інструмента '
        'вказано потрібний scope (mcp:read / mcp:write / mcp:admin).\n\n'
        'TeleZip — чотири інструменти: `tz_find` (пошук повідомлень; `stats=true` '
        'віддає лічильники без викачування), `tz_channels` (пошук каналів), '
        '`tz_users` (профілі людей) і безкоштовний `tz_status` (мережа, глибина '
        'індексу, слоти) — з нього варто починати перед збором.\n'
        'УВАГА, ЦІНА: один виклик до TeleZip ≈ $0.10 незалежно від обсягу, тож '
        '`limit=10000` коштує стільки ж, скільки `limit=10`. Обсяг питай '
        '`tz_find(stats=true)` (1 виклик), а не пошуком навмання; кожна наступна '
        'сторінка (`page_token`) і кожен чанк збору оплачуються окремо.\n'
        'УВАГА, СИНТАКСИС: діалекти протилежні. У `tz_find`/`tz_channels` (API v4) '
        'ПРОБІЛ = І, а АБО — це `|`: «(мигрант | приезж*) (драка | избил)». '
        'У запиті задачі (`task.telezip_query`, збір через `run_create`, API v3) '
        'навпаки: пробіл = АБО, а І — це `+`. Запит у чужому діалекті не падає з '
        'помилкою, а тихо віддає 0 збігів — і все одно коштує $0.10.\n\n'
        'Інструменти з поміткою «ЗМІНЮЄ СТАН» пишуть у БД те, що впливає на роботу '
        'сервісу. Службові позначки (як «востаннє використано» в `account_check`) '
        'такої помітки не дають — критерій саме в наслідках, а не в самому записі.'
    ),
)

PY_TYPES = {"str": str, "int": int, "float": float, "bool": bool}


# --- предметні інструменти: генеруються з маніфесту Django-шару --------------

def _build(spec: dict):
    """Синтезувати функцію з сигнатурою інструмента — з неї MCP будує JSON-схему."""
    args = []
    for p in spec["params"]:
        ann = p["type"] if p["type"] in PY_TYPES else "str"
        if p.get("doc"):
            ann = f"Annotated[{ann}, Field(description={p['doc']!r})]"
        if p["required"]:
            args.append(f"{p['name']}: {ann}")
        elif p["default"] is None:
            args.append(f"{p['name']}: {ann} | None = None")
        else:
            args.append(f"{p['name']}: {ann} = {p['default']!r}")
    src = (f"def {spec['name']}({', '.join(args)}) -> str:\n"
           f"    return _call({spec['name']!r}, dict(locals()))\n")
    ns = {"_call": _call_tool, "Annotated": Annotated, "Field": Field}
    exec(src, ns)  # noqa: S102 — джерело будуємо самі з маніфесту, не з вводу
    return ns[spec["name"]]


def _call_tool(name: str, payload: dict) -> str:
    try:
        return rpc.call(name, {k: v for k, v in payload.items() if v is not None})
    except rpc.RpcError as e:
        return f"⚠ {e}"


def register_domain_tools() -> int:
    for spec in rpc.manifest():
        doc = spec["doc"]
        if spec["mutates"]:
            doc = "ЗМІНЮЄ СТАН. " + doc
        mcp.add_tool(_build(spec), name=spec["name"], description=doc)
    return len(rpc.manifest(refresh=False))


# --- інфраструктура: те, для чого потрібен сам docker ------------------------

def _services() -> list[str]:
    proc = rpc.TARGET.run(rpc.TARGET.compose("config", "--services"), timeout=60)
    return sorted(s.strip() for s in (proc.stdout or "").splitlines() if s.strip())


def _check_services(names: str) -> list[str]:
    known = _services()
    picked, bad = [], []
    for raw in str(names).replace(",", " ").split():
        if raw in known:
            picked.append(raw)
        else:
            bad.append(raw)
    if bad:
        raise ValueError(f"немає таких сервісів: {', '.join(bad)}. "
                         f"Є: {', '.join(known)}")
    if not picked:
        raise ValueError(f"не вказано жодного сервісу. Є: {', '.join(known)}")
    return picked


@mcp.tool(description="Контейнери сервісу: статус, аптайм, хто в рестарт-лупі.")
def service_ps() -> str:
    proc = rpc.TARGET.run(rpc.TARGET.compose("ps", "--all", "--format", "json"), timeout=90)
    if proc.returncode != 0:
        return f"⚠ docker compose ps не вдався ({rpc.TARGET.label}):\n{proc.stderr[-800:]}"
    rows = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append((d.get("Service", "?"), d.get("State", "?"), d.get("Status", ""),
                     d.get("Health", "")))
    if not rows:
        return f"контейнерів не знайдено ({rpc.TARGET.label})"
    w = max(len(r[0]) for r in rows)
    bad = [r for r in rows if r[1] not in ("running", "exited") or "Restarting" in r[2]]
    body = "\n".join(f"{r[0].ljust(w)}  {r[1]:<10} {r[2]}{(' · ' + r[3]) if r[3] else ''}"
                     for r in sorted(rows))
    head = f"Ціль: {rpc.TARGET.label}\nКонтейнерів: {len(rows)}"
    if bad:
        head += f"\n⚠ не в порядку: {', '.join(r[0] for r in bad)}"
    return f"{head}\n\n{body}"


@mcp.tool(description="Логи контейнера (останні рядки, з необов'язковим фільтром). "
                      "service — ім'я сервісу з service_ps, напр. worker-info-collect.")
def service_logs(service: str, lines: int = 80, grep: str = "", since: str = "") -> str:
    try:
        names = _check_services(service)
    except ValueError as e:
        return f"⚠ {e}"
    argv = rpc.TARGET.compose("logs", "--tail", str(max(1, min(int(lines) * 5, 2000))),
                              "--no-color")
    if since:
        argv += ["--since", since]
    argv += names
    proc = rpc.TARGET.run(argv, timeout=120)
    out = (proc.stdout or "") + (proc.stderr or "")
    rows = [ln for ln in out.splitlines() if ln.strip()]
    if grep:
        needle = grep.lower()
        rows = [ln for ln in rows if needle in ln.lower()]
        if not rows:
            return f"у логах {', '.join(names)} немає рядків з «{grep}»"
    rows = rows[-int(lines):]
    return f"{', '.join(names)} — {len(rows)} рядків:\n\n" + "\n".join(rows)


@mcp.tool(description="ЗМІНЮЄ СТАН. Перезапустити сервіси (кілька — через кому). "
                      "recreate=true — пересоздати контейнер: ОБОВ'ЯЗКОВО після зміни "
                      ".env, бо `restart` не перечитує змінні середовища.")
def service_restart(services: str, recreate: bool = False) -> str:
    if rpc.TARGET.readonly:
        return "⚠ сервер у режимі лише-читання (TGA_READONLY=1)"
    try:
        names = _check_services(services)
    except ValueError as e:
        return f"⚠ {e}"
    argv = rpc.TARGET.compose(*(["up", "-d", "--force-recreate"] if recreate
                                else ["restart"]), *names)
    proc = rpc.TARGET.run(argv, timeout=300)
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-1500:]
    verb = "пересоздано" if recreate else "перезапущено"
    status = "✓" if proc.returncode == 0 else f"✗ код {proc.returncode}"
    return f"{status} {verb}: {', '.join(names)}\n\n{tail}"


@mcp.tool(description="ЗМІНЮЄ СТАН. Один прохід стадії конвеєра вручну "
                      "(`run_worker --stage X --once`) — перевірити, чи стадія рухає "
                      "чергу, без чекання на розклад воркера.")
def worker_once(stage: str, task: str = "", timeout: int = 600) -> str:
    if rpc.TARGET.readonly:
        return "⚠ сервер у режимі лише-читання (TGA_READONLY=1)"
    argv = rpc.TARGET.compose("exec", "-T", rpc.TARGET.web, "python", "manage.py",
                              "run_worker", "--stage", stage, "--once")
    if task:
        argv += ["--task", task]
    proc = rpc.TARGET.run(argv, timeout=max(30, min(int(timeout), 900)))
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    status = "✓" if proc.returncode == 0 else f"✗ код {proc.returncode}"
    return f"{status} стадія {stage}{' задача ' + task if task else ''}:\n\n{out[-3000:]}"


def main() -> None:
    n = register_domain_tools()
    print(f"[tg-analytics-mcp] ціль: {rpc.TARGET.label}; предметних інструментів: {n}",
          file=sys.stderr)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
