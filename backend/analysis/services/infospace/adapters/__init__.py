"""Реєстр адаптерів джерел: Source.kind → клас адаптера.

Адаптер додається декоратором:

    from analysis.services.infospace.adapters import register
    from analysis.services.infospace.adapters.base import BaseSourceAdapter

    @register
    class RssAdapter(BaseSourceAdapter):
        kind = "rss"
        def fetch(self, source): ...

Реалізації заїжджають по фазах (див. docs/infospace-monitoring-pipeline.md):
Phase 1 — rss; Phase 2 — web; Phase 3 — telegram; Phase 4 — vk.
"""
from .base import BaseSourceAdapter, RawItem  # noqa: F401 — публічний реекспорт

ADAPTERS: dict[str, type[BaseSourceAdapter]] = {}


def register(cls: type[BaseSourceAdapter]) -> type[BaseSourceAdapter]:
    """Реєструє адаптер за його kind. Повторна реєстрація kind — помилка
    (захист від тихого перекриття при копіпасті)."""
    if not cls.kind:
        raise ValueError(f"{cls.__name__}: порожній kind")
    if cls.kind in ADAPTERS:
        raise ValueError(f"адаптер для kind={cls.kind!r} уже зареєстровано "
                         f"({ADAPTERS[cls.kind].__name__})")
    ADAPTERS[cls.kind] = cls
    return cls


def get_adapter(kind: str) -> BaseSourceAdapter:
    """Інстанс адаптера для kind; KeyError з підказкою, якщо не реалізовано."""
    try:
        return ADAPTERS[kind]()
    except KeyError:
        raise KeyError(
            f"немає адаптера для kind={kind!r}; зареєстровані: "
            f"{sorted(ADAPTERS) or '(жодного)'}"
        ) from None


# Реєстрація конкретних адаптерів (імпорт нижче register/get_adapter — без циклу).
# Phase 1 — rss; Phase 2 — web; Phase 3 — telegram.
from . import rss  # noqa: E402,F401 — реєструє RssAdapter
