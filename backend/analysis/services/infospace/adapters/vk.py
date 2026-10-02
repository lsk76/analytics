"""VK-адаптер: полінг стіни спільноти через VK API.

Вся мережа — в `analysis/services/vk.py` (токен, темп, розбір помилок); тут
лише перетворення постів стіни на `RawItem` і watermark.

Watermark у `source.poll_cursor`:
  owner_id     — числовий id спільноти (від'ємний), щоб не резолвити щоразу;
  last_post_id — найбільший id поста, який уже віддали.
Закріплений пост (`is_pinned`) приходить першим незалежно від дати — його не
беремо за межу, інакше кожен полінг вважав би стіну «без нового» або, навпаки,
переемітив би старе.

Помилки VK мапимо на контракт адаптерів: темп (`VkRateLimited`) → `RateLimited`
(пауза без інкременту збоїв), решта — виняток, збій джерела рахує стадія.
"""
from __future__ import annotations

from analysis.services import vk

from . import register
from .base import BaseSourceAdapter, RateLimited, RawItem


@register
class VkAdapter(BaseSourceAdapter):
    kind = "vk"

    def _owner_id(self, source, poll_cursor: dict) -> int:
        oid = poll_cursor.get("owner_id")
        if oid:
            return int(oid)
        oid, _kind = vk.resolve_owner(source.url)
        poll_cursor["owner_id"] = oid
        return oid

    def fetch(self, source) -> list[RawItem]:
        poll_cursor = dict(source.poll_cursor or {})
        first_poll = "last_post_id" not in poll_cursor
        last_id = int(poll_cursor.get("last_post_id") or 0)
        limit = self.backfill_limit(source) if first_poll else self.max_items(source)

        try:
            owner_id = self._owner_id(source, poll_cursor)
            res = vk.wall_get(owner_id, count=min(limit, 100))
        except vk.VkRateLimited as e:
            raise RateLimited(e.retry_after) from e

        items: list[RawItem] = []
        max_id = last_id
        for it in (res.get("items") or []):
            pid = int(it.get("id") or 0)
            if it.get("is_pinned"):
                continue
            if pid <= last_id:
                continue           # стіна йде від найновіших — далі лише старе
            max_id = max(max_id, pid)
            text = vk.post_text(it)
            if not text:
                continue           # фото без підпису: скрінити нічого
            items.append(RawItem(
                external_id=str(pid),
                url=vk.post_url(owner_id, pid),
                title="",
                text=text,
                posted_at=vk.ts_to_dt(it.get("date")),
                author=source.name,
                meta={"vk": {"owner_id": owner_id, "post_id": pid,
                             "views": (it.get("views") or {}).get("count"),
                             "is_repost": bool(it.get("copy_history"))}},
            ))
            if len(items) >= limit:
                break

        # перший полінг: беремо лише backfill найновіших, але watermark ставимо
        # по НАЙБІЛЬШОМУ побаченому id — інакше наступний прохід віддав би
        # ті самі пости ще раз
        poll_cursor["last_post_id"] = max(max_id, last_id)
        source.poll_cursor = poll_cursor
        return items
