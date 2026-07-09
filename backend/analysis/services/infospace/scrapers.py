"""Реєстр кастомних web-скраперів: Source.scraper_key → клас.

Ієрархія extraction для kind=web (web-адаптер, Phase 2):
  1. scraper_key порожній і config.selectors порожній → автоекстракція
     (trafilatura сама знаходить заголовок/текст/дату — дефолт для більшості
     новинних сайтів);
  2. config.selectors = {"title":…, "body":…, "date":…} → явні CSS-селектори;
  3. scraper_key заданий → кастомний клас звідси (нестандартні API, пагінація,
     JS-агрегатори типу dzen.ru).

Кастомний скрапер:

    @register_scraper("dzen")
    class DzenScraper:
        def discover(self, source) -> list[str]: ...   # лінки статей
        def extract(self, url: str, html: str) -> dict: ...  # title/text/date
"""

SCRAPERS: dict[str, type] = {}


def register_scraper(key: str):
    """Декоратор реєстрації кастомного скрапера під ключем key."""
    def _wrap(cls):
        if key in SCRAPERS:
            raise ValueError(f"скрапер {key!r} уже зареєстровано "
                             f"({SCRAPERS[key].__name__})")
        SCRAPERS[key] = cls
        return cls
    return _wrap


def get_scraper(key: str):
    """Інстанс кастомного скрапера; KeyError з підказкою."""
    try:
        return SCRAPERS[key]()
    except KeyError:
        raise KeyError(
            f"немає скрапера {key!r}; зареєстровані: {sorted(SCRAPERS) or '(жодного)'}"
        ) from None
