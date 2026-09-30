"""Проба промптів monitor: перевірити методику до того, як витрачати гроші.

Навіщо окремий інструмент, а не `prompt_try`: там перевіряється скрін-промпт
інформпростору, де важлива думка про КОНКРЕТНИЙ пост. У monitor цінність інша —
промпт тегувальника задає МЕТОДИКУ, і зламаний промпт видно не по окремому
коментарю, а по двох підсумкових числах: частка коментарів з адресатом критики
і форма розкладки тегів. Канон v4 дає близько 41.7% і виражене домінування
`крит_рег_правит`; пласка розкладка при частці близько 51% означає, що модель
ігнорує ворота й старшинство. Тому тут показуються і вердикти, і підсумок.

Прогін іде БОЙОВИМИ функціями стадій (`monitor_stages._llm_prescreen` і
`_llm_tag`), а не власною копією: інакше перевірка показувала б поведінку
перевірки, а не поведінку конвеєра.

У БД не пишеться нічого: ні теги, ні `_prescreen`, ні чернетки промптів.
"""
from analysis.models import AnalysisTask, Post
from analysis.services.mcp_api import common, fmt
from analysis.services.mcp_api.registry import ToolError, tool

# Стеля вибірки. Прескрін кладе 50 коментарів в один виклик, тегувальник — 10,
# тож 30 коментарів це 1 виклик прескріну і 3 виклики тегувальника.
TRY_MAX = 30

# Орієнтири канону v4 (docs/AI-GUIDE.md §3): за ними й дивимось, чи промпт
# працює як методика, чи як щось інше.
CANON_SHARE = 41.7
CANON_TOP_TAG = "крит_рег_правит"


def _parse_ids(spec: str) -> list[int]:
    out = []
    for part in (spec or "").replace(";", ",").split(","):
        part = part.strip().lstrip("#")
        if not part:
            continue
        if not part.isdigit():
            raise ToolError(f"posts: очікується id поста, отримано «{part}»")
        out.append(int(part))
    return out


def _require_monitor(task):
    if task.pipeline != AnalysisTask.PIPELINE_MONITOR:
        raise ToolError(
            f"«{task.slug}» — конвеєр {task.pipeline}. Проба прескрін- і "
            "тегер-промптів є лише для monitor. Для infospace — prompt_try.")
    return task


def _sample(task, limit, post_ids, date_from, date_to, positives_only=False):
    """Випадкова вибірка зібраних коментарів із текстом.

    Випадкова, а не «найсвіжіші»: останні N постів — це майже завжди один-два
    найактивніші чати, і промпт на них перевіряється лише на одному регіоні.
    """
    # select_related обовʼязковий: _llm_tag читає регіон поста вже в
    # асинхронному контексті, де лінива підвантажка FK падає з
    # SynchronousOnlyOperation. Те саме робить і бойова стадія mon_tag.
    qs = (Post.objects.filter(task=task).exclude(text="")
          .select_related("channel", "region_subject", "channel__region_subject"))
    if post_ids:
        found = list(qs.filter(id__in=post_ids))
        missing = sorted(set(post_ids) - {p.id for p in found})
        return found[:limit], missing, False
    if date_from:
        qs = qs.filter(posted_at__date__gte=common.parse_date(date_from, "date_from"))
    if date_to:
        qs = qs.filter(posted_at__date__lte=common.parse_date(date_to, "date_to"))
    if positives_only:
        # Тегувальник у конвеєрі бачить ЛИШЕ прескрін-позитиви. Якщо перевіряти
        # його на випадкових постах, він отримає майже суцільний побутовий шум,
        # частка вийде близька до нуля і скаже не про промпт, а про вибірку.
        pos = qs.filter(classification__has_key="_prescreen",
                        classification___prescreen__could_be_criticism=True)
        if pos.exists():
            return list(pos.order_by("?")[:limit]), [], True
        return list(qs.order_by("?")[:limit]), [], False
    return list(qs.order_by("?")[:limit]), [], False


def _share(n, total):
    return f"{100.0 * n / total:.1f}%" if total else "—"


@tool("monitor_prompt_try", mutates=True, group="monitoring", params={
      "task": "Задача monitor: id, slug або частина назви.",
      "stage": "Що перевіряємо: tagger (дефолт) | prescreen | both.",
      "limit": f"Скільки коментарів узяти (1..{TRY_MAX}). Прескрін кладе 50 в один "
               "виклик, тегувальник 10 — звідси й ціна перевірки.",
      "posts": "Id постів через кому — коли треба саме ці коментарі (напр. ті, на "
               "яких промпт раніше помилявся). Порожньо = випадкова вибірка.",
      "date_from": "Нижня межа дати коментаря, YYYY-MM-DD.",
      "date_to": "Верхня межа дати коментаря, YYYY-MM-DD.",
      "prescreen_prompt": "Чернетка прескрін-промпта на цей виклик. Порожньо = те, "
                          "що в задачі. Не зберігається.",
      "tagger_prompt": "Чернетка промпта тегувальника на цей виклик. Порожньо = те, "
                       "що в задачі. Не зберігається.",
      "model": "Модель на цей виклик. Порожньо = як у задачі (прескрін — "
               "prescreen_model, тегувальник — llm_model).",
      })
def monitor_prompt_try(task: str, stage: str = "tagger", limit: int = 10,
                       posts: str = "", date_from: str = "", date_to: str = "",
                       prescreen_prompt: str = "", tagger_prompt: str = "",
                       model: str = ""):
    """Прогнати чернетку прескрін- або тегер-промпта на кількох зібраних коментарях.

    Показує вердикт по кожному коментарю І підсумок, за яким видно, чи промпт
    працює як методика: частку коментарів з адресатом критики та розкладку тегів.
    Канон v4 — близько 41.7% і домінування `крит_рег_правит`; пласка розкладка
    при ~51% означає, що ворота промпта не працюють.

    Нічого не зберігає: ні теги, ні позначку прескріну, ні самі чернетки.
    Позначений як такий, що змінює стан, бо кличе LLM (і тому недоступний у
    режимі лише-читання). Зберегти те, що сподобалось: task_update
    prescreen_prompt=… / tagger_prompt=…

    ВАЖЛИВО про тегувальника: у конвеєрі його виконують Claude-агенти (файлові
    пачки, воркер mon_agent_tag), а тут промпт іде в LLM через OpenRouter тим
    самим кодом, що й стадія mon_tag. Тобто перевіряється ПРОМПТ, а не виконавець;
    для звірки самого виконавця дивись docs/agent-runner-plan.md.
    """
    from analysis.services import monitor_stages as MS

    mode = (stage or "tagger").strip().lower()
    if mode not in ("tagger", "prescreen", "both"):
        raise ToolError("stage: tagger | prescreen | both")
    n = common.as_int(limit, "limit")
    if not 1 <= n <= TRY_MAX:
        raise ToolError(f"limit: від 1 до {TRY_MAX}, прийшло {n}")

    row = _require_monitor(common.resolve_task(task))
    ids = _parse_ids(posts)
    sample, missing, from_pos = _sample(row, n, ids, date_from, date_to,
                                        positives_only=(mode == "tagger"))
    head = [f"#{row.id} {row.slug}", f"коментарів у вибірці: {len(sample)}"
            + (" (прескрін-позитиви — те саме, що бачить тегувальник у конвеєрі)"
               if from_pos else
               " (випадкові коментарі: позначок прескріну в задачі немає, тож це "
               "переважно побутовий шум — частка нижче канону тут нормальна)"
               if mode == "tagger" and not ids else "")]
    if missing:
        head.append("немає поста або порожній текст: "
                    + ", ".join(f"#{i}" for i in missing))
    if not sample:
        return fmt.joinsec("\n".join(head),
                           "Немає коментарів із текстом — LLM не викликався. "
                           "Розширь період або передай posts=id.")

    parts = []
    calls = 0

    if mode in ("prescreen", "both"):
        sys_p = prescreen_prompt or row.prescreen_prompt or MS.PRESCREEN_SYSTEM_PROMPT_COMPACT
        mdl_p = model or row.prescreen_model or row.llm_model or MS.settings.LLM_MODEL
        batches = [sample[i:i + MS.PRESCREEN_SUB]
                   for i in range(0, len(sample), MS.PRESCREEN_SUB)]
        calls += len(batches)
        verdicts = MS.asyncio.run(MS._llm_prescreen(batches, mdl_p, sys_p))
        yes = [p for p in sample
               if (verdicts.get(p.id) or {}).get("could_be_criticism")]
        lines = [f"#{p.id} {'ТАК' if (verdicts.get(p.id) or {}).get('could_be_criticism') else 'ні'}"
                 f"  {fmt.trunc((p.text or '').replace(chr(10), ' '), 110)}"
                 for p in sample]
        parts.append(fmt.section(
            f"Прескрін ({'чернетка' if prescreen_prompt else 'промпт задачі'}, "
            f"модель {mdl_p})",
            "\n".join(lines + [
                "",
                f"пропущено далі: {len(yes)} із {len(sample)} ({_share(len(yes), len(sample))})",
                "прескрін лише відсіює очевидний шум: надто низька частка означає, "
                "що він ріже критику, і чисельник метрики буде занижений"])))

    if mode in ("tagger", "both"):
        sys_t = tagger_prompt or row.tagger_prompt or MS.TAGGER_SYSTEM_PROMPT
        mdl_t = model or row.llm_model or MS.settings.LLM_MODEL
        batches = [sample[i:i + MS.TAG_SUB]
                   for i in range(0, len(sample), MS.TAG_SUB)]
        calls += len(batches)
        verdicts = MS.asyncio.run(
            MS._llm_tag(batches, MS._region_of(row), mdl_t, sys_t))
        lines, tally, rel = [], {}, 0
        for p in sample:
            v = verdicts.get(p.id)
            if not isinstance(v, dict):
                lines.append(f"#{p.id} вердикту немає (бита або порожня відповідь)")
                continue
            targets = v.get("criticism_target") or []
            rel += bool(targets)
            for t in targets:
                tally[t] = tally.get(t, 0) + 1
            lines.append(
                f"#{p.id} {', '.join(targets) if targets else '—'}"
                f"  [рівень: {', '.join(v.get('crit_level') or []) or '—'}"
                f" | ЄР: {v.get('er_crit') or '—'}]"
                f"\n   {fmt.trunc((p.text or '').replace(chr(10), ' '), 110)}")
        n_verdicts = sum(1 for p in sample if isinstance(verdicts.get(p.id), dict))
        top = sorted(tally.items(), key=lambda kv: -kv[1])
        share = 100.0 * rel / len(sample) if sample else 0.0
        verdict_note = []
        if not n_verdicts:
            verdict_note.append(
                f"МОДЕЛЬ {mdl_t} НЕ ВЕРНУЛА ЖОДНОГО ВЕРДИКТУ. Стадія просить "
                "додати до кожного елемента поле \"id\" з підпису [id=...], і "
                "слабші моделі цю вимогу ігнорують — тоді конвеєр не може "
                "зіставити відповідь із коментарями і шле пости в retry. "
                "Спробуй model=anthropic/claude-haiku-4.5: саме Haiku зафіксований "
                "методикою як виконавець тегування.")
        elif n_verdicts < len(sample):
            verdict_note.append(f"вердикт є лише на {n_verdicts} із {len(sample)} — "
                                "решта в конвеєрі пішла б у retry")
        if top and top[0][0] != CANON_TOP_TAG:
            verdict_note.append(
                f"переважає не {CANON_TOP_TAG}, а {top[0][0]} — на малій вибірці "
                "це ще не діагноз, але на сотні коментарів так бути не повинно")
        if len(top) > 3 and top[0][1] - top[-1][1] <= 1:
            verdict_note.append("розкладка майже пласка — ознака того, що ворота "
                                "промпта не працюють")
        if share > CANON_SHARE + 8:
            verdict_note.append(f"частка {share:.1f}% помітно вище канону "
                                f"{CANON_SHARE}% — промпт тегує зайве")
        parts.append(fmt.section(
            f"Тегувальник ({'чернетка' if tagger_prompt else 'промпт задачі'}, "
            f"модель {mdl_t})",
            "\n".join(lines + [
                "",
                f"з адресатом критики: {rel} із {len(sample)} ({share:.1f}%), "
                f"канон {CANON_SHARE}%",
                "розкладка: " + (", ".join(f"{k} {v}" for k, v in top[:8]) or "—"),
                *(["⚠ " + s for s in verdict_note] or
                  ["схоже на канон: частка в межах, розкладка не пласка, "
                   "вердикт є на кожен коментар"])])))

    return fmt.joinsec(
        "\n".join(head + [f"викликів LLM: {calls}",
                          "у БД не записано, чернетки не збережено"]),
        *parts,
        "Зберегти: task_update prescreen_prompt=… / tagger_prompt=…; "
        "перетегувати вже зібране після зміни — run_tagging на потрібний період.")
