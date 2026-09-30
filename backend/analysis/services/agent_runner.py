"""Агент-ранер: перетворює `awaiting_agent` на `*_done.json` без людини.

Конвеєр автоматичний з обох боків від цього місця: `mon_runs` готує пачки й
ставить запуск в `awaiting_agent`, а `_try_ingest` бачить `*_done.json`, робить
інжест і створює події. Посередині досі була людина, яка просила Claude Code
розмітити пачки, — через це весь конвеєр залежав від чиєїсь присутності.
Ця стадія закриває саме цей проміжок і більше нічого не змінює.

Чому окремий процес, а не виклик LLM через `llm.make_client()` (як `mon_tag`):
виконавець розмітки зафіксований методикою — Haiku-агенти. Gemini на каноні v4
ігнорує ворота й старшинство (51% замість 41.7% і пласка розкладка тегів), тому
модель тут прибита в коді, а не береться з дефолту задачі. Зміна виконавця між
періодами ламає динаміку непомітно: цифри лишаються правдоподібними, а
порівнювати їх уже не можна. Див. docs/agent-runner-plan.md і AI-GUIDE §3.

Замок — на файлах, а не в БД. `ResearchRun.stats` для цього не годиться: його
перезаписує `mon_runs` (`save(update_fields=["stats"])` зі знімка, зробленого на
початку тіку), тож позначку про оренду він затер би. Файловий замок живе в теці
запуску, яка змонтована в усі контейнери, і створюється атомарно (`O_EXCL`), а
не перевіркою «чи існує» перед записом.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from analysis.models import ResearchRun
from analysis.pilot.prompts import build_user_prompt
from analysis.services import pipeline_runs

log = logging.getLogger(__name__)

# Виконавець прибитий свідомо (див. докстрінг). Змінна середовища лишена, щоб
# заміну можна було зробити усвідомлено й одним місцем, а не випадково.
MODEL = os.getenv("AGENT_TAG_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "/usr/local/bin/claude")

# Одна пачка з 50 коментарів — близько 2 хвилин очікування моделі й майже нуль
# процесорного часу, тому пачки йдуть паралельно. 6 — як TAG_CONCURRENCY у
# mon_tag: далі впирається не в нас, а в ліміти боку моделі.
CONCURRENCY = int(os.getenv("AGENT_TAG_CONCURRENCY", "6"))
BATCH_TIMEOUT = int(os.getenv("AGENT_TAG_TIMEOUT", "900"))

# Стеля витрат на ОДНУ пачку. У неї входять і накладні витрати сесії, тому
# ставити «очікувану ціну пачки» не можна — перевірено: 0.05 не вистачає навіть
# на промпт з одного слова.
BUDGET_USD = os.getenv("AGENT_TAG_BUDGET_USD", "1.00")

# Скільки пачок обробляємо за один тік: обмежує ціну однієї помилки, навіть
# якщо в черзі опиниться запуск на 946 пачок (таке вже було — запуск #30).
MAX_BATCHES_PER_TICK = int(os.getenv("AGENT_TAG_MAX_BATCHES", "12"))

# Пачка, яка тричі не дала валідної відповіді, лишається незробленою й не
# блокує решту: інжест однаково не піде, поки не готові всі пачки, тож проблема
# буде видна в run_show, а не тихо зʼїсть гроші в циклі.
MAX_ATTEMPTS = 3

LOCK_TTL = 3600           # прострочений замок запуску вважаємо мертвим
CLAIM_TTL = BATCH_TIMEOUT * 2


# --------------------------------------------------------------------- замки

def _take(path: Path, payload: str, ttl: int) -> bool:
    """Атомарно створити файл-замок. False — замок уже чийсь і живий."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
        try:
            if time.time() - path.stat().st_mtime < ttl:
                return False
            path.unlink()                      # прострочений — забираємо собі
        except FileNotFoundError:
            pass                               # хтось звільнив раніше за нас
        return _take(path, payload, ttl)
    with os.fdopen(fd, "w") as fh:
        fh.write(payload)
    return True


def _release(path: Path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------- промпт

def _system_prompt(bdir: Path) -> str:
    """Промпт тегувальника з SYSTEM_PROMPT.md.

    Файл — markdown із шапкою (задача, дата) і самим промптом у блоці ```.
    У модель має піти лише промпт: шапка з'їдала б контекст і додавала тексту,
    якого канон не бачив.
    """
    raw = (bdir / "SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    parts = raw.split("```")
    return (parts[1] if len(parts) >= 3 else raw).strip()


def _extract_json(text: str) -> dict:
    """Витягти JSON із відповіді.

    Промпт вимагає строгий JSON без markdown, і модель це порушує: додає
    вступне речення й обгортає відповідь у ```json. Тому беремо найширший
    фрагмент у фігурних дужках, а не довіряємо послуху.
    """
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("у відповіді немає JSON")
    return json.loads(m.group(0))


def _verdicts(raw: str, items: list[dict]) -> list[dict]:
    """Відповідь моделі → елементи для monitor_ingest_tags.

    Модель відповідає ПОЗИЦІЯМИ (`"i": 0`), а інжест чекає `id` поста, тож
    зіставлення обов'язкове. Повнота перевіряється арифметично: агент схильний
    тихо скорочувати пачку, а неповний done-файл дасть заниження частки, яке
    виглядатиме як справжнє падіння критики.
    """
    got = _extract_json(raw).get("items") or []
    if len(got) != len(items):
        raise ValueError(f"елементів {len(got)}, а на вході {len(items)}")
    idx = [v.get("i") for v in got]
    if sorted(idx) != list(range(len(items))):
        raise ValueError("індекси не покривають пачку рівно один раз")
    out = []
    for v in got:
        verdict = {k: val for k, val in v.items() if k != "i"}
        out.append({"id": items[v["i"]]["id"], "verdict": verdict})
    return out


# -------------------------------------------------------------------- пачка

def _ask_claude(system_path: Path, user_text: str, cwd: Path) -> str:
    """Один запуск Claude на пачку.

    Права навмисно вузькі: жодних інструментів, робоча тека — тека запуску.
    Сесія працює на проді, де база на сотні тисяч подій і Telegram-сесії, тому
    агент отримує рівно те, що потрібно для розмітки тексту, і нічого більше.
    Вкладені агенти заборонені — інакше одна хвиля породжує лавину запусків.
    """
    argv = [CLAUDE_BIN, "-p", user_text,
            "--system-prompt-file", str(system_path),
            "--model", MODEL,
            "--max-budget-usd", BUDGET_USD,
            "--disallowedTools", "Task,Bash,Edit,Write,WebFetch,WebSearch"]
    proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                          timeout=BATCH_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"claude код {proc.returncode}: "
                           f"{(proc.stderr or proc.stdout or '')[-300:]}")
    return proc.stdout or ""


def _attempts(claim: Path) -> int:
    try:
        return int(json.loads(claim.read_text(encoding="utf-8")).get("attempt", 0))
    except Exception:  # noqa: BLE001 — зміст замка не критичний, лише лічильник
        return 0


def tag_batch(bdir: Path, batch: Path, system_path: Path) -> bool:
    """Розмітити одну пачку. True — файл `*_done.json` записано."""
    done = batch.with_name(batch.stem + "_done.json")
    if done.exists():
        return False
    claim = batch.with_suffix(".claim")
    attempt = _attempts(claim) + 1
    if not _take(claim, json.dumps({"attempt": attempt, "pid": os.getpid()}),
                 CLAIM_TTL):
        return False
    try:
        if attempt > MAX_ATTEMPTS:
            log.error("agent_tag: %s — %s спроб без валідної відповіді, кидаю",
                      batch.name, attempt - 1)
            return False
        data = json.loads(batch.read_text(encoding="utf-8"))
        items = data.get("items") or []
        raw = _ask_claude(system_path, build_user_prompt(items), bdir)
        payload = {"meta": {"batch_id": (data.get("meta") or {}).get("batch_id"),
                            "model": MODEL, "by": "agent_runner"},
                   "items": _verdicts(raw, items)}
        # спершу у тимчасовий файл, потім перейменування: інжест не має
        # побачити половину файла, якщо процес помре під час запису
        tmp = done.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, done)
        log.info("agent_tag: %s → %s вердиктів", batch.name, len(payload["items"]))
        _release(claim)
        return True
    except Exception as e:  # noqa: BLE001 — одна пачка не валить хвилю
        log.warning("agent_tag: %s спроба %s не вдалась: %s", batch.name, attempt, e)
        # замок лишаємо зі збільшеним лічильником, щоб спроби не були вічними
        try:
            claim.write_text(json.dumps({"attempt": attempt, "error": str(e)[:300]}),
                             encoding="utf-8")
        except OSError:
            pass
        return False


# ------------------------------------------------------------------- стадія

def _pending(bdir: Path) -> list[Path]:
    """Пачки без готового результату, у порядку номерів."""
    out = []
    for b in sorted(bdir.glob("batch_*.json")):
        if b.name.endswith("_done.json") or b.name.endswith(".tmp"):
            continue
        if not b.with_name(b.stem + "_done.json").exists():
            out.append(b)
    return out


def mon_agent_tag_once(task) -> bool:
    """Розмітити пачки всіх запусків задачі, що чекають агента.

    Контракт run_worker: True — щось зробили (тік не спить).
    """
    did = False
    for run in (ResearchRun.objects
                .filter(task=task, status="awaiting_agent").order_by("id")):
        bdir = Path(pipeline_runs.batch_dir(run))
        if not bdir.is_dir():
            log.warning("agent_tag: запуск #%s чекає агента, але теки %s немає",
                        run.id, bdir)
            continue
        lock = bdir / ".agent.lock"
        if not _take(lock, json.dumps({"pid": os.getpid(), "run": run.id}), LOCK_TTL):
            continue                      # цей запуск уже хтось розмічає
        try:
            batches = _pending(bdir)[:MAX_BATCHES_PER_TICK]
            if not batches:
                continue
            system_path = bdir / ".system_prompt.txt"
            system_path.write_text(_system_prompt(bdir), encoding="utf-8")
            log.info("agent_tag: запуск #%s — %s пачок у роботу (модель %s, "
                     "паралельно %s)", run.id, len(batches), MODEL, CONCURRENCY)
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
                results = list(pool.map(
                    lambda b: tag_batch(bdir, b, system_path), batches))
            ok = sum(1 for r in results if r)
            log.info("agent_tag: запуск #%s — готово %s із %s", run.id, ok, len(batches))
            did = did or bool(ok)
        finally:
            _release(lock)
    return did


STAGE_RUNNERS = {"mon_agent_tag": mon_agent_tag_once}
