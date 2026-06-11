"""
Exclusion filters for opinion-monitor pipeline.

Goal: drop noise BEFORE feeding to the LLM tagger.

ВАЖЛИВО — філософія:
  * Основний шум (нерухомість, перевезення, продажі, робота, спам, sex, наркотики,
    криптовалюта, оренда) ВИКЛЮЧАЄТЬСЯ НА СТОРОНІ TELEZIP через cluster-теги
    у запиті, напр.: `-(##нерухомість ##перевозки ##продажа ##робота ##спам)`.
    Це робиться у `monitor_collect` через `AnalysisTask.telezip_query`.

  * Цей regex-шар лишився мінімальним: ловить тільки ботів і дуже специфічний
    шум, який TeleZip-кластери не покривають.

  * Підписи / прес-релізи / новинні репости ловить `monitor_filter --max-length`.

Перевірка показала: після TeleZip-cluster виключень, 5 наших старих regex-категорій
(transport, realestate, gadget, car, job) давали 100% false positives — ловили
невинні згадки слів ("у тебя Айфон или Андрей?", "3.5 млн человек"). Тому
викинуто.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class FilterRule:
    pattern: re.Pattern
    label: str
    description: str = ""


def _r(pat: str, label: str, desc: str = "", flags=re.I) -> FilterRule:
    return FilterRule(re.compile(pat, flags), label, desc)


# =========================================================================
# Common exclusions — apply to every region.
# =========================================================================

COMMON_PATTERNS: List[FilterRule] = [
    # --- Prayer-time bot («Зухр: 11:46  Махачкала  Бот: Namaz Time») -----
    # TeleZip кластери цього не вловлюють — лишаємо локально.
    _r(r"namaz\s*time", "namaz_bot"),
    _r(r"^(зухр|аср|магриб|иша|восход|фаджр)\s*[:\s]\s*\d", "namaz_bot"),
]


# =========================================================================
# Regional-specific exclusions
# =========================================================================

REGIONAL_PATTERNS: dict = {
    # Поки порожньо. Якщо побачимо регіональний шум, який TeleZip не ловить —
    # додамо вузькі ознаки (точний хедер прес-релізу, лого/підпис).
    # НЕ персоналії — бо ризикуємо відсіяти саме критику.
}


# =========================================================================
# Default TeleZip exclusion query for opinion-monitor tasks
# =========================================================================
# Включає 9 кластер-тегів шуму. Використовуй у `monitor_collect --query=...`
# або зберігай у `AnalysisTask.telezip_query` як дефолт.
TELEZIP_DEFAULT_EXCLUSION = (
    "-(##нерухомість ##перевозки ##продажа ##робота ##спам "
    "##sex ##аренда ##крипта ##нарко)"
)


# =========================================================================
# Public API
# =========================================================================

def classify(text: str, region: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """Return (label, description) of the first matching exclusion rule,
    or None if the text passes (kept for tagging)."""
    if not text:
        return None
    region_rules = REGIONAL_PATTERNS.get(region or "", [])
    for rule in region_rules + COMMON_PATTERNS:
        if rule.pattern.search(text):
            return rule.label, rule.description
    return None


def is_excluded(text: str, region: Optional[str] = None) -> bool:
    return classify(text, region) is not None
