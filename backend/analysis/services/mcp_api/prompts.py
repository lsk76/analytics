"""Проба промптів infospace і обмежене перетегування вже зібраних подій.

Повний скид черги (`rescreen_task_now`) сюди свідомо не винесено: він видаляє
ВСІ події задачі і кличе LLM на кожен пост. Порядок тут інший: чернетка на
кількох постах (`prompt_try`) → зберегти правило (`task_update`) → перетегувати
обмежену вибірку (`posts_retag`).
"""
import asyncio
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from analysis.models import AnalysisTask, Post
from analysis.services.mcp_api import common, fmt
from analysis.services.mcp_api.registry import ToolError, tool

TRY_MAX = 8
RETAG_MAX = 25


def _require_infospace(task):
    if task.pipeline != AnalysisTask.PIPELINE_INFOSPACE:
        raise ToolError(
            f"«{task.slug}» — конвеєр {task.pipeline}. Проба скрін-промпта і "
            "перетегування зібраного — лише infospace (info_screen_prompt + "
            "info_tagger_prompt). Інший конвеєр: task_show, потім task_update "
            "його промпта.")


def _parse_ids(spec: str) -> list[int]:
    if not (spec or "").strip():
        return []
    out = []
    for part in spec.replace(";", ",").split(","):
        part = part.strip().lstrip("#")
        if not part:
            continue
        if not part.isdigit():
            raise ToolError(f"posts: очікується id поста, отримано «{part}»")
        out.append(int(part))
    return out


def _check_limit(limit: int, cap: int, name: str):
    if limit < 1 or limit > cap:
        raise ToolError(
            f"{name}: limit має бути 1..{cap}. Кожен пост — окремий виклик LLM; "
            f"стеля {cap}, щоб не прогнати всю задачу одним викликом.")


def _cutoff(days: int):
    if days < 0:
        raise ToolError("days: невід'ємне число діб; 0 = без обмеження дати")
    if not days:
        return None
    return timezone.now() - timedelta(days=int(days))


def _dated(qs, *, days: int, date_from: str, date_to: str, on_event: bool):
    """Вікно вибірки. Явні дати замінюють `days`. Поле — дата події або поста."""
    start = common.parse_date(date_from, "date_from") if date_from else None
    end = common.parse_date(date_to, "date_to") if date_to else None
    if start and end and start > end:
        raise ToolError("date_from пізніше за date_to")
    if start is None and end is None:
        cutoff = _cutoff(days)
        if cutoff is None:
            return qs
        if on_event:
            return qs.filter(event__event_date__gte=cutoff.date())
        return qs.filter(posted_at__gte=cutoff)
    if on_event:
        if start:
            qs = qs.filter(event__event_date__gte=start)
        if end:
            qs = qs.filter(event__event_date__lte=end)
        return qs
    if start:
        qs = qs.filter(posted_at__date__gte=start)
    if end:
        qs = qs.filter(posted_at__date__lte=end)
    return qs


def _stock(task):
    """Скільки done-постів ще мають текст і скільки з них стали подіями."""
    done = Post.objects.filter(task=task, stage=Post.STAGE_DONE)
    n_done = done.count()
    n_text = done.exclude(text="").count()
    n_events = (Post.objects.filter(task=task, event__isnull=False).exclude(text="")
                .order_by().values("event_id").distinct().count())
    return n_done, n_text, n_events


def _draft(task, info_screen_prompt: str, info_tagger_prompt: str):
    """Копія полів промпта на цей виклик. У задачі нічого не зберігається."""
    class _View:
        pass
    view = _View()
    view.info_screen_prompt = task.info_screen_prompt
    view.info_tagger_prompt = task.info_tagger_prompt
    view.tag_categories = task.tag_categories
    notes = []
    if info_screen_prompt:
        view.info_screen_prompt = "" if info_screen_prompt.strip() == "-" else info_screen_prompt
        notes.append("чернетка info_screen_prompt" if view.info_screen_prompt
                     else "чернетка: info_screen_prompt порожній (дефолт із коду)")
    if info_tagger_prompt:
        view.info_tagger_prompt = "" if info_tagger_prompt.strip() == "-" else info_tagger_prompt
        notes.append("чернетка info_tagger_prompt" if view.info_tagger_prompt
                     else "чернетка: info_tagger_prompt порожній (лише підказки категорій)")
    if not notes:
        notes.append("промпт із задачі, як його бачить воркер зараз")
    return view, notes


def _raw_tags(verdict) -> str:
    tags = verdict.get("tags") if isinstance(verdict, dict) else None
    if isinstance(tags, dict):
        parts = []
        for key, vals in tags.items():
            if isinstance(vals, str):
                vals = [vals]
            if isinstance(vals, (list, tuple)):
                shown = "+".join(str(v) for v in vals if v)
                if shown:
                    parts.append(f"{key}:{shown}")
        return ", ".join(parts) or "—"
    if isinstance(tags, (list, tuple)):
        return ", ".join(str(v) for v in tags) or "—"
    return "—"


def _event_tags(event, keys) -> str:
    if event is None:
        return "немає події"
    rows = [t for t in event.tags.all() if not keys or t.category in keys]
    if not rows:
        return "—"
    by = {}
    for t in rows:
        by.setdefault(t.category, []).append(t.name)
    return ", ".join(f"{k}:{'+'.join(v)}" for k, v in sorted(by.items()))


def _resolve_tags(task, verdict):
    """Канонічні теги з вердикту — ті самі правила, що при створенні події."""
    from analysis.services import tags as tag_service
    cls_tags = (verdict or {}).get("tags") or {}
    flat = []
    if isinstance(cls_tags, (list, tuple)):
        flat, cls_tags = list(cls_tags), {}
    elif not isinstance(cls_tags, dict):
        cls_tags = {}
    chosen, dropped = [], []
    for c in task.tag_categories.all():
        vals = cls_tags.get(c.key) or []
        if isinstance(vals, str):
            vals = [vals]
        elif not isinstance(vals, (list, tuple)):
            vals = [vals] if vals else []
        if flat and c.closed:
            vals = list(vals) + list(flat)
        for v in vals:
            if not v:
                continue
            obj = tag_service.resolve(c.key, str(v))
            if obj:
                chosen.append(obj)
            else:
                dropped.append(f"{c.key}:{v}")
    return tag_service.collapse_scales(chosen), dropped


def _screen(task, posts, system):
    from analysis.services import llm
    from analysis.services.infospace import stages as info_stages
    model = task.info_screen_model or task.llm_model or settings.LLM_MODEL
    verdicts = asyncio.run(info_stages._llm_screen(
        posts, system, model, llm.key_for_user(task.owner)))
    return model, verdicts


def _try_posts(task, *, limit, days, date_from, date_to, post_ids, only_with_event):
    qs = Post.objects.filter(task=task).exclude(text="")
    if post_ids:
        found = list(qs.filter(id__in=post_ids).select_related("event"))
        missing = [i for i in post_ids if i not in {p.id for p in found}]
        return found[:limit], missing
    if only_with_event:
        qs = qs.filter(event__isnull=False)
    qs = _dated(qs, days=days, date_from=date_from, date_to=date_to, on_event=False)
    return list(qs.select_related("event").order_by("-posted_at", "-id")[:limit]), []


def _retag_heads(task, *, limit, days, date_from, date_to, post_ids):
    """Пост, з якого подія взяла теги: найраніший із текстом. Явні id — як є."""
    if post_ids:
        return list(Post.objects.filter(task=task, id__in=post_ids)
                    .exclude(text="").select_related("event")[:limit])
    qs = (Post.objects.filter(task=task, event__isnull=False).exclude(text="")
          .select_related("event")
          .order_by("-event__event_date", "posted_at", "id"))
    qs = _dated(qs, days=days, date_from=date_from, date_to=date_to, on_event=True)
    seen, heads = set(), []
    for post in qs.iterator(chunk_size=200):
        if post.event_id in seen:
            continue
        seen.add(post.event_id)
        heads.append(post)
        if len(heads) >= limit:
            break
    return heads


def _retag_window(task, days, date_from, date_to) -> int:
    qs = Post.objects.filter(task=task, event__isnull=False).exclude(text="")
    qs = _dated(qs, days=days, date_from=date_from, date_to=date_to, on_event=True)
    return qs.order_by().values("event_id").distinct().count()


@tool("prompt_try", mutates=True, group="monitoring", params={
      "task": "Задача infospace: id, slug або частина назви.",
      "limit": f"Скільки постів прогнати (1..{TRY_MAX}). Кожен — один виклик LLM.",
      "posts": "Id постів через кому. Порожньо = свіжа вибірка самотужки.",
      "days": "Брати пости не старші за стільки діб. 0 = без обмеження дати. "
              "Ігнорується, якщо задано date_from або date_to.",
      "date_from": "Нижня межа дати поста, YYYY-MM-DD. Порожньо = за days.",
      "date_to": "Верхня межа дати поста, YYYY-MM-DD.",
      "only_with_event": "Лише пости, що вже стали подією (текст у них лишається). "
                         "false = будь-які done із текстом, включно з відсіяними.",
      "info_screen_prompt": "Чернетка системного промпта на цей виклик. Порожньо = "
                            "те, що в задачі. '-' = дефолт із коду. Не зберігається.",
      "info_tagger_prompt": "Чернетка правил тегів на цей виклик. Порожньо = те, що "
                            "в задачі. '-' = лише підказки категорій. Не зберігається.",
      })
def prompt_try(task: str, limit: int = 3, posts: str = "", days: int = 14,
               date_from: str = "", date_to: str = "",
               only_with_event: bool = True, info_screen_prompt: str = "",
               info_tagger_prompt: str = ""):
    """Прогнати скрін-промпт infospace на кількох уже зібраних постах.

    У БД нічого не пише і чернетку не зберігає: це перевірка правила тега перед
    `task_update`. Позначений як такий, що змінює стан, бо кожен пост — виклик
    LLM (модель скріну задачі), і режим лише-читання його блокує. Стелі: 8 постів.

    Пости без тексту прогнати не можна: ретеншн видаляє нерелевантні done старші
    за info_retention_days. Пости, що стали подіями, текст зберігають.

    Зберегти правило, яке сподобалось: task_update info_tagger_prompt=… Потім
    обмежено оновити теги подій: posts_retag. Повертати десятки тисяч постів
    зі стадії done у чергу скріну цей інструмент не вміє.
    """
    _check_limit(limit, TRY_MAX, "prompt_try")
    row = common.resolve_task(task)
    _require_infospace(row)
    view, notes = _draft(row, info_screen_prompt, info_tagger_prompt)
    from analysis.services.infospace.prompts import build_screen_prompt
    system = build_screen_prompt(view)
    ids = _parse_ids(posts)
    sample, missing = _try_posts(row, limit=limit, days=days, date_from=date_from,
                                 date_to=date_to, post_ids=ids,
                                 only_with_event=only_with_event)
    n_done, n_text, n_events = _stock(row)
    keys = set(row.tag_categories.values_list("key", flat=True))
    head = [
        f"#{row.id} {row.slug}",
        "; ".join(notes),
        f"зібраний промпт: {len(system)} симв",
        f"done={n_done}, із текстом={n_text}, подій із текстом={n_events}",
    ]
    if missing:
        head.append("немає поста або в нього порожній текст: "
                    + ", ".join(f"#{i}" for i in missing))
    if not sample:
        return fmt.joinsec(
            "\n".join(head),
            "Немає постів із текстом у цій вибірці — LLM не викликався. "
            "Звузь або розширь days, передай posts=id або only_with_event=false. "
            "Чернетку не збережено.")
    model, verdicts = _screen(row, sample, system)
    lines = []
    for post in sample:
        verdict, _empty = verdicts.get(post.id, (None, True))
        if not isinstance(verdict, dict):
            lines.append(f"#{post.id}: немає вердикту (порожня або бита відповідь)")
            continue
        rel = "relevant" if verdict.get("relevant") else "не релевантний"
        ev = f"подія #{post.event_id}" if post.event_id else "без події"
        lines.append(
            f"#{post.id} {ev} → {rel}\n"
            f"  на події зараз: {_event_tags(post.event, keys)}\n"
            f"  модель: {_raw_tags(verdict)}\n"
            f"  reason: {fmt.trunc(verdict.get('reason') or '', 160)}\n"
            f"  текст: {fmt.trunc(post.text, 140)}")
    tail = system[-500:].strip()
    return fmt.joinsec(
        "\n".join(head + [f"модель: {model}", f"викликів LLM: {len(sample)}",
                          "у БД не записано, чернетку не збережено"]),
        fmt.section("Вердикти", "\n\n".join(lines)),
        fmt.section("Хвіст зібраного промпта", tail))


@tool("posts_retag", mutates=True, group="monitoring", params={
      "task": "Задача infospace: id, slug або частина назви.",
      "limit": f"Скільки подій перетегувати (1..{RETAG_MAX}). Кожна — один виклик LLM.",
      "days": "Події не старші за стільки діб (за датою події). 0 = без обмеження. "
              "Ігнорується, якщо задано date_from або date_to.",
      "date_from": "Нижня межа дати події, YYYY-MM-DD. Порожньо = за days.",
      "date_to": "Верхня межа дати події, YYYY-MM-DD. Повторний виклик із тими "
                 "самими датами бере ті самі найсвіжіші події — щоб іти далі, звузь вікно.",
      "posts": "Id постів через кому — прогнати саме їх і оновити їхні події. "
               "Порожньо = найраніший пост із текстом у кожної події (як при створенні).",
      "confirm": "false = лише порахувати вибірку, LLM не кликати. "
                 "true = прогнати поточний збережений промпт і замінити теги.",
      })
def posts_retag(task: str, limit: int = 5, days: int = 14,
                date_from: str = "", date_to: str = "", posts: str = "",
                confirm: bool = False):
    """Перетегувати вже зібрані події infospace поточним збереженим промптом.

    Міняє лише теги категорій задачі на події і в classification поста.
    Подію не видаляє, стадію не скидає, опис і релевантність не чіпає.
    Теги інших категорій (наприклад населений пункт) лишаються.

    Спершу перевір правило через prompt_try, запиши його task_update, потім
    цей інструмент. confirm=false нічого не викликає. Стелі: 25 подій за виклик.
    Повний перепрогін усієї задачі (скинути пости в чергу й пересобрати події)
    тут немає: на десятках тисяч постів це окремі гроші й години, а ретеншн
    уже вирізав тексти нерелевантних.
    """
    _check_limit(limit, RETAG_MAX, "posts_retag")
    row = common.resolve_task(task)
    _require_infospace(row)
    ids = _parse_ids(posts)
    n_done, n_text, n_events = _stock(row)
    if ids:
        window = Post.objects.filter(task=row, id__in=ids).exclude(text="").count()
    else:
        window = _retag_window(row, days, date_from, date_to)
    take = min(limit, window)
    head = (f"#{row.id} {row.slug}: done={n_done}, із текстом={n_text}, "
            f"подій із текстом={n_events}; у вікні {window}, візьме {take}")
    if not confirm:
        return (head + "\n\nconfirm=false: LLM не викликався, теги не змінено. "
                "Щоб застосувати поточний збережений промпт, повтори з confirm=true. "
                "Чернетку сюди передати не можна — спершу task_update.")
    if take == 0:
        return head + "\n\nНічого перетегувати: у вибірці немає постів із текстом."
    from analysis.services.infospace.prompts import build_screen_prompt
    sample = _retag_heads(row, limit=limit, days=days, date_from=date_from,
                          date_to=date_to, post_ids=ids)
    if not sample:
        return head + "\n\nНічого перетегувати: у вибірці немає постів із текстом."
    system = build_screen_prompt(row)
    model, verdicts = _screen(row, sample, system)
    keys = set(row.tag_categories.values_list("key", flat=True))
    lines = []
    for post in sample:
        verdict, _empty = verdicts.get(post.id, (None, True))
        if not isinstance(verdict, dict):
            lines.append(f"#{post.id}: немає вердикту, теги не змінено")
            continue
        new_tags, dropped = _resolve_tags(row, verdict)
        before = _event_tags(post.event, keys)
        cls = dict(post.classification or {})
        grouped = {}
        for tag in new_tags:
            grouped.setdefault(tag.category, []).append(tag.name)
        cls["tags"] = grouped
        if verdict.get("reason"):
            cls["screen_reason"] = str(verdict["reason"])[:300]
        cls["_retag_model"] = model
        post.classification = cls
        post.save(update_fields=["classification"])
        if post.event_id:
            _replace_event_tags(post.event, keys, new_tags)
            post.event.refresh_from_db()
            after = _event_tags(post.event, keys)
            where = f"подія #{post.event_id}"
        else:
            after = "події немає"
            where = "без події"
        extra = ""
        if dropped:
            extra += "\n  відкинуто (немає в закритій категорії): " + ", ".join(dropped)
        if not verdict.get("relevant"):
            extra += "\n  модель сказала «не релевантний»; релевантність і подію не чіпав"
        lines.append(f"#{post.id} {where}\n  було: {before}\n  стало: {after}{extra}")
    return fmt.joinsec(
        head + f"\nмодель: {model}; викликів LLM: {len(sample)}; "
        "оновлено теги, події й стадії на місці",
        fmt.section("Перетеговано", "\n\n".join(lines)))


def _replace_event_tags(event, keys, new_tags):
    from analysis.services import tags as tag_service
    keep = [t for t in event.tags.all() if t.category not in keys]
    event.tags.set(tag_service.collapse_scales(keep + list(new_tags)))
