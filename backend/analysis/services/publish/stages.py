"""Стадія `publish` — TASKLESS: активні PublishConfig → AI-фільтр+рерайт → Telegram.

  publish_once()  claim одну кандидат-подію профілю → AI → skip|send → PublishedEvent

Claim = наявність рядка PublishedEvent(config, event) (unique). Свіжа подія (без
рядка) клеймиться створенням pending-рядка; крешнутий pending (locked_at застарів)
пере-клеймиться. Пейсинг: PACE_SECONDS між реальними відправками; max_per_pass
обмежує розмір батчу на профіль за прохід.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone as djtz

from analysis.models import Event, PublishConfig, PublishedEvent
from .. import llm
from . import telegram
from .prompts import PUBLISH_PROMPT

logger = logging.getLogger(__name__)

LOCK_TIMEOUT = timedelta(minutes=20)   # реклейм крешнутого pending
MAX_ATTEMPTS = 3                       # після — status=failed (термінально)
PACE_SECONDS = 3.0                     # пауза між РЕАЛЬНИМИ відправками (TG rate-limit)


def _active_configs():
    return list(PublishConfig.objects.filter(is_active=True).order_by("id"))


def _candidate_events(config):
    """QS approved-подій під фільтр профілю, ще НЕ взятих цим профілем."""
    qs = Event.objects.filter(review_status=config.review_status)
    if config.task_id:
        qs = qs.filter(task_id=config.task_id)
    region_ids = list(config.regions.values_list("id", flat=True))
    if region_ids:
        qs = qs.filter(region_subject_id__in=region_ids)
    if config.publish_from:
        qs = qs.filter(event_date__gte=config.publish_from)
    if config.max_age_days:
        # ковзне вікно: старе лишається в базі, але в канал не йде
        qs = qs.filter(event_date__gte=(djtz.localdate()
                                        - timedelta(days=config.max_age_days)))
    tag_ids = list(config.tags.values_list("id", flat=True))
    if tag_ids:
        qs = qs.filter(tags__in=tag_ids)
    excl_ids = list(config.exclude_tags.values_list("id", flat=True))
    if excl_ids:
        qs = qs.exclude(tags__in=excl_ids)
    taken = PublishedEvent.objects.filter(config=config).values("event_id")
    return qs.exclude(id__in=taken).distinct().order_by("event_date", "id")


def _reclaim_stale(config):
    """Пере-клеймити крешнутий pending (locked_at застарів або NULL). Повертає
    PublishedEvent або None."""
    cutoff = djtz.now() - LOCK_TIMEOUT
    with transaction.atomic():
        pub = (PublishedEvent.objects
               .filter(config=config, status=PublishedEvent.STATUS_PENDING)
               .filter(Q(locked_at__isnull=True) | Q(locked_at__lt=cutoff))
               .select_for_update(skip_locked=True)
               .order_by("created_at")
               .first())
        if pub is None:
            return None
        pub.locked_at = djtz.now()
        pub.save(update_fields=["locked_at"])
    return pub


def _claim_fresh(config):
    """Заклеймити одну свіжу подію, створивши pending-рядок. None — нема кандидатів
    (IntegrityError = хтось випередив на цій події → повертаємо None, наступний
    прохід підбере інші)."""
    event_id = _candidate_events(config).values_list("id", flat=True).first()
    if event_id is None:
        return None
    try:
        with transaction.atomic():
            pub = PublishedEvent.objects.create(
                config=config, event_id=event_id,
                status=PublishedEvent.STATUS_PENDING, locked_at=djtz.now())
    except IntegrityError:
        return None
    return pub


def _claim(config):
    return _reclaim_stale(config) or _claim_fresh(config)


def _source_url(event) -> str:
    """URL найранішого (оригінального) поста події; '' якщо постів немає.
    posted_at ASC → NULL-дати в кінець (Postgres), тож реальний першоджерельний
    пост має пріоритет; id — тай-брейк."""
    url = (event.posts.order_by("posted_at", "id")
           .values_list("url", flat=True).first())
    return url or ""


def _event_payload(event, source_url: str) -> str:
    tags = ", ".join(t.name for t in event.tags.all())
    parts = [
        f"ДАТА: {event.event_date}",
        f"РЕГІОН: {event.region_subject.name if event.region_subject else event.region}",
    ]
    if event.settlement:
        parts.append(f"НАСЕЛЕНИЙ ПУНКТ: {event.settlement}")
    if tags:
        parts.append(f"ТЕГИ: {tags}")
    if source_url:
        parts.append(f"ПОСИЛАННЯ: {source_url}")
    parts.append(f"ОПИС:\n{event.summary}")
    return "\n".join(parts)


def _hashtag(value: str) -> str:
    """«Саха (Якутія)» → #Саха. Telegram ріже хештег на першому не-словному
    символі, тож дужки й пробіли треба зняти самим, інакше в пості лишиться
    сміття після тега."""
    word = re.split(r"[^\w']", (value or "").strip(), maxsplit=1)[0]
    return f"#{word}" if word else ""


def _media_of(event):
    """Адреса оригіналу {kind, chat, mid} — звідки публікація візьме фото/відео.

    Позначка з етапу збору (Post.media) — лише підказка про тип. Головне джерело
    істини — САМ URL поста: `t.me/<чат>/<id>` завжди веде на оригінал, тож медіа
    дістається навіть для постів, зібраних до появи позначок. Якщо на оригіналі
    медіа немає, акаунт просто відправить текст.
    """
    post = (event.posts.order_by("posted_at", "id")
            .only("classification", "media", "url").first())
    if post is None:
        return None
    media = post.media or ((post.classification or {}).get("_tgs") or {}).get("media")
    if isinstance(media, dict) and media.get("chat") and media.get("mid"):
        return media
    m = re.match(r"https?://t\.me/(?:c/)?([A-Za-z0-9_]+)/(\d+)", (post.url or "").strip())
    if not m:
        return None            # RSS/сайт — медіа немає звідки взяти
    chat = m.group(1)
    if chat.isdigit():         # t.me/c/<internal>/<id> — приватний чат
        chat = f"-100{chat}"
    return {"kind": None, "chat": chat, "mid": int(m.group(2))}


def _render_raw(event, source_url: str, header: str = "", limit: int = 3000) -> str:
    """Пост без ШІ: рубрика + ОРИГІНАЛЬНИЙ текст джерела + теги + посилання.

    Оригінал беремо з найранішого поста події — для tgsearch це сама репліка
    людини, для infospace — текст новини. Переказу немає свідомо: замовник
    читає першоджерело, а не переповідання.
    """
    post = (event.posts.order_by("posted_at", "id")
            .only("text", "url").first())
    body = html.escape(((post.text if post else "") or event.summary or "").strip())
    if len(body) > limit:
        body = body[:limit].rsplit(" ", 1)[0] + "…"
    # dict.fromkeys — дедуп зі збереженням порядку: однойменний тег може прийти
    # з двох категорій, і в пості виходило «#фальсифікації #фальсифікації»
    names = list(dict.fromkeys(t.name for t in event.tags.all()))
    hot = any(n in ("важливість_4", "важливість_5") for n in names)
    region = event.region_subject.name if event.region_subject else (event.region or "")
    head = " ".join(x for x in [(header or "").strip(), _hashtag(region)] if x)
    return "\n".join(x for x in [
        ("❗ " if hot else "") + head,
        f"#{event.id}",
        "",
        body,
        "",
        " ".join(_hashtag(n) for n in names),
        f'<a href="{source_url}">Джерело</a>' if source_url else "",
    ] if x)


def _bump_or_fail(pub, err):
    pub.attempts += 1
    pub.error = err[:2000]
    pub.locked_at = None
    if pub.attempts >= MAX_ATTEMPTS:
        pub.status = PublishedEvent.STATUS_FAILED
    pub.save(update_fields=["attempts", "error", "locked_at", "status"])


def _process(config, pub) -> bool:
    """Обробити одну заклеймлену публікацію. Повертає True, якщо БУЛА відправка
    (щоб викликач витримав PACE_SECONDS)."""
    event = pub.event
    source_url = _source_url(event)

    if config.raw_mode:
        # Без ШІ: відбір уже зробив фільтр профілю (теги/регіони/статус), тож
        # питати LLM «чи публікувати» нема сенсу — це був би другий фільтр
        # поверх першого, за гроші і з ризиком мовчазних відмов.
        media = _media_of(event)
        # Підпис до медіа вчетверо коротший за пост (ліміт Bot API 1024), тому
        # тіло ріжемо сильніше — повний текст лишається за посиланням.
        post_text = _render_raw(event, source_url, config.raw_header,
                                limit=600 if media else 3000)
        if not post_text.strip():
            _bump_or_fail(pub, "raw_mode: порожній текст джерела")
            return False
        pub.ai_verdict = True
        pub.ai_reason = "raw_mode" + (f" + {media['kind']}" if media else "")
        pub.post_text = post_text
        return _send(config, pub, event, post_text, media)

    system = (config.ai_prompt or "").strip() or PUBLISH_PROMPT
    model = (config.ai_model or "").strip() or None
    raw = asyncio.run(llm.query(
        [{"role": "system", "content": system},
         {"role": "user", "content": _event_payload(event, source_url)}],
        model=model, json_mode=True, max_tokens=1200,
        api_key=llm.key_for_user(config.owner)))

    if not (raw or "").strip():
        # порожньо (таймаут/рейт-ліміт LLM) — транзієнт: звільнити лок БЕЗ спроби
        pub.locked_at = None
        pub.save(update_fields=["locked_at"])
        return False
    verdict = llm.extract_json(raw)
    if not isinstance(verdict, dict):
        _bump_or_fail(pub, "AI: битий JSON")
        return False

    publish = bool(verdict.get("publish"))
    post_text = (verdict.get("post_text") or "").strip()
    if publish:
        # Внутрішній id події першим рядком (для звірки оператором у каналі);
        # детерміновано з коду — LLM цей номер не знає.
        post_text = f"#{event.id}\n{post_text}"
        # Страхувальна сітка: посилання на оригінал має бути в пості. Якщо модель
        # його не вставила (або вигадала свій голий https://t.me/ без message-id) —
        # дописуємо реальний URL самі. Умова `not in` уникає дублю, коли LLM уже
        # вставив саме цей URL згідно з промптом.
        if source_url and source_url not in post_text:
            post_text = f"{post_text}\n\n<a href=\"{source_url}\">Джерело</a>"
    pub.ai_verdict = publish
    pub.ai_reason = (verdict.get("reason") or "")[:2000]
    pub.post_text = post_text

    if not publish:
        pub.status = PublishedEvent.STATUS_SKIPPED
        pub.locked_at = None
        pub.save(update_fields=["ai_verdict", "ai_reason", "post_text", "status", "locked_at"])
        logger.info("publish[%s]: skip event#%d (%s)", config.name, event.id, pub.ai_reason[:80])
        return False
    if not post_text:
        _bump_or_fail(pub, "AI: publish=true, але порожній post_text")
        return False

    return _send(config, pub, event, post_text)


def _send_via_account(config, media, post_text):
    """Пост від імені акаунта: медіа першоджерела в тому ж повідомленні."""
    from accounts.services.telegram_client import TelegramUserClient
    res = TelegramUserClient.send_post_sync(
        config.forward_account, config.chat_id, post_text,
        src_chat=(media or {}).get("chat"), src_msg_id=(media or {}).get("mid") or 0)
    if not res.get("ok"):
        raise telegram.TelegramError(f"акаунт #{config.forward_account_id}: {res.get('error')}")
    return res.get("message_id")


def _send(config, pub, event, post_text, media=None) -> bool:
    """Спільний фінал для обох режимів: відправка + облік стану публікації.

    media=(kind, path) — фото/відео першоджерела йде ОДНИМ повідомленням із
    підписом: доказ і текст мають бути разом, інакше в каналі їх доводиться
    зшивати очима.
    """
    try:
        if config.post_as_account and config.forward_account_id:
            mid = _send_via_account(config, media, post_text)
        else:
            mid = telegram.send_message(config.resolved_token(), config.chat_id, post_text)
    except telegram.TelegramError as e:
        if e.retry_after:
            # rate-limit каналу — відкласти БЕЗ інкременту спроб
            pub.locked_at = None
            pub.save(update_fields=["ai_verdict", "ai_reason", "post_text", "locked_at"])
            logger.info("publish[%s]: TG rate-limit, retry_after=%s", config.name, e.retry_after)
            time.sleep(min(e.retry_after or 5, 30))
            return False
        _bump_or_fail(pub, f"TG: {e}")
        logger.warning("publish[%s]: send failed event#%d: %s", config.name, event.id, e)
        return False

    pub.tg_message_id = mid
    pub.published_at = djtz.now()
    pub.status = PublishedEvent.STATUS_PUBLISHED
    pub.locked_at = None
    pub.error = ""
    pub.save(update_fields=["ai_verdict", "ai_reason", "post_text", "tg_message_id",
                            "published_at", "status", "locked_at", "error"])
    logger.info("publish[%s]: опубліковано event#%d → msg %d", config.name, event.id, mid)
    return True


def publish_once() -> bool:
    """TASKLESS-прохід: для кожного активного профілю обробити до max_per_pass
    подій. True, якщо десь була робота (реклейм/claim/обробка)."""
    did = False
    for config in _active_configs():
        n = 0
        cap = config.max_per_pass or 5
        while n < cap:
            pub = _claim(config)
            if pub is None:
                break
            did = True
            n += 1
            sent = _process(config, pub)
            if sent:
                time.sleep(PACE_SECONDS)
    return did


STAGE_RUNNERS = {
    "publish": publish_once,   # taskless
}
