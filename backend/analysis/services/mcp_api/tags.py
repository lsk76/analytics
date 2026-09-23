"""Довідник тегів: категорії (`TagCategory`) і канонічні теги (`Tag`).

Спільний для всіх задач. Нова категорія має з'явитися рядком тут — інакше
фасет подій `?tag_<ключ>` не реєструється. `closed` зупиняє пайплайн від
вигадування нових тегів; сід-список закритої категорії якраз і наповнюється
цими інструментами.
"""
from django.core.exceptions import ValidationError
from django.core.validators import validate_slug
from django.db import transaction
from django.db.models import Count

from analysis.models import ResearchRubric, Tag, TagCategory
from analysis.services import access
from analysis.services.mcp_api import fmt
from analysis.services.mcp_api.registry import SCOPE_CREATE, ToolError, actor, tool


def _key(raw: str) -> str:
    key = (raw or "").strip().lower()
    if not key:
        raise ToolError("ключ порожній")
    if len(key) > 32:
        raise ToolError("ключ довший за 32 символи")
    try:
        validate_slug(key)
    except ValidationError:
        raise ToolError("ключ — латиниця, цифри, _ і - (slug)")
    return key


def _category(ref: str) -> TagCategory:
    key = (ref or "").strip()
    c = TagCategory.objects.filter(key__iexact=key).first()
    if not c:
        raise ToolError(f"категорії «{key}» немає (список: tag_categories)")
    return c


def _match_name(category: str, name: str, exclude_pk: int = None):
    """Той самий тег у категорії без огляду на регістр.

    `__iexact` у Postgres з C-локаллю (і в SQLite) не зводить українські літери,
    тож «Важливість» і «важливість» інакше стали б двома рядками.
    """
    folded = (name or "").strip().casefold()
    qs = Tag.objects.filter(category=category)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    for t in qs.iterator():
        if t.name.casefold() == folded:
            return t
    return None


def _resolve_tag(ref: str) -> Tag:
    ref = (ref or "").strip().lstrip("#")
    if not ref:
        raise ToolError("тег не вказано")
    if ref.isdigit():
        t = Tag.objects.filter(pk=int(ref)).first()
        if not t:
            raise ToolError(f"тега #{ref} немає")
        return t
    if ":" in ref:
        cat, _, name = ref.partition(":")
        c = _category(cat)
        t = _match_name(c.key, name)
        if not t:
            raise ToolError(f"тега «{c.key}:{name.strip()}» немає")
        return t
    folded = ref.casefold()
    matches = [t for t in Tag.objects.iterator() if t.name.casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError(f"тегів «{ref}» кілька — вкажи `категорія:назва` або id")
    raise ToolError(f"тега «{ref}» немає")


def _save(obj):
    try:
        obj.full_clean()
    except ValidationError as e:
        parts = []
        if hasattr(e, "message_dict"):
            for k, msgs in e.message_dict.items():
                parts.append(f"{k}: {', '.join(msgs)}")
        else:
            parts.extend(e.messages)
        raise ToolError("; ".join(parts) or "невалідне значення")
    obj.save()


def _clip(value: str, limit: int, what: str) -> str:
    value = (value or "").strip()
    if len(value) > limit:
        raise ToolError(f"{what} довше за {limit} символів")
    return value


def _links(tag: Tag):
    return (tag.events.count(), tag.posts.count(),
            tag.publish_configs.count() + tag.required_publish_configs.count()
            + tag.excluded_from_publish_configs.count(),
            tag.aliases.count())


def _tag_line(tag: Tag, links=None) -> str:
    n_ev, n_posts, n_pub, n_al = links or _links(tag)
    return fmt.kv([
        ("id", f"#{tag.id}"),
        ("категорія", tag.category),
        ("назва", tag.name),
        ("подій", n_ev),
        ("постів", n_posts),
        ("профілів публікації", n_pub or "—"),
        ("аліасів", n_al or "—"),
    ])


def _category_card(c: TagCategory) -> str:
    who = actor()
    tasks = access.visible(c.tasks.all(), who.user, unrestricted=who.unrestricted)
    slugs = list(tasks.order_by("slug").values_list("slug", flat=True))
    n_tags = Tag.objects.filter(category=c.key).count()
    return fmt.kv([
        ("ключ", c.key),
        ("назва", c.label),
        ("тип", "закрита" if c.closed else "відкрита"),
        ("підказка", c.hint or "—"),
        ("порядок", c.order),
        ("тегів", n_tags),
        ("задачі", ", ".join(slugs) or "—"),
    ])


@tool("tag_category_show", group="monitoring", params={
      "key": "Ключ категорії (латиниця, як у tag_categories)."})
def tag_category_show(key: str):
    """Картка категорії. «Підказка» — одне речення в схемі тегів, не текст, який іде в LLM.

    Повний промпт моделі — розділ «Промпт, який іде в LLM» у task_show задачі,
    яка цю категорію використовує.
    """
    c = _category(key)
    return fmt.section(
        f"Категорія {c.key}",
        _category_card(c) + "\n\nПідказка — не промпт LLM. Зібраний текст моделі: "
        "task_show, розділ «Промпт, який іде в LLM».")


@tool("tag_category_create", group="monitoring", mutates=True, scope=SCOPE_CREATE, params={
      "key": "Ключ slug до 32 символів (латиниця, цифри, _, -). Потім не змінюється: на нього посилаються теги.",
      "label": "Людська назва (до 80 символів).",
      "closed": "true — пайплайн не вигадує нові теги, лише сід-список. Сід додається через tag_create.",
      "hint": "Підказка для промпта відкритої категорії (до 300 символів). Порожньо — дефолтна підказка в коді.",
      "order": "Порядок у списках. Менше — вище."})
def tag_category_create(key: str, label: str, closed: bool = False, hint: str = "",
                        order: int = 100):
    """Створити категорію тегів. Без цього рядка фасет подій `?tag_<ключ>` не реєструється.

    Закрита категорія не забороняє tag_create: сід-список якраз набирається тут,
    а пайплайн нові значення відкидає.
    """
    key = _key(key)
    label = _clip(label, 80, "назва")
    if not label:
        raise ToolError("назва порожня")
    hint = _clip(hint, 300, "підказка")
    if TagCategory.objects.filter(key=key).exists():
        raise ToolError(f"категорія «{key}» уже є (правки: tag_category_update)")
    c = TagCategory(key=key, label=label, closed=bool(closed), hint=hint,
                    order=max(0, int(order)))
    _save(c)
    from analysis.services.tags import invalidate_cache
    invalidate_cache()
    return fmt.section(f"Категорія {c.key} створена", _category_card(c))


@tool("tag_category_update", group="monitoring", mutates=True, params={
      "key": "Ключ категорії. Сам ключ не змінюється.",
      "label": "Нова назва. Порожньо = не змінювати.",
      "closed": "Закрити (лише сід-список) або відкрити. Не передавати = не змінювати.",
      "hint": "Підказка промпта. Порожньо = не змінювати; '-' = очистити.",
      "order": "Порядок. -1 = не змінювати."})
def tag_category_update(key: str, label: str = "", closed: bool = None, hint: str = "",
                        order: int = -1):
    """Змінити назву, прапор closed, підказку або порядок категорії тегів.

    Прапор closed воркери тримають у пам'яті процесу. Після зміни перезапусти
    воркери класифікації (`service_restart`), інакше до рестарту лишиться старе.
    """
    c = _category(key)
    changed = []
    if label:
        c.label = _clip(label, 80, "назва")
        if not c.label:
            raise ToolError("назва порожня")
        changed.append(f"назва={c.label}")
    if closed is not None:
        c.closed = bool(closed)
        changed.append("тип=" + ("закрита" if c.closed else "відкрита"))
    if hint:
        c.hint = "" if hint.strip() == "-" else _clip(hint, 300, "підказка")
        changed.append("підказка " + ("очищена" if not c.hint else f"({len(c.hint)} симв)"))
    if order >= 0:
        c.order = int(order)
        changed.append(f"порядок={c.order}")
    if not changed:
        return f"{c.key}: нічого не змінено (жоден параметр не передано)"
    _save(c)
    if closed is not None:
        from analysis.services.tags import invalidate_cache
        invalidate_cache()
        changed.append("кеш closed цього процесу скинуто; воркери — після service_restart")
    return fmt.section(f"Категорія {c.key}", "\n".join(changed))


@tool("tag_category_delete", group="monitoring", mutates=True, params={
      "key": "Ключ категорії.",
      "confirm": "true — підтвердити, якщо є теги або задачі. Теги видаляються, задачі відв'язуються, з подій і постів тег знімається."})
def tag_category_delete(key: str, confirm: bool = False):
    """Видалити категорію тегів. Порожню — одразу; з тегами чи задачами — лише з confirm=true.

    Рубрики research зберігають ключ текстом: поки така рубрика є, категорію
    не видалити (інакше ключ зависне).
    """
    c = _category(key)
    rubrics = list(ResearchRubric.objects.filter(tag_category__iexact=c.key)
                   .select_related("task").order_by("task__slug", "key")[:8])
    if rubrics:
        shown = ", ".join(f"{r.task.slug}/{r.key or r.tag_name}" for r in rubrics)
        raise ToolError(f"категорію «{c.key}» не видалено: на неї посилаються рубрики "
                        f"({shown}). Спершу зміни їх tag_category.")
    n_tags = Tag.objects.filter(category=c.key).count()
    task_slugs = list(c.tasks.order_by("slug").values_list("slug", flat=True))
    if (n_tags or task_slugs) and not confirm:
        bits = []
        if n_tags:
            bits.append(f"{n_tags} тегів (будуть видалені)")
        if task_slugs:
            bits.append("задачі відв'яжуться: " + ", ".join(task_slugs))
        raise ToolError(f"категорію «{c.key}» не видалено: " + "; ".join(bits)
                        + ". Підтверди confirm=true.")
    with transaction.atomic():
        if n_tags:
            Tag.objects.filter(category=c.key).delete()
        c.delete()
    from analysis.services.tags import invalidate_cache
    invalidate_cache()
    return (f"категорію «{c.key}» видалено"
            + (f", тегів {n_tags}" if n_tags else "")
            + (f", відв'язано задач: {', '.join(task_slugs)}" if task_slugs else ""))


@tool("tags_list", group="monitoring", params={
      "category": "Ключ категорії. Порожньо = усі.",
      "query": "Частина назви тега.",
      "limit": "Скільки рядків. Стеля 200."})
def tags_list(category: str = "", query: str = "", limit: int = 80):
    """Теги довідника: категорія, назва, скільки подій і аліасів."""
    qs = Tag.objects.all()
    if category:
        c = _category(category)
        qs = qs.filter(category=c.key)
    if query:
        qs = qs.filter(name__icontains=query.strip())
    total = qs.count()
    limit = min(max(int(limit or 80), 1), 200)
    rows_qs = (qs.order_by().annotate(n_ev=Count("events"), n_al=Count("aliases"))
               .order_by("category", "name")[:limit])
    rows = [[f"#{t.id}", t.category, t.name, t.n_ev, t.n_al or ""] for t in rows_qs]
    if not rows:
        return "тегів за цим фільтром немає"
    table = fmt.table(["id", "категорія", "назва", "подій", "аліасів"], rows)
    if total > limit:
        table += f"\nпоказано {limit} з {total} — звузь category= або query="
    return table


@tool("tag_show", group="monitoring", params={
      "ref": "Тег: числовий id (можна з #, як у tags_list), `категорія:назва` або назва, якщо вона одна в довіднику."})
def tag_show(ref: str):
    """Картка тега: де висить і які в нього аліаси."""
    t = _resolve_tag(ref)
    aliases = list(t.aliases.order_by("raw").values_list("raw", flat=True)[:40])
    extra = ""
    n_al = t.aliases.count()
    if n_al > len(aliases):
        extra = f"\n… ще {n_al - len(aliases)} аліасів"
    body = _tag_line(t)
    if aliases:
        body += "\nаліаси: " + ", ".join(aliases) + extra
    return fmt.section(f"Тег {t.category}:{t.name}", body)


@tool("tag_create", group="monitoring", mutates=True, scope=SCOPE_CREATE, params={
      "category": "Ключ категорії, яка вже існує (tag_category_create).",
      "name": "Канонічна назва тега (до 80 символів)."})
def tag_create(category: str, name: str):
    """Додати канонічний тег у категорію. Повтор з тією самою назвою не дублює.

    Працює і для закритої категорії: це і є спосіб поповнити сід-список.
    Пайплайн сам у закриту категорію тег не створить.
    """
    c = _category(category)
    name = _clip(name, 80, "назва")
    if not name:
        raise ToolError("назва порожня")
    existing = _match_name(c.key, name)
    if existing:
        return fmt.section(f"Тег {existing.category}:{existing.name} уже був",
                           _tag_line(existing))
    t = Tag(category=c.key, name=name)
    _save(t)
    return fmt.section(f"Тег {t.category}:{t.name} створено", _tag_line(t))


@tool("tag_update", group="monitoring", mutates=True, params={
      "ref": "Тег: числовий id (можна з #), `категорія:назва` або однозначна назва.",
      "name": "Нова канонічна назва. Порожньо = не змінювати. Події лишаються на тому самому id.",
      "category": "Перенести в іншу категорію (ключ). Порожньо = не змінювати."})
def tag_update(ref: str, name: str = "", category: str = ""):
    """Перейменувати тег або перенести в іншу категорію. Аліаси йдуть за id тега."""
    t = _resolve_tag(ref)
    changed = []
    if category:
        c = _category(category)
        if c.key != t.category:
            if _match_name(c.key, name or t.name, exclude_pk=t.pk):
                raise ToolError(f"у «{c.key}» тег «{name or t.name}» уже є")
            t.category = c.key
            changed.append(f"категорія={c.key}")
    if name:
        name = _clip(name, 80, "назва")
        if not name:
            raise ToolError("назва порожня")
        if _match_name(t.category, name, exclude_pk=t.pk):
            raise ToolError(f"у «{t.category}» тег «{name}» уже є")
        t.name = name
        changed.append(f"назва={t.name}")
    if not changed:
        return f"#{t.id}: нічого не змінено (жоден параметр не передано)"
    _save(t)
    return fmt.section(f"Тег {t.category}:{t.name}", "\n".join(changed))


@tool("tag_delete", group="monitoring", mutates=True, params={
      "ref": "Тег: числовий id (можна з #), `категорія:назва` або однозначна назва.",
      "confirm": "true — підтвердити, якщо тег висить на подіях, постах або профілях публікації. Зв'язки знімаються, аліаси видаляються."})
def tag_delete(ref: str, confirm: bool = False):
    """Видалити тег. Якщо він ніде не висить — одразу; інакше лише з confirm=true."""
    t = _resolve_tag(ref)
    n_ev, n_posts, n_pub, n_al = _links(t)
    if (n_ev or n_posts or n_pub) and not confirm:
        bits = []
        if n_ev:
            bits.append(f"подій {n_ev}")
        if n_posts:
            bits.append(f"постів {n_posts}")
        if n_pub:
            bits.append(f"профілів публікації {n_pub}")
        raise ToolError(f"тег «{t.category}:{t.name}» не видалено: " + ", ".join(bits)
                        + ". Підтверди confirm=true — зв'язки знімуться.")
    label = f"{t.category}:{t.name}"
    t.delete()
    tail = []
    if n_ev:
        tail.append(f"знято з подій {n_ev}")
    if n_posts:
        tail.append(f"з постів {n_posts}")
    if n_pub:
        tail.append(f"з профілів публікації {n_pub}")
    if n_al:
        tail.append(f"аліасів {n_al}")
    return f"тег «{label}» видалено" + (("; " + ", ".join(tail)) if tail else "")
