"""Золоті фікстури: кожен кастомний скрапер SCRAPERS проганяється по збереженому
HTML і звіряється з очікуваним RawItem. Ловить регресії скрапера в НАШОМУ коді.
Конвенція: fixtures/golden/<key>.html + <key>.expected.json (див. README)."""
import json
from pathlib import Path

import pytest

from analysis.services.infospace.scrapers import SCRAPERS, get_scraper, register_scraper

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def _golden_keys():
    return [p.stem for p in GOLDEN.glob("*.html")] if GOLDEN.exists() else []


@pytest.mark.parametrize(
    "key",
    _golden_keys() or [pytest.param(
        None, marks=pytest.mark.skip(
            reason="золоті фікстури зʼявляться з першим кастомним скрапером"))],
)
def test_registered_scraper_matches_golden(key):
    html = (GOLDEN / f"{key}.html").read_text(encoding="utf-8")
    expected = json.loads((GOLDEN / f"{key}.expected.json").read_text(encoding="utf-8"))
    got = get_scraper(key).extract(expected.get("url", ""), html)
    for field, needle in (expected.get("fields") or {}).items():
        assert needle in (got.get(field) or ""), f"{key}.{field}: очікувалось «{needle}»"


def test_golden_harness_demo():
    """Демо патерну (готовий шаблон): реєструємо скрапер, звіряємо з «золотим»."""
    @register_scraper("_golden_demo")
    class _Demo:
        def extract(self, url, html):
            from selectolax.parser import HTMLParser
            t = HTMLParser(html)
            return {"title": t.css_first("h1").text(strip=True),
                    "text": t.css_first(".body").text(strip=True), "date": None}
    try:
        html = "<h1>Заголовок</h1><div class='body'>Тіло статті достатньої довжини.</div>"
        got = get_scraper("_golden_demo").extract("https://x/1", html)
        assert got["title"] == "Заголовок"
        assert "Тіло статті" in got["text"]
    finally:
        SCRAPERS.pop("_golden_demo", None)
