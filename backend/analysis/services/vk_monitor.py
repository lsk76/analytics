"""Збір коментарів VK для monitor-конвеєра (mon_collect_source=vk_comments).

ТРЕТІЙ спосіб збору критики поруч із TeleZip-потоком і вибіркою Telegram-
акаунтами. Усе після збору — те саме: mon_filter → mon_prescreen → тегування
агентами → `sync_comment_event` (1 коментар = 1 подія, БЕЗ дедупу).

Що саме збираємо: для кожної спільноти з «Чатів» дослідження (рядок довідника з
platform=vk) беремо пости стіни за період чанка, а під кожним — усі коментарі
разом із гілками відповідей. Коментар людини = `Post`.

Межі, які варто пам'ятати:
  * запити VK безкоштовні, але темп ~3/сек — довгий період жвавої спільноти
    триває довго (кожні 100 коментарів = окремий запит). Тому чанки по 1-3 дні,
    як у TeleZip-збору;
  * коментар може з'явитися ПІЗНІШЕ за свій пост, і тоді він потрапить у чанк
    поста, а не у свій день. Повторний збір сусіднього періоду його не задвоїть:
    ключ `Post.url` (посилання з `?reply=`) унікальний у межах задачі;
  * закриті спільноти й вимкнені коментарі — провина цілі: такий чат пропускаємо
    з попередженням у лог, а не валимо весь чанк.
"""
from __future__ import annotations

import logging

from analysis.models import Channel, MonitorChat, Post
from analysis.services import vk

logger = logging.getLogger(__name__)

DEFAULT_MAX_POSTS = 300        # постів стіни на спільноту за чанк
DEFAULT_MAX_COMMENTS = 300     # коментарів на пост (разом із гілками)


def _limit(key: str, default: int) -> int:
    from analysis.models import Setting
    try:
        return max(1, int(Setting.get(key, str(default))))
    except (TypeError, ValueError):
        return default


def communities(task):
    """Спільноти VK, підключені до задачі (вкладка «Чати»)."""
    return list(MonitorChat.objects.filter(task=task, is_active=True,
                                           channel__platform="vk")
                .select_related("channel").order_by("priority", "id"))


def owner_id_of(channel: Channel) -> int:
    """Числовий id спільноти з кешем у довіднику: резолв — зайвий запит, а
    коротке ім'я спільноти незмінне, доки її не перейменують."""
    cached = (channel.raw_meta or {}).get("vk_owner_id")
    if cached:
        return int(cached)
    oid, _kind = vk.resolve_owner(channel.url)
    meta = dict(channel.raw_meta or {})
    meta["vk_owner_id"] = oid
    channel.raw_meta = meta
    channel.save(update_fields=["raw_meta"])
    return oid


def _comment_posts(task, channel, owner_id, post_id, max_comments):
    """Коментарі одного поста → список незбережених `Post`."""
    from analysis.services.monitor_stages import _content_hash

    items, authors = vk.all_comments(owner_id, post_id, limit=max_comments)
    out = []
    for c in items:
        text = (c.get("text") or "").strip()
        cid = int(c.get("id") or 0)
        if not text or not cid:
            continue
        from_id = int(c.get("from_id") or 0)
        out.append(Post(
            task=task,
            url=vk.comment_url(owner_id, post_id, cid),
            stage=Post.STAGE_MON_COLLECTED,
            channel=channel,
            channel_name=(channel.title or channel.url)[:128],
            region_subject_id=channel.region_subject_id,
            posted_at=vk.ts_to_dt(c.get("date")),
            text=text,
            content_hash=_content_hash(text),
            author_name=vk.author_name(authors.get(from_id))[:128],
            # те саме поле, що й для Telegram: «скільки РІЗНИХ людей» рахується
            # по ньому (metrics.people_count). У VK-задачі тут id людини з VK.
            author_tg_id=from_id or None,
            reply_to_msg=post_id,
            classification={"_monitor": True, "_collect_source": "vk_comments",
                            "_vk": {"owner_id": owner_id, "post_id": post_id,
                                    "comment_id": cid, "author_id": from_id}},
        ))
    return out


def collect_window(task, since, until) -> int:
    """Зібрати коментарі всіх спільнот задачі за вікно → скільки постів створено.

    Викликається стадією `mon_collect` на чанк (див. monitor_stages). Помилки
    мережі/темпу VK піднімаються нагору — ретрай чанка вирішує стадія.
    """
    chats = communities(task)
    if not chats:
        raise ValueError(
            f"{task.slug}: у дослідженні немає жодної активної спільноти VK. "
            "Додай її у «Чати» (chat_add https://vk.com/…) — збір коментарів VK "
            "читає рядки довідника з platform=vk.")
    max_posts = _limit("vk_max_posts_per_chunk", DEFAULT_MAX_POSTS)
    max_comments = _limit("vk_max_comments_per_post", DEFAULT_MAX_COMMENTS)

    candidates: list[Post] = []
    for mc in chats:
        ch = mc.channel
        try:
            owner_id = owner_id_of(ch)
            wall = vk.wall_posts_between(owner_id, since, until, max_posts=max_posts)
        except vk.VkAccessDenied as e:
            logger.warning("vk_comments %s: спільнота %s закрита (%s) — пропускаю",
                           task.slug, ch.url, e)
            continue
        n_before = len(candidates)
        for item in wall:
            pid = int(item.get("id") or 0)
            if not pid or not int(item.get("comments", {}).get("count") or 0):
                continue
            try:
                candidates.extend(_comment_posts(task, ch, owner_id, pid, max_comments))
            except vk.VkAccessDenied as e:
                logger.info("vk_comments %s: коментарі %s закриті (%s)",
                            task.slug, vk.post_url(owner_id, pid), e)
        logger.info("vk_comments %s: %s — %d постів стіни, +%d коментарів",
                    task.slug, ch.url, len(wall), len(candidates) - n_before)

    if not candidates:
        return 0
    # Дедуп у межах задачі: той самий коментар міг прийти з сусіднього чанка
    # (коментують і через тиждень після поста).
    urls = [p.url for p in candidates]
    existing = set()
    for i in range(0, len(urls), 2000):
        existing.update(Post.objects.filter(task=task, url__in=urls[i:i + 2000])
                        .values_list("url", flat=True))
    fresh, seen = [], set()
    for p in candidates:
        if p.url in existing or p.url in seen:
            continue
        seen.add(p.url)
        fresh.append(p)
    if fresh:
        Post.objects.bulk_create(fresh, batch_size=1000, ignore_conflicts=True)
    return len(fresh)
