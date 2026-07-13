"""Контракт адаптера джерела (спільний інтерфейс для telegram/rss/web/vk).

Правила контракту (закріплені контракт-тестом по реєстру ADAPTERS):
  1. fetch() НЕ пише в БД — усю персистенцію робить стадія info_collect.
  2. Повертає ЛИШЕ НОВІ елементи відносно source.poll_cursor (watermark свого kind);
     source.poll_cursor мутується В ПАМ'ЯТІ, зберігає його стадія ПІСЛЯ успішного
     upsert постів (щоб збій не загубив елементи).
  3. Мережеві/парс-помилки → виняток; backoff і health рахує стадія.
  4. Ліміти: config["max_items"] на виклик; перший полінг (порожній poll_cursor) —
     config["backfill_limit"] (щоб не залити конвеєр історією).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

DEFAULT_MAX_ITEMS = 100
DEFAULT_BACKFILL_LIMIT = 20


class RateLimited(Exception):
    """Джерело просить зачекати (напр. Telegram FloodWait). НЕ вважається збоєм:
    стадія відсуває next_poll_at на retry_after БЕЗ інкременту failures."""
    def __init__(self, retry_after: float):
        self.retry_after = float(retry_after)
        super().__init__(f"rate limited, retry after {retry_after}s")


@dataclass
class RawItem:
    """Один елемент від джерела (стаття/пост/повідомлення)."""
    external_id: str                    # msg_id | guid | канонічний url
    url: str                            # канонічний лінк (canonical_url!)
    title: str = ""                     # для telegram порожній
    text: str = ""
    posted_at: datetime | None = None   # UTC; None → стадія ставить now
    author: str = ""
    meta: dict = field(default_factory=dict)  # сирі дані адаптера


class BaseSourceAdapter:
    """Базовий адаптер. Підкласи реєструються декоратором @register."""

    kind: str = ""  # має збігатися зі Source.KIND_*

    def fetch(self, source) -> list[RawItem]:
        """Повертає нові елементи джерела. Див. правила контракту вище."""
        raise NotImplementedError

    # --- спільні хелпери для підкласів ---

    @staticmethod
    def max_items(source) -> int:
        return int((source.config or {}).get("max_items", DEFAULT_MAX_ITEMS))

    @staticmethod
    def backfill_limit(source) -> int:
        return int((source.config or {}).get("backfill_limit", DEFAULT_BACKFILL_LIMIT))
