"""Пости задачі: перегляд і повернення конкретних постів у чергу конвеєра.

Повний скид задачі (`rescreen_task_now` / `reprocess_period` на весь період)
сюди не винесено. Тут лише вибірка, яку назвали id постів або id подій.
"""
from django.db import transaction
from django.db.models import Q

from analysis.models import AnalysisTask, Event, Post, PublishedEvent
from analysis.services.mcp_api import common, fmt
from analysis.services.mcp_api.prompts import _dated
from analysis.services.mcp_api.registry import ToolError, tool

LIST_MAX = 40
REQUEUE_MAX = 50
TEXT_CAP = 6000

# Стадії, у які пост цього конвеєра можна повернути. Чужий префікс не ставимо:
# events-воркер забрав би infospace-пост зі стадії collected.
_STAGES = {
    AnalysisTask.PIPELINE_EVENTS: ("collected", "enriched", "preclustered", "classified"),
    AnalysisTask.PIPELINE_MONITOR: ("mon_collected", "mon_filtered", "mon_prescreened"),
    AnalysisTask.PIPELINE_RESEARCH: ("mon_collected", "mon_filtered"),
    AnalysisTask.PIPELINE_INFOSPACE: ("info_collected", "info_screened"),
    AnalysisTask.PIPELINE_TGSEARCH: ("tgs_collected", "tgs_screened", "tgs_tagged"),
}
_DEFAULT_STAGE = {
    AnalysisTask.PIPELINE_EVENTS: "collected",
    AnalysisTask.PIPELINE_MONITOR: "mon_filtered",
    AnalysisTask.PIPELINE_RESEARCH: "mon_collected",
    AnalysisTask.PIPELINE_INFOSPACE: "info_collected",
    AnalysisTask.PIPELINE_TGSEARCH: "tgs_collected",
}
_CLS_KEYS = ("summary", "signature", "screen_reason", "reason", "region", "tags",
             "_screen_model", "_retag_model")


def _ids(spec: str, name: str) -> list[int]:
    if not (spec or "").strip():
        return []
    out = []
    for part in spec.replace(";", ",").split(","):
        part = part.strip().lstrip("#")
        if not part:
            continue
        if not part.isdigit():
            raise ToolError(f"{name}: очікується id, отримано «{part}»")
        out.append(int(part))
    return out


def _rel(value) -> str:
    if value is True:
        return "так"
    if value is False:
        return "ні"
    return "—"


def _note(post) -> str:
    if post.stage_error:
        return post.stage_error
    cls = post.classification or {}
    return cls.get("screen_reason") or cls.get("reason") or ""


@tool("posts_list", group="monitoring", params={
      "task": "Задача: id, slug або частина назви. Порожньо лише якщо задано event або posts.",
      "event": "Id подій через кому — пости цих подій.",
      "posts": "Id постів через кому.",
      "stage": "Стадія конвеєра (collected, info_collected, done, failed, …). Порожньо = усі.",
      "days": "Пости не старші за стільки діб. 0 = без обмеження. Ігнорується, якщо задано id постів/подій і немає дат.",
      "date_from": "Дата публікації від, YYYY-MM-DD.",
      "date_to": "Дата публікації до, YYYY-MM-DD.",
      "relevant": "yes | no | unknown. Порожньо = усі.",
      "has_event": "yes — лише вже на події; no — без події. Порожньо = усі.",
      "query": "Текст поста містить цей рядок.",
      "limit": f"Скільки рядків (1..{LIST_MAX}).",
      })
def posts_list(task: str = "", event: str = "", posts: str = "", stage: str = "",
               days: int = 14, date_from: str = "", date_to: str = "",
               relevant: str = "", has_event: str = "", query: str = "",
               limit: int = 20):
    """Список зібраних постів: id, стадія, релевантність, подія, уривок тексту.

    Без task, event або posts інструмент не стартує — інакше це сканування
    всієї таблиці. Повний текст і класифікація — `post_show`.
    """
    if limit < 1 or limit > LIST_MAX:
        raise ToolError(f"posts_list: limit має бути 1..{LIST_MAX}")
    post_ids, event_ids = _ids(posts, "posts"), _ids(event, "event")
    if not task and not post_ids and not event_ids:
        raise ToolError("вкажи task, event або posts — без цього таблиця постів завелика")
    qs = common.scope_by_task(Post.objects.all())
    desc = []
    if task:
        row = common.resolve_task(task)
        qs = qs.filter(task=row)
        desc.append(row.slug)
    if post_ids:
        qs = qs.filter(id__in=post_ids)
        desc.append(f"id {len(post_ids)}")
    if event_ids:
        qs = qs.filter(event_id__in=event_ids)
        desc.append("події " + ",".join(f"#{i}" for i in event_ids))
    if stage:
        known = {s for _, stages in _STAGES.items() for s in stages}
        known |= {Post.STAGE_DONE, Post.STAGE_FAILED}
        if stage not in known:
            raise ToolError(f"невідома стадія «{stage}»")
        qs = qs.filter(stage=stage)
        desc.append(stage)
    if not (post_ids or event_ids) or date_from or date_to:
        qs = _dated(qs, days=days, date_from=date_from, date_to=date_to, on_event=False)
        if date_from or date_to:
            desc.append(f"{date_from or '…'}…{date_to or '…'}")
        elif days:
            desc.append(f"{days} дн")
    rel = (relevant or "").strip().lower()
    if rel in ("yes", "true", "1"):
        qs, desc = qs.filter(is_relevant=True), desc + ["релевантні"]
    elif rel in ("no", "false", "0"):
        qs, desc = qs.filter(is_relevant=False), desc + ["нерелевантні"]
    elif rel in ("unknown", "null"):
        qs, desc = qs.filter(is_relevant__isnull=True), desc + ["без вердикту"]
    elif rel:
        raise ToolError("relevant: yes | no | unknown")
    link = (has_event or "").strip().lower()
    if link in ("yes", "true", "1"):
        qs, desc = qs.filter(event__isnull=False), desc + ["з подією"]
    elif link in ("no", "false", "0"):
        qs, desc = qs.filter(event__isnull=True), desc + ["без події"]
    elif link:
        raise ToolError("has_event: yes | no")
    if query:
        qs = qs.filter(text__icontains=query.strip())
        desc.append(f"текст ~{query.strip()}")
    total = qs.count()
    rows = []
    for p in qs.order_by("-posted_at", "-id")[:limit]:
        when = str(p.posted_at)[:16] if p.posted_at else "—"
        rows.append([
            f"#{p.id}", p.stage, _rel(p.is_relevant),
            f"#{p.event_id}" if p.event_id else "—", when,
            fmt.trunc(p.channel_name or "", 18),
            fmt.trunc(_note(p), 40),
            fmt.trunc(p.text, 80),
        ])
    return fmt.joinsec(
        fmt.section(f"Пости: {total} (показано {len(rows)})", "; ".join(desc)),
        fmt.table(["id", "стадія", "рел", "подія", "коли", "канал", "нотатка", "текст"],
                  rows) if rows else "нічого не знайдено",
        "Повний текст: post_show. Повернути в чергу: posts_requeue.")


@tool("post_show", group="monitoring", params={
      "ref": "Id поста (з posts_list або з колонки пост у events_list).",
      })
def post_show(ref: str):
    """Картка поста: текст, стадія, помилка, класифікація, теги, подія, посилання."""
    post = _resolve_post(ref)
    cls = post.classification or {}
    picked = [(k, cls.get(k)) for k in _CLS_KEYS if cls.get(k) not in (None, "", {}, [])]
    prescreen = cls.get("_prescreen")
    if isinstance(prescreen, dict) and "could_be_criticism" in prescreen:
        picked.append(("prescreen", prescreen.get("could_be_criticism")))
    other = [k for k in cls if k not in _CLS_KEYS and k != "_prescreen"]
    text = post.text or ""
    body = text if len(text) <= TEXT_CAP else text[:TEXT_CAP] + f"\n… обрізано, усього {len(text)} симв"
    tags = ", ".join(f"{t.category}:{t.name}" for t in post.tags.all()) or "—"
    return fmt.joinsec(
        fmt.section(f"Пост #{post.id}", fmt.kv([
            ("задача", f"#{post.task_id} {post.task.slug}"),
            ("стадія", post.stage + (f", спроб {post.stage_attempts}" if post.stage_attempts else "")),
            ("помилка", fmt.trunc(post.stage_error, 400)),
            ("релевантний", _rel(post.is_relevant)),
            ("подія", f"#{post.event_id}" if post.event_id else "—"),
            ("коли", str(post.posted_at)[:19] if post.posted_at else "—"),
            ("канал", post.channel_name or (post.channel.title if post.channel_id else "")),
            ("посилання", post.url),
            ("заголовок", post.title),
            ("теги", tags),
            ("класифікація", "; ".join(f"{k}={fmt.trunc(v, 180)}" for k, v in picked) or "—"),
            ("інші ключі classification", ", ".join(other)),
        ])),
        fmt.section("Текст", body or "(порожньо)"))


def _resolve_post(ref):
    if not str(ref).strip().lstrip("#").isdigit():
        raise ToolError("пост — за числовим id (з posts_list)")
    post = (common.scope_by_task(Post.objects.select_related("task", "channel"))
            .filter(pk=common.as_int(ref, "ref")).first())
    if post is None:
        raise ToolError(f"поста #{ref} немає")
    return post


def _target_stage(task, stage: str) -> str:
    allowed = _STAGES.get(task.pipeline)
    if not allowed:
        raise ToolError(f"конвеєр {task.pipeline} не має черги постів, яку можна перезапустити")
    target = (stage or "").strip() or _DEFAULT_STAGE[task.pipeline]
    if target not in allowed:
        raise ToolError(
            f"стадія «{target}» не для конвеєра {task.pipeline}. "
            f"Можна: {', '.join(allowed)}. Дефолт — {_DEFAULT_STAGE[task.pipeline]}.")
    return target


def _selection(task, post_ids, event_ids):
    q = Q()
    if post_ids:
        q |= Q(id__in=post_ids)
    if event_ids:
        q |= Q(event_id__in=event_ids)
    found = list(common.scope_by_task(Post.objects.filter(task=task)).filter(q)
                 .select_related("event"))
    missing = [i for i in post_ids if i not in {p.id for p in found}]
    return found, missing


@tool("posts_requeue", mutates=True, group="monitoring", params={
      "task": "Задача, чиї пости повертаємо. id, slug або частина назви.",
      "posts": "Id постів через кому.",
      "events": "Id подій через кому — усі їхні пости. Разом із posts це обʼєднання.",
      "stage": "Куди повернути. Порожньо = початок обробки цього конвеєра "
               "(infospace: info_collected, events: collected, monitor: mon_filtered).",
      "drop_events": "Пости, що вже на події, без цього не чіпаються: скидання стадії "
                     "народило б другі події. true = відчепити і видалити подію, "
                     "якщо постів на ній не лишилось. Опубліковану подію не видаляє.",
      "confirm": "false = лише показати, що буде. true = повернути в чергу.",
      "limit": f"Стеля вибірки (1..{REQUEUE_MAX}). Більше за стелю — відмова, не обрізання.",
      })
def posts_requeue(task: str, posts: str = "", events: str = "", stage: str = "",
                  drop_events: bool = False, confirm: bool = False, limit: int = 20):
    """Повернути названі пости в чергу їхнього конвеєра.

    Потрібен posts або events. Усю задачу одним викликом не скинути.
    Пости, вже привʼязані до події, без drop_events=true лишаються на місці:
    інакше скрін створив би другу подію. Для infospace оновити лише теги,
    не чіпаючи подію, — `posts_retag`. confirm=false нічого не пише.
    """
    if limit < 1 or limit > REQUEUE_MAX:
        raise ToolError(f"posts_requeue: limit має бути 1..{REQUEUE_MAX}")
    row = common.resolve_task(task)
    target = _target_stage(row, stage)
    post_ids, event_ids = _ids(posts, "posts"), _ids(events, "events")
    if not post_ids and not event_ids:
        raise ToolError("вкажи posts або events. Усю задачу posts_requeue не скидає.")
    found, missing = _selection(row, post_ids, event_ids)
    linked = [p for p in found if p.event_id]
    head = (f"#{row.id} {row.slug}: знайдено {len(found)}, з них на подіях {len(linked)}. "
            f"Цільова стадія: {target}.")
    if missing:
        head += " Немає в цій задачі: " + ", ".join(f"#{i}" for i in missing) + "."
    if len(found) > limit:
        raise ToolError(
            head + f" У вибірці {len(found)} постів, limit={limit}. "
            "Звузь список або підніми limit. Частково чергу не ріжу, "
            "щоб не розірвати подію навпіл.")
    if not found:
        return head + "\n\nНічого повертати."
    if linked and not drop_events:
        ids = ", ".join(f"#{p.event_id}" for p in linked[:12])
        extra = ""
        if row.pipeline == AnalysisTask.PIPELINE_INFOSPACE:
            extra = " Теги без скиду події — posts_retag."
        return (head + f"\n\n{len(linked)} постів уже на подіях ({ids}). "
                "Без drop_events=true їх не чіпаю: повернення в чергу створило б "
                "другі події." + extra + " Щоб відчепити і перепрогнати: "
                "confirm=true drop_events=true. Зараз нічого не змінено.")
    if not confirm:
        return (head + "\n\nconfirm=false: у чергу не ставив. "
                + ("Події, що спорожніють, будуть видалені (опубліковані — ні). "
                   if drop_events else "")
                + "Повтори з confirm=true.")
    with transaction.atomic():
        event_ids_touched = {p.event_id for p in found if p.event_id}
        Post.objects.filter(id__in=[p.id for p in found]).update(
            stage=target, stage_locked_at=None, stage_attempts=0, stage_error="",
            event=None, dedup_group=None, is_classified=False, is_relevant=None)
        removed, kept_published, refreshed = [], [], []
        for ev in Event.objects.filter(id__in=event_ids_touched):
            if Post.objects.filter(event=ev).exists():
                _refresh_event(ev)
                refreshed.append(ev.id)
                continue
            if PublishedEvent.objects.filter(
                    event=ev, status=PublishedEvent.STATUS_PUBLISHED).exists():
                ev.post_count = 0
                ev.channel_count = 0
                ev.reach = 0
                ev.save(update_fields=["post_count", "channel_count", "reach"])
                kept_published.append(ev.id)
                continue
            removed.append(ev.id)
            ev.delete()
    bits = [f"у чергу {target}: {len(found)}"]
    if removed:
        bits.append("видалено порожні події " + ", ".join(f"#{i}" for i in removed))
    if refreshed:
        bits.append("перераховано " + ", ".join(f"#{i}" for i in refreshed))
    if kept_published:
        bits.append("не видалено (вже публікувались) "
                    + ", ".join(f"#{i}" for i in kept_published))
    return head + "\n" + "; ".join(bits)


def _refresh_event(ev):
    members = list(Post.objects.filter(event=ev).select_related("channel"))
    chans = {p.channel_id: (p.channel.subscribers or 0) for p in members if p.channel_id}
    ev.post_count = len(members)
    ev.channel_count = len(chans)
    ev.reach = sum(chans.values())
    ev.save(update_fields=["post_count", "channel_count", "reach"])
