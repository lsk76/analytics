"""Публікація подій у Telegram-чат: профілі (`PublishConfig`) і журнал
опублікованого (`PublishedEvent`).

Профіль — власна річ користувача (owner, як в адмінці): чужий не видно й не
правиться. Bot token у відповідь ніколи не потрапляє (лише «заданий/ні»).
Сам постинг робить воркер стадії `publish` — тут лише конфіг і журнал.
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from analysis.models import Event, PublishConfig, PublishedEvent, Region, Tag
from analysis.services.mcp_api import common, fmt, registry
from analysis.services.mcp_api.registry import SCOPE_CREATE, ToolError, tool

STATUS_ICON = {"published": "✓", "skipped": "⊘", "failed": "✗", "pending": "…"}


def _configs():
    qs = PublishConfig.objects.select_related("task", "owner", "forward_account")
    who = registry.actor()
    return qs if who.is_superuser else qs.filter(owner=who.user)


def resolve_config(ref):
    """PublishConfig за id / частиною назви — лише свій."""
    ref = str(ref).strip()
    if ref.lstrip("#").isdigit():
        c = _configs().filter(pk=int(ref.lstrip("#"))).first()
        if not c:
            raise ToolError(f"профілю публікації #{ref} немає")
        return c
    return common._pick(_configs().filter(name__icontains=ref).order_by("id"), ref,
                        "профіль публікації", lambda c: f"#{c.id} {c.name}")


def _tags(spec: str) -> list[Tag]:
    """`кат:тег, кат:тег` → теги (лише наявні; без категорії — якщо однозначно)."""
    out = []
    for item in common_split(spec):
        cat, _, name = item.rpartition(":")
        qs = Tag.objects.filter(name__iexact=name.strip())
        if cat:
            qs = qs.filter(category=cat.strip())
        tags = list(qs[:5])
        if not tags:
            raise ToolError(f"тега «{item}» немає (список: tag_categories)")
        if len(tags) > 1:
            raise ToolError(f"«{item}» неоднозначний: " + ", ".join(f"{t.category}:{t.name}" for t in tags))
        out.append(tags[0])
    return out


def common_split(spec):
    return [x.strip() for x in (spec or "").split(",") if x.strip()]


def _set_m2m(cfg, field, spec, resolver, notes):
    """Списки відбору: '' = не чіпати, '-' = очистити, інакше — повна заміна."""
    if not spec:
        return
    if spec.strip() == "-":
        getattr(cfg, field).clear()
        notes.append(f"{field}: очищено")
        return
    objs = resolver(spec)
    getattr(cfg, field).set(objs)
    notes.append(f"{field}: " + ", ".join(str(getattr(o, 'name', o)) for o in objs))


def _regions(spec):
    return [common.resolve_region(x) for x in common_split(spec)]


def _tg_link(cfg, p) -> str:
    """Посилання на пост у каналі: приватний/публічний за chat_id."""
    if not p.tg_message_id:
        return ""
    chat = str(cfg.chat_id or "").strip()
    if chat.startswith("-100"):
        return f"https://t.me/c/{chat[4:]}/{p.tg_message_id}"
    if chat.startswith("@"):
        return f"https://t.me/{chat[1:]}/{p.tg_message_id}"
    return f"msg {p.tg_message_id}"


def _card(cfg) -> str:
    pub = cfg.published.order_by()
    return fmt.kv([
        ("активний", fmt.flag(cfg.is_active)),
        ("власник", cfg.owner.username if cfg.owner_id else "—"),
        ("дослідження", cfg.task.slug if cfg.task_id else "усі"),
        ("канал", f"{cfg.chat_id or '—'}; bot token: {'заданий' if cfg.bot_token else 'з env'}"),
        ("режим", ("сирий (оригінал + теги)" if cfg.raw_mode else "AI фільтр+рерайт")
                  + (f", постить акаунт #{cfg.forward_account_id}" if cfg.post_as_account else "")),
        ("відбір", f"аудит={cfg.review_status}, від {cfg.publish_from or '—'}, "
                   f"не старше {cfg.max_age_days or '∞'} дн, до {cfg.max_per_pass}/прохід"),
        ("теги (АБО)", ", ".join(f"{t.category}:{t.name}" for t in cfg.tags.all()) or "—"),
        ("обовʼязкові (І)", ", ".join(f"{t.category}:{t.name}" for t in cfg.require_tags.all()) or "—"),
        ("виключення", ", ".join(f"{t.category}:{t.name}" for t in cfg.exclude_tags.all()) or "—"),
        ("регіони", ", ".join(r.name for r in cfg.regions.all()) or "усі"),
        ("AI", f"{cfg.ai_model or 'дефолт'}; промпт {len(cfg.ai_prompt or '')} симв"),
        ("опубліковано/відсіяно/збій",
         f"{pub.filter(status='published').count()}/{pub.filter(status='skipped').count()}"
         f"/{pub.filter(status='failed').count()}"),
        ("адмінка", f"/admin/analysis/publishconfig/{cfg.id}/change/"),
    ])


CONFIG_DOCS = {
    "name": "Назва профілю.",
    "task": "Дослідження (id/slug/назва — своє). '-' = будь-яке (усі свої події).",
    "chat_id": "Chat ID каналу Telegram: -100… або @username.",
    "bot_token": "Bot token (порожньо = глобальний з env). У відповідях не показується.",
    "is_active": "Активний профіль (воркер публікує лише активні).",
    "review_status": "Публікувати події зі статусом: approved (дефолт) | pending | rejected.",
    "tags": "Теги-відбір (АБО): `кат:тег, кат:тег`. '-' = без обмеження.",
    "require_tags": "Обовʼязкові теги (І): `кат:тег, …`. '-' = очистити.",
    "exclude_tags": "Теги-виключення: подія з будь-яким із них не публікується. '-' = очистити.",
    "regions": "Субʼєкти РФ через кому (канонічна назва/аліас). '-' = усі.",
    "max_age_days": "Не публікувати старше N діб. 0 = без обмеження; -1 = не змінювати.",
    "publish_from": "Публікувати події від дати YYYY-MM-DD. '-' = зняти.",
    "raw_mode": "true — без ШІ: оригінальний текст + теги; AI-промпт тоді лише фільтр.",
    "ai_model": "AI-модель (порожньо = дефолт). '-' = скинути.",
    "ai_prompt": "AI-промпт (фільтр + рерайт). '-' = очистити (у raw_mode = LLM не викликається).",
    "max_per_pass": "Макс. постів за прохід воркера. 0 = не змінювати.",
    "post_as_account": "true — постити Telegram-акаунтом (з медіа), потрібен forward_account.",
    "forward_account": "Акаунт публікації (id/номер/назва — видимий тобі). '-' = зняти.",
}


def _apply(cfg, notes, *, task="", chat_id="", bot_token="", is_active=None, review_status="",
           tags="", require_tags="", exclude_tags="", regions="", max_age_days=-1,
           publish_from="", raw_mode=None, ai_model="", ai_prompt="", max_per_pass=0,
           post_as_account=None, forward_account="", name=""):
    if name:
        cfg.name = name.strip()[:120]
        notes.append(f"назва={cfg.name}")
    if task:
        cfg.task = None if task.strip() == "-" else common.resolve_task(task)
        notes.append(f"дослідження={cfg.task.slug if cfg.task_id else 'усі'}")
    if chat_id:
        cfg.chat_id = chat_id.strip()[:64]
        notes.append(f"канал={cfg.chat_id}")
    if bot_token:
        cfg.bot_token = "" if bot_token.strip() == "-" else bot_token.strip()
        notes.append("bot token " + ("скинуто на env" if not cfg.bot_token else "оновлено"))
    if is_active is not None:
        cfg.is_active = bool(is_active)
        notes.append(f"активний={fmt.flag(cfg.is_active)}")
    if review_status:
        if review_status not in dict(Event.REVIEW_CHOICES):
            raise ToolError("review_status: approved | pending | rejected")
        cfg.review_status = review_status
        notes.append(f"аудит={review_status}")
    if max_age_days >= 0:
        cfg.max_age_days = int(max_age_days)
        notes.append(f"не старше={cfg.max_age_days or '∞'} дн")
    if publish_from:
        cfg.publish_from = None if publish_from.strip() == "-" else \
            common.parse_date(publish_from, "publish_from")
        notes.append(f"від={cfg.publish_from or '—'}")
    if raw_mode is not None:
        cfg.raw_mode = bool(raw_mode)
        notes.append(f"сирий режим={fmt.flag(cfg.raw_mode)}")
    if ai_model:
        cfg.ai_model = "" if ai_model.strip() == "-" else ai_model.strip()[:120]
        notes.append(f"AI-модель={cfg.ai_model or 'дефолт'}")
    if ai_prompt:
        cfg.ai_prompt = "" if ai_prompt.strip() == "-" else ai_prompt
        notes.append(f"AI-промпт {len(cfg.ai_prompt)} симв")
    if max_per_pass:
        cfg.max_per_pass = max(1, int(max_per_pass))
        notes.append(f"за прохід={cfg.max_per_pass}")
    if post_as_account is not None:
        cfg.post_as_account = bool(post_as_account)
        notes.append(f"постити акаунтом={fmt.flag(cfg.post_as_account)}")
    if forward_account:
        cfg.forward_account = None if forward_account.strip() == "-" else \
            common.resolve_account(forward_account)
        notes.append(f"акаунт публікації={'#' + str(cfg.forward_account_id) if cfg.forward_account_id else '—'}")
    if cfg.post_as_account and cfg.forward_account_id is None:
        raise ToolError("post_as_account=true потребує forward_account")
    cfg.save()
    _set_m2m(cfg, "tags", tags, _tags, notes)
    _set_m2m(cfg, "require_tags", require_tags, _tags, notes)
    _set_m2m(cfg, "exclude_tags", exclude_tags, _tags, notes)
    _set_m2m(cfg, "regions", regions, _regions, notes)


@tool("publish_config_show", group="service", params={"ref": "Профіль: id або частина назви."})
def publish_config_show(ref: str):
    """Картка профілю публікації: канал, режим, відбір (теги/регіони/аудит), лічильники."""
    cfg = resolve_config(ref)
    return fmt.section(f"Профіль публікації #{cfg.id} {cfg.name}", _card(cfg))


@tool("publish_config_create", group="service", mutates=True, scope=SCOPE_CREATE,
      params=CONFIG_DOCS)
def publish_config_create(name: str, chat_id: str, task: str = "", bot_token: str = "",
                          is_active: bool = False, review_status: str = "", tags: str = "",
                          require_tags: str = "", exclude_tags: str = "", regions: str = "",
                          max_age_days: int = -1, publish_from: str = "", raw_mode: bool = None,
                          ai_model: str = "", ai_prompt: str = "", max_per_pass: int = 0,
                          post_as_account: bool = None, forward_account: str = ""):
    """Створити профіль публікації подій у Telegram-канал (власник = ти).

    За замовчуванням НЕАКТИВНИЙ — спершу перевір відбір `events_list` із тими
    самими тегами/регіоном, потім `publish_config_update(is_active=true)`.
    Активний профіль воркер `publish` бере одразу і публікує реальні пости.
    """
    if not (name or "").strip() or not (chat_id or "").strip():
        raise ToolError("потрібні name і chat_id")
    who = registry.actor()
    cfg = PublishConfig(name=name.strip()[:120], chat_id=chat_id.strip()[:64], owner=who.user)
    notes = []
    _apply(cfg, notes, task=task, bot_token=bot_token, is_active=is_active,
           review_status=review_status, tags=tags, require_tags=require_tags,
           exclude_tags=exclude_tags, regions=regions, max_age_days=max_age_days,
           publish_from=publish_from, raw_mode=raw_mode, ai_model=ai_model, ai_prompt=ai_prompt,
           max_per_pass=max_per_pass, post_as_account=post_as_account,
           forward_account=forward_account)
    return fmt.joinsec(
        fmt.section(f"Профіль публікації #{cfg.id} {cfg.name} створено", _card(cfg)),
        "" if cfg.is_active else
        f"Профіль вимкнений. Увімкнути: publish_config_update ref={cfg.id} is_active=true")


@tool("publish_config_update", group="service", mutates=True,
      params={"ref": "Профіль: id або частина назви (свій).", **CONFIG_DOCS})
def publish_config_update(ref: str, name: str = "", task: str = "", chat_id: str = "",
                          bot_token: str = "", is_active: bool = None, review_status: str = "",
                          tags: str = "", require_tags: str = "", exclude_tags: str = "",
                          regions: str = "", max_age_days: int = -1, publish_from: str = "",
                          raw_mode: bool = None, ai_model: str = "", ai_prompt: str = "",
                          max_per_pass: int = 0, post_as_account: bool = None,
                          forward_account: str = ""):
    """Змінити профіль публікації: увімкнути/вимкнути, канал, відбір, режим, AI.

    Списки (tags, require_tags, exclude_tags, regions) замінюються ПОВНІСТЮ
    переданим значенням; '-' очищає; не передано = без змін. Не передані
    скаляри теж не змінюються.
    """
    cfg = resolve_config(ref)
    notes = []
    _apply(cfg, notes, name=name, task=task, chat_id=chat_id, bot_token=bot_token,
           is_active=is_active, review_status=review_status, tags=tags,
           require_tags=require_tags, exclude_tags=exclude_tags, regions=regions,
           max_age_days=max_age_days, publish_from=publish_from, raw_mode=raw_mode,
           ai_model=ai_model, ai_prompt=ai_prompt, max_per_pass=max_per_pass,
           post_as_account=post_as_account, forward_account=forward_account)
    if not notes:
        return f"профіль #{cfg.id}: нічого не змінено (жоден параметр не передано)"
    return fmt.section(f"Профіль публікації #{cfg.id} {cfg.name}",
                       "\n".join(notes) + "\n\n" + _card(cfg))


@tool("published_list", group="service", params={
      "config": "Профіль: id або частина назви. Порожньо = усі свої.",
      "task": "Дослідження (id/slug/назва). Порожньо = усі.",
      "status": "published (дефолт) | skipped (відсіяно AI) | failed | pending | all.",
      "days": "За останні N днів (за часом публікації/створення запису).",
      "query": "Текст у пості або описі події.",
      "limit": "Скільки записів показати."})
def published_list(config: str = "", task: str = "", status: str = "published", days: int = 14,
                   query: str = "", limit: int = 30):
    """Що було опубліковано (журнал `PublishedEvent`): коли, яким профілем, яка подія,
    посилання на пост у каналі; для відсіяних/збійних — причина.

    Повний текст поста — `published_show`. Перечергувати подію (щоб воркер
    обробив наново) можна лише в адмінці (дія «Перечергувати»).
    """
    qs = PublishedEvent.objects.filter(config__in=_configs()) \
        .select_related("config", "event", "event__task")
    desc = []
    if config:
        c = resolve_config(config)
        qs, desc = qs.filter(config=c), desc + [f"профіль {c.name}"]
    if task:
        t = common.resolve_task(task)
        qs, desc = qs.filter(event__task=t), desc + [f"дослідження {t.slug}"]
    if status and status != "all":
        if status not in dict(PublishedEvent.STATUS_CHOICES):
            raise ToolError("status: published | skipped | failed | pending | all")
        qs, desc = qs.filter(status=status), desc + [f"статус {status}"]
    if days:
        since = timezone.now() - timedelta(days=max(1, int(days)))
        qs = qs.filter(Q(published_at__gte=since) | Q(published_at__isnull=True, created_at__gte=since))
        desc.append(f"за {days} дн")
    if query:
        qs = qs.filter(Q(post_text__icontains=query) | Q(event__summary__icontains=query))
        desc.append(f"текст ~{query}")
    total = qs.count()
    rows = [[f"#{p.id}", fmt.ago(p.published_at or p.created_at), fmt.trunc(p.config.name, 16),
             STATUS_ICON.get(p.status, p.status), f"#{p.event_id}",
             fmt.trunc(p.post_text or (p.event.summary if p.event_id else ""), 70),
             _tg_link(p.config, p) or fmt.trunc(p.ai_reason or p.error, 40)]
            for p in qs.order_by("-created_at")[:limit]]
    return fmt.joinsec(
        fmt.section(f"Публікації: {total} (показано {len(rows)})", "; ".join(desc) or "усі"),
        fmt.table(["id", "коли", "профіль", "ст.", "подія", "текст", "пост / причина"], rows)
        if rows else "нічого не знайдено",
        "Статуси: ✓ опубліковано, ⊘ відсіяно AI, ✗ збій, … в обробці. Деталі: published_show ref=<id>.")


@tool("published_show", group="service", params={"ref": "id запису публікації (з published_list)."})
def published_show(ref: str):
    """Одна публікація повністю: текст поста, вердикт і причина AI, посилання, помилка."""
    p = PublishedEvent.objects.filter(config__in=_configs(), pk=common.as_int(ref, "ref")) \
        .select_related("config", "event").first()
    if not p:
        raise ToolError(f"публікації #{ref} немає")
    return fmt.section(f"Публікація #{p.id} · {p.config.name} · {p.status}", fmt.kv([
        ("подія", f"#{p.event_id} {fmt.trunc(p.event.summary, 200) if p.event_id else '—'}"),
        ("опубліковано", f"{p.published_at or '—'}; {_tg_link(p.config, p) or '—'}"),
        ("AI", f"{'публікувати' if p.ai_verdict else ('ні' if p.ai_verdict is False else '—')}"
               + (f": {p.ai_reason}" if p.ai_reason else "")),
        ("спроб", p.attempts), ("помилка", p.error or "—"),
        ("текст поста", p.post_text or "—"),
        ("адмінка", f"/admin/analysis/publishedevent/{p.id}/change/"),
    ]))
