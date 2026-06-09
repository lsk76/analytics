"""
Open vocabulary with LLM canonicalization -> no semantic duplicates.

LLM emits free text (росіянин / русский / росіянка / "выходец из Таджикистана").
Each is normalized to ONE canonical term:
  1. exact alias hit (lowercased) -> canonical            [free, cached]
  2. LLM: map to an existing canonical OR a new normalized name (singular, uk),
     then cache the mapping as an alias                    [1 LLM call per NEW term]

This collapses synonyms / cross-language / gender variants that fuzzy matching
cannot ("русский"==="росіянин"==="росіянка" -> "росіянин").
"""
import json
import logging
from typing import List, Optional, Tuple

from django.conf import settings
from openai import OpenAI
from rapidfuzz.fuzz import token_set_ratio

from analysis.models import Tag, TagAlias, Region, RegionAlias

logger = logging.getLogger(__name__)


def _key(text: str) -> str:
    return (text or "").strip().lower()


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _sync_llm(prompt: str) -> str:
    try:
        client = OpenAI(api_key=settings.OPENROUTER_API_KEY, base_url=settings.OPENROUTER_API_BASE_URL)
        # canonicalize / region-resolve LLM replies = a single short string (tag name or
        # JSON {subject,settlement}); 256 tokens is more than enough. Without explicit
        # max_tokens, the model's theoretical max (65535 for Gemini 2.5 Flash) gets
        # reserved against the key's credit limit → spurious 402 even when we'd only
        # actually use ~30 tokens.
        resp = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
            timeout=60,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("canonicalize LLM error: %s", e)
        return ""


def resolve_in_category(raw: str, category: str, closed: bool) -> Optional[Tag]:
    """
    Canonicalize a raw value WITHIN a known category.
      closed=True  -> map to the closest seeded tag (gender/number variants); if no
                      match, DROP (return None) — never invent a tag in a closed cat.
      closed=False -> LLM-canonicalize against existing tags of this category (merge
                      synonyms like русский/росіянин), else create a new normalized tag.
    """
    key = _key(raw)
    if not key:
        return None
    hit = TagAlias.objects.filter(raw=key, tag__category=category).select_related("tag").first()
    if hit:
        return hit.tag

    seeded = list(Tag.objects.filter(category=category).values_list("name", flat=True))

    if closed:
        name = raw.strip()[:80]
        obj = Tag.objects.filter(category=category, name__iexact=name).first()
        if not obj and seeded:
            nl = name.lower()
            best = max(seeded, key=lambda s: (_common_prefix(nl, s.lower()),
                                              token_set_ratio(nl, s.lower())))
            bl = best.lower()
            if _common_prefix(nl, bl) >= 4 or token_set_ratio(nl, bl) >= 85:
                obj = Tag.objects.filter(category=category, name__iexact=best).first()
        if not obj:
            return None
    else:
        prompt = (
            f'Канонізуй значення категорії "{category}".\n'
            f'Наявні канонічні: {seeded or "(порожньо)"}\n'
            f'Значення: "{raw}"\n'
            'Якщо за змістом = одне з наявних — поверни ТОЧНО його. Інакше — нову '
            'нормалізовану назву (українською, узагальнено, однина). Лише назва.'
        )
        ans = _sync_llm(prompt).strip().strip('".').strip() or raw.strip()
        name = ans[:80].lower()       # open-category values are lowercase (автоледі, не Автоледі)
        obj = (Tag.objects.filter(category=category, name__iexact=name).first()
               or Tag.objects.create(name=name, category=category))

    TagAlias.objects.get_or_create(raw=key, defaults={"tag": obj})
    return obj


# Words a model may put in a field instead of leaving it empty.
_PLACEHOLDERS = {
    "порожньо", "порожнє", "пусто", "немає", "нема", "невідомо", "не визначено",
    "не вказано", "відсутнє", "відсутній", "none", "null", "empty", "n/a", "na",
    "-", "—", "?", "невідома", "невідомий",
}


def _blank_placeholder(s: str) -> str:
    return "" if s.strip().lower().strip(".") in _PLACEHOLDERS else s


# Authoritative city -> RF subject for the most confusable cases: federal cities
# are their OWN subjects (not the surrounding oblast), and a few capitals get
# mis-geolocated by the LLM. Keyed by lowercase settlement.
CITY_SUBJECT = {
    "москва": "Москва",
    "санкт-петербург": "Санкт-Петербург", "санкт петербург": "Санкт-Петербург",
    "петербург": "Санкт-Петербург", "спб": "Санкт-Петербург", "питер": "Санкт-Петербург",
    "севастополь": "Севастополь",
    "іркутськ": "Іркутська область", "иркутск": "Іркутська область",
    "чита": "Забайкальський край",
    "казань": "Татарстан", "уфа": "Башкортостан", "махачкала": "Дагестан",
    "владивосток": "Приморський край",
    "хабаровськ": "Хабаровський край", "хабаровск": "Хабаровський край",
    "єкатеринбург": "Свердловська область", "екатеринбург": "Свердловська область",
    "новосибірськ": "Новосибірська область", "новосибирск": "Новосибірська область",
    "нижній новгород": "Нижегородська область", "нижний новгород": "Нижегородська область",
    "ростов-на-дону": "Ростовська область", "ростов на дону": "Ростовська область",
    "краснодар": "Краснодарський край",
    "красноярськ": "Красноярський край", "красноярск": "Красноярський край",
    "перм": "Пермський край", "пермь": "Пермський край",
    "самара": "Самарська область",
    "челябінськ": "Челябінська область", "челябинск": "Челябінська область",
    "воронеж": "Воронезька область", "волгоград": "Волгоградська область",
    "саратов": "Саратовська область", "тюмень": "Тюменська область",
    "омськ": "Омська область", "омск": "Омська область",
    "сургут": "Ханти-Мансійський АО — Югра",
    "нижньовартовськ": "Ханти-Мансійський АО — Югра", "нижневартовск": "Ханти-Мансійський АО — Югра",
}


def resolve_region(raw: str) -> Tuple[Optional[Region], str]:
    """
    Free-text region -> (canonical RF subject, settlement).
    Subject is chosen from the seeded list of RF federal subjects; the LLM also
    geolocates bare city names (e.g. "Коркіно" -> "Челябінська область", settlement="Коркіно").
    A settlement that names a known city authoritatively fixes the subject
    (federal cities, commonly-confused capitals) regardless of any region hint.
    """
    key = _key(raw)
    if not key:
        return None, ""

    cacheable = len(key) <= 200  # RegionAlias.raw is varchar(200); skip long free text
    if cacheable:
        hit = RegionAlias.objects.filter(raw=key).select_related("region").first()
        if hit:
            return hit.region, hit.settlement

    subjects: List[str] = list(Region.objects.values_list("name", flat=True))
    prompt = (
        "Визнач суб'єкт РФ і населений пункт для тексту про місце події.\n"
        f"Дозволені суб'єкти РФ (обери ТОЧНО один зі списку): {subjects}\n"
        f"Текст: \"{raw}\"\n"
        "ПРАВИЛА:\n"
        "- Визначай суб'єкт ЗА НАСЕЛЕНИМ ПУНКТОМ за його фактичною адмінналежністю; "
        "якщо вказаний у тексті регіон суперечить місту — довіряй МІСТУ.\n"
        "- Москва, Санкт-Петербург, Севастополь — це ОКРЕМІ суб'єкти, "
        "а НЕ Московська/Ленінградська область чи Крим.\n"
        "- Якщо суб'єкт не визначається — subject порожній.\n"
        'Поверни СТРОГО JSON: {"subject":"<назва зі списку або порожньо>","settlement":"<місто/село або порожньо>"}\n'
        "Лише JSON."
    )
    raw_ans = _sync_llm(prompt).strip()
    if raw_ans.startswith("```"):
        raw_ans = raw_ans.split("```", 2)[1]
        if raw_ans.startswith("json"):
            raw_ans = raw_ans[4:]
    subject, settlement = "", ""
    try:
        a, b = raw_ans.find("{"), raw_ans.rfind("}")
        data = json.loads(raw_ans[a:b + 1])
        subject = _blank_placeholder((data.get("subject") or "").strip())
        settlement = _blank_placeholder((data.get("settlement") or "").strip())[:160]
    except Exception:  # noqa: BLE001
        pass

    # authoritative override: a known city fixes the subject (СПб≠ЛО, Іркутськ≠Забайкалля)
    override = CITY_SUBJECT.get(_key(settlement))
    if override:
        subject = override

    region_obj = None
    if subject:
        region_obj = Region.objects.filter(name__iexact=subject).first()
        if not region_obj:  # LLM proposed a subject not in the seed — keep it (open fallback)
            region_obj = Region.objects.create(name=subject)

    if cacheable:
        RegionAlias.objects.get_or_create(
            raw=key, defaults={"region": region_obj, "settlement": settlement})
    return region_obj, settlement
