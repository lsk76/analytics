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

from analysis.models import (
    Nationality, NationalityAlias, ConflictType, ConflictTypeAlias,
    Region, RegionAlias,
)

logger = logging.getLogger(__name__)


def _key(text: str) -> str:
    return (text or "").strip().lower()


def _sync_llm(prompt: str) -> str:
    try:
        client = OpenAI(api_key=settings.OPENROUTER_API_KEY, base_url=settings.OPENROUTER_API_BASE_URL)
        resp = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            timeout=60,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("canonicalize LLM error: %s", e)
        return ""


_NAT_RULE = ('українською, в однині, для націй/етносів — чоловічий рід в називному відмінку '
             '(напр. "таджик", "росіянин", "чеченець"); узагальнені групи лишай як є '
             '(напр. "мігрант", "місцеві", "кавказець")')
_TYPE_RULE = 'українською, узагальнено (напр. "напад", "бійка", "вбивство", "погроза", "погром")'


def _canonicalize(raw: str, model, alias_model, alias_fk: str, rule: str) -> Optional[object]:
    key = _key(raw)
    if not key:
        return None

    hit = alias_model.objects.filter(raw=key).select_related(alias_fk).first()
    if hit:
        return getattr(hit, alias_fk)

    existing: List[str] = list(model.objects.values_list("name", flat=True))
    prompt = (
        f'Канонізуй термін.\n'
        f'Наявні канонічні назви: {existing or "(порожньо)"}\n'
        f'Термін: "{raw}"\n'
        f'Якщо термін за змістом = одна з наявних назв — поверни ТОЧНО цю назву.\n'
        f'Інакше поверни нову нормалізовану назву: {rule}.\n'
        f'Відповідь — лише назва, без лапок, пояснень чи крапки.'
    )
    ans = _sync_llm(prompt).strip().strip('".').strip()
    if not ans or len(ans) > 80:
        ans = raw.strip()[:80]

    obj = model.objects.filter(name__iexact=ans).first() or model.objects.create(name=ans)
    alias_model.objects.get_or_create(raw=key, defaults={alias_fk: obj})
    return obj


def resolve_nationality(raw: str) -> Optional[Nationality]:
    return _canonicalize(raw, Nationality, NationalityAlias, "nationality", _NAT_RULE)


def resolve_conflict_type(raw: str) -> Optional[ConflictType]:
    return _canonicalize(raw, ConflictType, ConflictTypeAlias, "conflict_type", _TYPE_RULE)


def resolve_region(raw: str) -> Tuple[Optional[Region], str]:
    """
    Free-text region -> (canonical RF subject, settlement).
    Subject is chosen from the seeded list of RF federal subjects; the LLM also
    geolocates bare city names (e.g. "Коркіно" -> "Челябінська область", settlement="Коркіно").
    """
    key = _key(raw)
    if not key:
        return None, ""

    hit = RegionAlias.objects.filter(raw=key).select_related("region").first()
    if hit:
        return hit.region, hit.settlement

    subjects: List[str] = list(Region.objects.values_list("name", flat=True))
    prompt = (
        "Визнач суб'єкт РФ і населений пункт для тексту про місце події.\n"
        f"Дозволені суб'єкти РФ (обери ТОЧНО один зі списку): {subjects}\n"
        f"Текст: \"{raw}\"\n"
        "Якщо вказано лише місто/село — визнач, до якого суб'єкта воно належить.\n"
        "Якщо суб'єкт не визначається — subject порожній.\n"
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
        subject = (data.get("subject") or "").strip()
        settlement = (data.get("settlement") or "").strip()[:160]
    except Exception:  # noqa: BLE001
        pass

    region_obj = None
    if subject:
        region_obj = Region.objects.filter(name__iexact=subject).first()
        if not region_obj:  # LLM proposed a subject not in the seed — keep it (open fallback)
            region_obj = Region.objects.create(name=subject)

    RegionAlias.objects.get_or_create(
        raw=key, defaults={"region": region_obj, "settlement": settlement})
    return region_obj, settlement
