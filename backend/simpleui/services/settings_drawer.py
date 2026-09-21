"""Шухляда «Налаштування» (лише читання) + макет майстра «Зібрати за період».

Одне речення людською мовою, список джерел по регіонах і готові фрази для
асистента. Фрази сформульовані так, щоб асистент виконав їх наявними
MCP-інструментами (source_update / chat_update / run_create / task_update).
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

from analysis.models import (AnalysisTask, MonitorChat, PublishConfig, Source,
                             SourceSubscription)

KIND_HUMAN = {
    Source.KIND_TELEGRAM: "Telegram",
    Source.KIND_RSS: "стрічка сайту",
    Source.KIND_WEB: "сайт",
    Source.KIND_VK: "VK",
}

TELEZIP_PRICE_USD = 0.10       # один запит TeleZip (CLAUDE.md)
TELEZIP_SEC_PER_CALL = 20      # орієнтовна тривалість одного запиту


@dataclass
class Overview:
    sentence: str
    groups: list = field(default_factory=list)   # [{"region": str, "items": [{"name","kind","url"}]}]
    phrases: list = field(default_factory=list)
    n_sources: int = 0
    n_regions: int = 0
    how: str = ""                                # «оновлюється сам…» / «збір за період…»


def _group_by_region(rows):
    """rows: iterable of (region_name|None, item dict) → список груп, без регіону в кінці."""
    groups = {}
    for region, item in rows:
        groups.setdefault(region or "", []).append(item)
    named = sorted((k, v) for k, v in groups.items() if k)
    out = [{"region": k, "items": sorted(v, key=lambda i: i["name"].lower())} for k, v in named]
    if "" in groups:
        out.append({"region": "Без регіону",
                    "items": sorted(groups[""], key=lambda i: i["name"].lower())})
    return out


def _sources(task):
    """(рядки для групування, як оновлюється) залежно від конвеєра."""
    if task.pipeline == AnalysisTask.PIPELINE_INFOSPACE:
        subs = (SourceSubscription.objects.filter(task=task, is_active=True, source__is_active=True)
                .select_related("source", "source__region_subject"))
        rows, intervals = [], []
        for s in subs:
            src = s.source
            rows.append((src.region_subject.name if src.region_subject else None,
                         {"name": src.name, "kind": KIND_HUMAN.get(src.kind, src.kind),
                          "url": src.url}))
            intervals.append(src.poll_interval_sec)
        minutes = (sorted(intervals)[len(intervals) // 2] // 60) if intervals else 0
        how = f"оновлюємо самі кожні ~{minutes} хв" if minutes else "оновлюємо самі"
        return rows, how
    chats = (MonitorChat.objects.filter(task=task, is_active=True)
             .select_related("channel", "channel__region_subject"))
    rows = []
    for c in chats:
        ch = c.channel
        rows.append((ch.region_subject.name if ch.region_subject else None,
                     {"name": ch.title or f"@{ch.username}", "kind": "чат Telegram",
                      "url": f"https://t.me/{ch.username}" if ch.username else ""}))
    if task.pipeline == AnalysisTask.PIPELINE_TGSEARCH:
        how = "читаємо чати безперервно"
    elif rows:
        how = "збираємо за період на ваш запит"
    else:
        how = "шукаємо по всьому Telegram за ключовими словами, збір за період на ваш запит"
    return rows, how


def _publish(task):
    names = list(PublishConfig.objects.filter(task=task, is_active=True)
                 .values_list("name", flat=True))
    return ("публікуємо в «" + "», «".join(names) + "»") if names else "нікуди не публікуємо"


def _phrases(task, groups, n_sources):
    name = task.human_name
    region = groups[0]["region"] if groups and groups[0]["region"] != "Без регіону" else "Дагестану"
    sample = groups[0]["items"][0]["name"] if groups and groups[0]["items"] else "…"
    if task.pipeline == AnalysisTask.PIPELINE_INFOSPACE:
        add = f"Додай джерело @… у «{name}» для {region}"
        rm = f"Прибери джерело «{sample}» з «{name}»"
        silent = f"Покажи, які джерела «{name}» мовчать понад добу"
    else:
        add = f"Додай чат @… у «{name}» для {region}"
        rm = f"Прибери чат «{sample}» з «{name}»"
        silent = f"Покажи стан збору «{name}»"
    return [
        add, rm, silent,
        f"Збери «{name}» за минулий тиждень",
        f"Зміни в «{name}» налаштування «…» на «…»",
        f"Перейменуй секцію «{name}» на «…»",
    ]


def overview(task) -> Overview:
    rows, how = _sources(task)
    groups = _group_by_region(rows)
    n_sources = len(rows)
    n_regions = len([g for g in groups if g["region"] != "Без регіону"])
    if n_sources:
        what = f"Стежимо за {n_sources} джерелами у {n_regions} регіонах"
    else:
        what = "Стежимо за всім Telegram"
    sentence = f"{what}, {how}, {_publish(task)}."
    return Overview(sentence=sentence, groups=groups, n_sources=n_sources,
                    n_regions=n_regions, how=how,
                    phrases=_phrases(task, groups, n_sources))


# --- майстер «Зібрати за період» (макет, без запуску) ------------------------

COLLECT_PRESETS = (
    ("last_week", "Минулий тиждень", 7),
    ("last_month", "Минулий місяць", 30),
)


def collect_estimate(task, days: int, n_sources: int) -> dict:
    """Груба оцінка як у CLAUDE.md: запит TeleZip ≈ $0.10, один запит на чанк
    (і на канал, якщо збір по каналах)."""
    chunk = max(1, task.collect_chunk_days or 1)
    per_channel = max(1, n_sources)
    calls = -(-days // chunk) * per_channel
    return {
        "days": days,
        "calls": calls,
        "price_usd": round(calls * TELEZIP_PRICE_USD, 2),
        "minutes": max(1, round(calls * TELEZIP_SEC_PER_CALL / 60)),
        "continuous": task.pipeline in (AnalysisTask.PIPELINE_INFOSPACE,
                                        AnalysisTask.PIPELINE_TGSEARCH),
    }


def collect_presets(today=None):
    today = today or date.today()
    out = []
    for key, label, days in COLLECT_PRESETS:
        out.append({"key": key, "label": label,
                    "from": today - timedelta(days=days), "to": today - timedelta(days=1),
                    "days": days})
    return out


# --- усі налаштування задачі (лише читання) ----------------------------------
# Групування по етапах беремо з адмінки (AnalysisTaskAdmin._FS_*): один
# перелік полів на конвеєр, без дублювання. Тут лише переклад значень у
# людський вигляд; help_text і технічні описи етапів не показуємо.

_GENERAL_FIELDS = ("display_name", "name", "description", "pipeline", "is_active")
_LONG_TEXT = 160          # довші значення (промпти) згортаємо


def _stage_fieldsets(task):
    from analysis.admin import AnalysisTaskAdmin as A
    return {
        AnalysisTask.PIPELINE_MONITOR: A._FS_MONITOR,
        AnalysisTask.PIPELINE_RESEARCH: A._FS_RESEARCH,
        AnalysisTask.PIPELINE_INFOSPACE: A._FS_INFOSPACE,
        AnalysisTask.PIPELINE_TGSEARCH: A._FS_TGSEARCH,
    }.get(task.pipeline, A._FS_EVENTS)


def _human_value(task, field):
    """(текст, чи довгий). Порожньо → «стандартне» для промптів/моделей, «—» інакше."""
    val = getattr(task, field.name)
    if field.many_to_many:
        names = [getattr(o, "label", None) or str(o) for o in val.all()]
        return (", ".join(names) or "—"), False
    if field.choices:
        return dict(field.choices).get(val, str(val)), False
    if isinstance(val, bool):
        return ("так" if val else "ні"), False
    if isinstance(val, (list, dict)):
        if not val:
            return "—", False
        return (", ".join(map(str, val)) if isinstance(val, list) else str(val)), False
    if val is None or val == "":
        is_text = field.get_internal_type() == "TextField" or "model" in field.name
        return ("стандартне (з коду)" if is_text else "—"), False
    s = str(val)
    return s, len(s) > _LONG_TEXT or "\n" in s


def all_settings(task):
    """[{title, rows:[{label, value, long}]}] — усі поля задачі по етапах конвеєра."""
    meta = {f.name: f for f in task._meta.get_fields() if hasattr(f, "verbose_name")}
    groups = [("Загальне", {"fields": _GENERAL_FIELDS})]
    groups += [(title, opts) for title, opts in _stage_fieldsets(task)]
    out = []
    for title, opts in groups:
        rows = []
        for name in opts.get("fields", ()):
            f = meta.get(name)
            if not f:
                continue
            value, long = _human_value(task, f)
            rows.append({"label": str(f.verbose_name), "value": value, "long": long})
        if rows:
            out.append({"title": title, "rows": rows})
    return out
