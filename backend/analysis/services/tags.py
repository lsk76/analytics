"""
ЄДИНИЙ сервіс тегів — одна точка входу для ВСІХ пайплайнів (events, monitor,
validate-інжести, адмін-скрипти).

Правила:
  * closed-флаг категорії живе ТІЛЬКИ в TagCategory (БД) — call-site його не знає
    і не передає. Помітив категорію closed в адмінці → всі пайплайни одразу
    перестають створювати в ній нові теги.
  * closed=True  -> resolve мапить варіант на найближчий канонічний тег
    (аліас-кеш / iexact / префікс+fuzzy). Немає збігу → None, тег НЕ створюється.
  * closed=False -> LLM-канонізація проти наявних тегів категорії, новий
    нормалізований тег створюється лише якщо нічого не підійшло.
  * merge/rename в monitor_review_tags має писати TagAlias через add_alias(),
    щоб мердж був незворотним (інжест завжди проходить крізь аліаси).

Двигун — normalize.resolve_in_category; цей модуль лише дає uniform API і
бере closed з БД.
"""
import logging
from typing import Optional

from analysis.models import Tag, TagAlias, TagCategory
from analysis.services.normalize import resolve_in_category

logger = logging.getLogger(__name__)

# key -> closed; маленький і стабільний — кешуємо на процес
_closed_cache: dict[str, bool] = {}


def _closed(category: str) -> bool:
    if category not in _closed_cache:
        row = TagCategory.objects.filter(key=category).first()
        # Невідома категорія = closed: нічого не створюємо для категорій,
        # яких немає в довіднику (захист від одруківок LLM у назві категорії).
        _closed_cache[category] = row.closed if row else True
    return _closed_cache[category]


def invalidate_cache() -> None:
    _closed_cache.clear()


def resolve(category: str, raw: str) -> Optional[Tag]:
    """Канонічний Tag для вільного тексту `raw` у `category`, або None.

    None означає: closed-категорія і варіант не мапиться на канон — тег
    просто відкидається (НЕ створюється), як і просив власник даних.
    """
    category = (category or "").strip()
    if not category:
        return None
    return resolve_in_category(raw, category, _closed(category))


def add_alias(category: str, variant: str, canonical_tag: Tag) -> bool:
    """Записати аліас variant -> canonical_tag (для merge/rename), щоб мердж
    пережив будь-який наступний інжест. False = колізія raw з іншою категорією."""
    key = (variant or "").strip().lower()
    if not key:
        return False
    existing = TagAlias.objects.filter(raw=key).select_related("tag").first()
    if existing:
        if existing.tag.category != canonical_tag.category:
            # TagAlias.raw глобально unique — крос-категорійний raw не чіпаємо
            # (та сама колізія, що колись з'їла «теорія_змови» opinion vs topic).
            logger.warning("tag alias collision: %r already -> %s:%s",
                           key, existing.tag.category, existing.tag.name)
            return False
        if existing.tag_id != canonical_tag.id:
            existing.tag = canonical_tag
            existing.save(update_fields=["tag"])
        return True
    TagAlias.objects.create(raw=key, tag=canonical_tag)
    return True


# ---------------------------------------------------------------------------
# Шкальні категорії: значення ВЗАЄМОВИКЛЮЧНІ (важливість_1 … важливість_5).
# LLM регулярно віддає два значення одразу, і це не косметика: фільтр публікації
# виключає «важливість_1/2», а виключення перемагає збіг — тож подія з парою
# (важливість_1, важливість_4) мовчки НЕ виходить у канал. Ловили на проді
# 17.09.2026: канал затих, хоча події були.
# ---------------------------------------------------------------------------
SCALE_CATEGORIES = {"importance", "fed_importance"}


def _scale_rank(name: str) -> int:
    """«важливість_4» → 4. Без числа в хвості — найнижчий ранг."""
    tail = (name or "").rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def collapse_scales(tag_objs):
    """З кількох значень однієї шкальної категорії лишає НАЙВИЩЕ.

    Порядок решти тегів зберігається — виклик безпечний для будь-якого списку.
    """
    best = {}
    for t in tag_objs:
        if t.category in SCALE_CATEGORIES:
            cur = best.get(t.category)
            if cur is None or _scale_rank(t.name) > _scale_rank(cur.name):
                best[t.category] = t
    if not best:
        return list(tag_objs)
    out = [t for t in tag_objs if t.category not in SCALE_CATEGORIES]
    out.extend(best.values())
    return out
