"""
Стадії infospace-конвеєра (pipeline="infospace").

  info_collect   Source(due) -> Post(info_collected)   TASKLESS (полінг джерел)
  info_screen    info_collected -> info_screened|done  (LLM: релевантність+теги)
  info_event     info_screened  -> done (-> Event)      (advisory lock, реюз dedup)
  info_retention (done, !relevant, старі) -> DELETE      (пер-задачна чистка)

Реюз: `_claim_posts`/`_advance`/`_create_event`/`_attach_posts` зі `stages.py`,
`llm.query`/`extract_json`, `normalize.resolve_region`. Дизайн:
docs/infospace-monitoring-pipeline.md §5-§6.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone as djtz
from rapidfuzz.fuzz import token_set_ratio

from analysis.models import Event, Post, Source, SourceSubscription
from .. import llm
from ..normalize import resolve_region
from ..stages import _advance, _attach_posts, _claim_posts, _create_event
from .adapters import get_adapter
from .adapters.base import RateLimited
from .prompts import INFO_JUDGE_PROMPT, INFO_SCREEN_PROMPT

logger = logging.getLogger(__name__)

LOCK_TIMEOUT = timedelta(minutes=20)
SCREEN_TICK = 10           # постів на прохід скріну (кожен = 1 LLM-виклик)
BACKOFF_CAP = timedelta(hours=6)
JUDGE_TOPK = 5             # скільки кандидатів показувати судді
FUZZY_FLOOR = 38           # мін. схожість (max від signature↔summary/↔signature)
                           # для кандидата; поріг РЕКОЛУ — точність вирішує суддя
RETENTION_TICK = 1000      # постів на прохід чистки
MAX_ITEM_AGE_DAYS = 7      # фолбек-вікно свіжості, якщо task.info_max_age_days=0/None
                           # (монітор бере лише свіже: архів, хибна дата extraction)


def _content_hash(text: str) -> str:
    return hashlib.sha1((text or "").strip()[:2000].encode("utf-8")).hexdigest()[:40]


# =========================================================================== collect

def _claim_source():
    """Атомарно захопити одне джерело, якому час полінгу, що має ≥1 активну
    підписку активної infospace-задачі. Stale-lock (>LOCK_TIMEOUT) перезахоплюється."""
    now = djtz.now()
    cutoff = now - LOCK_TIMEOUT
    # джерела з ≥1 активною підпискою активної infospace-задачі — через підзапит
    # id__in (а не JOIN), бо SELECT ... FOR UPDATE несумісний з DISTINCT у Postgres,
    # а JOIN по subscriptions дав би дублі рядків джерела.
    have_subs = SourceSubscription.objects.filter(
        is_active=True, task__is_active=True,
        task__pipeline="infospace").values("source_id")
    with transaction.atomic():
        sid = (
            Source.objects.filter(is_active=True, next_poll_at__lte=now)
            .filter(Q(locked_at__isnull=True) | Q(locked_at__lt=cutoff))
            .filter(id__in=have_subs)
            .order_by("next_poll_at", "id")
            .select_for_update(skip_locked=True)
            .values_list("id", flat=True)
            .first()
        )
        if sid is None:
            return None
        Source.objects.filter(id=sid).update(locked_at=now)
    return Source.objects.get(id=sid)


def _schedule_ok(source):
    jitter = random.uniform(-0.1, 0.1)
    delay = source.poll_interval_sec * (1 + jitter)
    source.next_poll_at = djtz.now() + timedelta(seconds=delay)
    source.last_ok_at = djtz.now()
    source.last_error = ""
    source.consecutive_failures = 0
    source.locked_at = None
    source.save(update_fields=["poll_cursor", "next_poll_at", "last_ok_at", "last_error",
                               "consecutive_failures", "locked_at"])


def _schedule_rate_limited(source, retry_after):
    """FloodWait тощо: відсунути полінг, але НЕ рахувати як збій (failures не росте)."""
    source.next_poll_at = djtz.now() + timedelta(seconds=max(retry_after, 60))
    source.locked_at = None
    source.save(update_fields=["next_poll_at", "locked_at"])


def _schedule_fail(source, err):
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    backoff = source.poll_interval_sec * (2 ** min(source.consecutive_failures, 10))
    delay = min(timedelta(seconds=backoff), BACKOFF_CAP)
    source.next_poll_at = djtz.now() + delay
    source.last_error = str(err)[:2000]
    source.locked_at = None
    source.save(update_fields=["next_poll_at", "last_error",
                               "consecutive_failures", "locked_at"])


def _fanout(source, items):
    """Створити Post на кожну активну підписку задачі (unique(task, url)).
    Наявний пост НЕ відкочуємо — лише пропускаємо (ідемпотентний повтор)."""
    subs = list(SourceSubscription.objects.filter(
        source=source, is_active=True,
        task__is_active=True, task__pipeline="infospace").select_related("task"))
    now = djtz.now()
    n_new = 0
    for sub in subs:
        # відсів застарілих елементів (архів, хибна дата extraction) — пер-задачне
        # вікно свіжості task.info_max_age_days; posted_at=None лишаємо (→ now)
        cutoff = now - timedelta(days=sub.task.info_max_age_days or MAX_ITEM_AGE_DAYS)
        for it in items:
            if it.posted_at and it.posted_at < cutoff:
                continue
            posted = it.posted_at or now
            _, created = Post.objects.get_or_create(
                task=sub.task, url=it.url,
                defaults=dict(
                    stage=Post.STAGE_INFO_COLLECTED,
                    source=source, title=it.title[:500],
                    channel_name=source.name[:128],
                    region_subject=source.region_subject,
                    posted_at=posted, text=it.text,
                    content_hash=_content_hash(it.text),
                ),
            )
            if created:
                n_new += 1
    return n_new, len(subs)


def _poll_source(source):
    """Полить ОДНЕ джерело (fetch + фан-аут + розклад/health). Спільне ядро
    info_collect_once і кнопки «Запустити зараз». Повертає к-сть НОВИХ постів."""
    try:
        items = get_adapter(source.kind).fetch(source)
    except RateLimited as e:
        logger.info("info_collect: %s rate-limited, retry за %ss", source.name, e.retry_after)
        _schedule_rate_limited(source, e.retry_after)
        return 0
    except Exception as e:  # noqa: BLE001 — health рахуємо, воркер живий
        logger.warning("info_collect: %s (%s) fetch failed: %r", source.name, source.kind, e)
        _schedule_fail(source, e)
        return 0
    n_new, n_subs = _fanout(source, items)
    _schedule_ok(source)
    logger.info("info_collect: %s → %d items, %d нових постів на %d задач",
                source.name, len(items), n_new, n_subs)
    return n_new


def info_collect_once():
    """TASKLESS: полінг одного джерела → фан-аут постів на підписані задачі."""
    source = _claim_source()
    if source is None:
        return False
    _poll_source(source)
    return True


def run_task_now(task, screen_passes=6, event_cap=300):
    """Синхронний ТЕСТ-прогін infospace-задачі: полить активні джерела →
    скрін → події. Обмежено для веб-запиту (кнопка «Запустити зараз»).
    Полінг поважає watermark: перший раз — backfill, далі — лише нове.
    Повертає лічильники."""
    subs = (SourceSubscription.objects
            .filter(task=task, is_active=True, source__is_active=True)
            .select_related("source"))
    collected = sum(_poll_source(s.source) for s in subs)
    for _ in range(screen_passes):
        if not info_screen_once(task):
            break
    screened = Post.objects.filter(task=task, stage=Post.STAGE_INFO_SCREENED).count()
    before = Event.objects.filter(task=task).count()
    for _ in range(event_cap):
        if not info_event_once(task):
            break
    after = Event.objects.filter(task=task).count()
    return {"sources": subs.count(), "collected": collected,
            "screened_pending": screened,
            "events_created": after - before, "events_total": after}


def rescreen_task_now(task):
    """ПЕРЕПРОГІН ФІЛЬТРА: видалити події задачі, скинути ВСІ її пости у
    info_collected і застосувати ПОТОЧНИЙ скрін-промпт наново — для тюнінгу
    фільтра на вже зібраних постах (без нового полінгу). Повертає лічильники."""
    Event.objects.filter(task=task).delete()   # пости.event → NULL (SET_NULL)
    n_posts = Post.objects.filter(task=task).update(
        stage=Post.STAGE_INFO_COLLECTED, is_relevant=None, stage_attempts=0,
        stage_locked_at=None, stage_error="", classification={})
    while info_screen_once(task):
        pass
    kept = Post.objects.filter(task=task, stage=Post.STAGE_INFO_SCREENED).count()
    for _ in range(5000):
        if not info_event_once(task):
            break
    return {"posts": n_posts, "relevant": kept,
            "events_total": Event.objects.filter(task=task).count()}


# =========================================================================== screen

def _build_screen_prompt(task):
    """Скрін-промпт = системний промпт задачі + (якщо є категорії тегів) схема
    tags + правила тегування. Порожній промпт → дефолт із коду."""
    system = (task.info_screen_prompt or INFO_SCREEN_PROMPT).strip()
    cats = list(task.tag_categories.all())
    if not cats:
        return system
    tag_fields = ",".join(f'"{c.key}":["..."]' for c in cats)
    lines = [system, "",
             f'Додай у JSON поле "tags" зі списками значень: {{{tag_fields}}}.']
    for c in cats:
        if c.closed:
            from analysis.models import Tag
            seeded = list(Tag.objects.filter(category=c.key).values_list("name", flat=True))
            lines.append(f'- "{c.key}" ({c.label}): ТОЧНО зі списку {seeded}; нема — пропусти.')
        else:
            lines.append(f'- "{c.key}" ({c.label}): {c.hint or "вільні значення, узагальнено"}.')
    if task.info_tagger_prompt:
        lines.append(task.info_tagger_prompt.strip())
    return "\n".join(lines)


async def _llm_screen(posts, system, model):
    """→ {post_id: (parsed|None, was_empty)}. was_empty=True — LLM віддав ""
    (транзієнт: таймаут/рейт-ліміт), НЕ битий JSON; стадія пере-черговує його
    без інкременту спроб. Конкурентність 3 (gemini-flash на OpenRouter
    рейт-лімітить при 6 — ловили масові порожні відповіді)."""
    client = llm.make_client()
    sem = asyncio.Semaphore(3)

    async def one(p):
        async with sem:
            user = f"TITLE: {p.title}\n\nTEXT:\n{p.text[:4000]}"
            raw = await llm.query(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                model=model, client=client, json_mode=True, max_tokens=1500)
        return p.id, (llm.extract_json(raw), not (raw or "").strip())

    try:
        results = await asyncio.gather(*(one(p) for p in posts))
    finally:
        await client.close()
    return dict(results)


def info_screen_once(task):
    ids = _claim_posts(task, Post.STAGE_INFO_COLLECTED, SCREEN_TICK)
    if not ids:
        return False
    model = task.info_screen_model or task.llm_model or settings.LLM_MODEL
    posts = list(Post.objects.filter(id__in=ids).order_by("posted_at", "id"))
    system = _build_screen_prompt(task)
    verdicts = asyncio.run(_llm_screen(posts, system, model))

    decided, screened, bad, transient = [], [], [], []
    for p in posts:
        v, was_empty = verdicts.get(p.id, (None, True))
        if not isinstance(v, dict):
            (transient if was_empty else bad).append(p.id)
            continue
        relevant = bool(v.get("relevant"))
        cls = dict(p.classification or {})
        cls.update({
            "signature": (v.get("signature") or "").strip(),
            "summary": (v.get("summary") or "").strip(),
            "screen_reason": (v.get("reason") or "").strip(),
            "region": (v.get("region") or "").strip() if v.get("region") else "",
            "tags": v.get("tags") or {},
            "_screen_model": model,
        })
        p.classification = cls
        p.is_relevant = relevant
        p.stage_locked_at = None
        if relevant:
            p.stage = Post.STAGE_INFO_SCREENED
            screened.append(p)
        else:
            p.stage = Post.STAGE_DONE
        decided.append(p)
    Post.objects.bulk_update(
        decided, ["classification", "is_relevant", "stage", "stage_locked_at"],
        batch_size=200)
    # транзієнт (порожня відповідь: таймаут/рейт-ліміт) — просто звільнити lock,
    # БЕЗ інкременту спроб (наступний тік підбере); битий JSON — до 3 спроб → failed
    if transient:
        Post.objects.filter(id__in=transient).update(stage_locked_at=None)
    if bad:
        _bump_attempts(bad, "info_screen")
    logger.info("info_screen[%s]: %d релевантних, %d відсіяно, %d транзієнт, %d битих",
                task.slug, len(screened), len(decided) - len(screened),
                len(transient), len(bad))
    return True


def _bump_attempts(ids, stage_label):
    """Звільнити lock, +1 спроба; після 3 → failed (як у monitor-конвеєрі)."""
    from django.db.models import F
    Post.objects.filter(id__in=ids).update(
        stage_locked_at=None, stage_attempts=F("stage_attempts") + 1)
    Post.objects.filter(id__in=ids, stage_attempts__gte=3).update(
        stage=Post.STAGE_FAILED, stage_error=f"{stage_label}: битий JSON після спроб")


# =========================================================================== event

def _judge_prompt(task, post, candidates):
    system = (task.info_judge_prompt or INFO_JUDGE_PROMPT).strip()
    cand = [{"id": e.id, "date": str(e.event_date), "summary": e.summary[:400]}
            for e in candidates]
    import json
    user = (f"NEW ITEM:\nTITLE: {post.title}\nDATE: {post.posted_at:%Y-%m-%d}\n"
            f"TEXT:\n{post.text[:3000]}\n\nCANDIDATES:\n{json.dumps(cand, ensure_ascii=False)}")
    return system, user


def _event_signature(event):
    """Підпис події = signature її РЕП-поста (найранішого). Підписи дедуплять
    краще за summary (вони — канонічний відбиток факту)."""
    posts = list(event.posts.all())  # prefetch-кеш; Meta ordering = posted_at
    if posts:
        return (posts[0].classification or {}).get("signature") or ""
    return ""


def _candidates(task, post):
    """Живі події ±вікно від дати поста; свій регіон — першими. Скоринг проти
    І summary, І підпису події (max) — різні видання формулюють по-різному, тож
    signature↔signature ловить дублі, які signature↔summary пропускає."""
    win = timedelta(hours=task.info_match_window_hours or 24)
    lo, hi = post.posted_at - win, post.posted_at + win
    events = list(Event.objects.filter(
        task=task, last_post_at__isnull=False,
        last_post_at__gte=lo, last_post_at__lte=hi).prefetch_related("posts"))
    if post.region_subject_id:
        events.sort(key=lambda e: (e.region_subject_id != post.region_subject_id))
    sig = (post.classification or {}).get("signature") or post.title or post.text[:200]

    def score(e):
        return max(token_set_ratio(sig, e.summary or ""),
                   token_set_ratio(sig, _event_signature(e)))
    scored = sorted(((score(e), e) for e in events), key=lambda t: t[0], reverse=True)
    return [e for s, e in scored if s >= FUZZY_FLOOR][:JUDGE_TOPK]


def info_event_once(task):
    """Обробити ОДИН info_screened-пост під advisory-локом задачі (щоб два
    воркери не створили дубль-подію з двох постів про той самий факт)."""
    ids = _claim_posts(task, Post.STAGE_INFO_SCREENED, 1)
    if not ids:
        return False
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", [task.id, 0])
        post = Post.objects.select_related("region_subject").get(id=ids[0])
        if post.posted_at is None:
            post.posted_at = djtz.now()
        cands = _candidates(task, post)
        verdict = None
        if cands:
            system, user = _judge_prompt(task, post, cands)
            raw = asyncio.run(llm.query(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                model=(task.llm_model or None), json_mode=True, max_tokens=800))
            verdict = llm.extract_json(raw)

        ev = None
        if isinstance(verdict, dict) and verdict.get("verdict") == "attach":
            ev = next((e for e in cands if e.id == verdict.get("event_id")), None)
        if ev is not None:
            _attach_posts(ev, [post])  # -> STAGE_DONE, перерахунок count/reach
            if (verdict.get("update_summary") and task.info_update_summaries
                    and verdict.get("new_summary")):
                ev.summary = verdict["new_summary"].strip()
            ev.last_post_at = max(ev.last_post_at or post.posted_at, post.posted_at)
            ev.save(update_fields=["summary", "last_post_at"])
            logger.info("info_event[%s]: attach post#%d → event#%d", task.slug, post.id, ev.id)
        else:
            ev = _create_event(task, [post])   # -> STAGE_DONE всередині
            # авто-аудит (опційно): review_enabled → подія чекає review-воркера
            # (pending); інакше — одразу approved (дефолт infospace, свіжість)
            ev.review_status = (Event.REVIEW_PENDING if task.review_enabled
                                else Event.REVIEW_APPROVED)
            ev.last_post_at = post.posted_at
            ev.save(update_fields=["review_status", "last_post_at"])
            logger.info("info_event[%s]: new event#%d (post#%d, %s)",
                        task.slug, ev.id, post.id, ev.review_status)
    return True


# =========================================================================== retention

def info_retention_once(task):
    """Пер-задачна чистка: нерелевантні done-пости старші за N діб, БЕЗ події."""
    days = task.info_retention_days or 2
    cutoff = djtz.now() - timedelta(days=days)
    ids = list(Post.objects.filter(
        task=task, stage=Post.STAGE_DONE, event__isnull=True,
        posted_at__lt=cutoff).exclude(is_relevant=True)
        .values_list("id", flat=True)[:RETENTION_TICK])
    if not ids:
        return False
    Post.objects.filter(id__in=ids).delete()
    logger.info("info_retention[%s]: видалено %d сирих постів (старші за %d діб)",
                task.slug, len(ids), days)
    return True


# =========================================================================== healthcheck

# Самоперевірка ловить ТИХИЙ злам скрапера («успіх без користі»), який
# consecutive_failures не бачить (бо це не виняток). Тільки web/rss — вони
# ламаються тихо (redesign/селектор); telegram-збої гучні (виняток).
HEALTHCHECK_KINDS = {"web", "rss"}
HEALTHCHECK_INTERVAL = timedelta(hours=24)
MIN_ARTICLE_CHARS = 80


def evaluate_quality(source, items):
    """Канарки «схоже на робоче» (kind-залежні):
    - будь-який kind: 0 елементів → злам (discovery/лістинг/стрічка порожні);
    - web: extraction МУСИТЬ давати тіло статті — усі порожні/тонкі = зламано
      шаблон/селектор;
    - rss/інші: заголовкові стрічки дають короткий text НОРМАЛЬНО — вимагаємо
      лише хоч якийсь непорожній контент (title або text)."""
    if not items:
        return False, "0 елементів (discovery/лістинг/стрічка порожні — злам?)"
    n = len(items)
    if source.kind == Source.KIND_WEB:
        thin = sum(1 for it in items if len((it.text or "").strip()) < MIN_ARTICLE_CHARS)
        if thin == n:
            return False, f"усі {n} без тіла (<{MIN_ARTICLE_CHARS} симв.) — extraction зламано?"
    else:
        empty = sum(1 for it in items
                    if not (it.title or "").strip() and not (it.text or "").strip())
        if empty == n:
            return False, f"усі {n} елементів без контенту (title і text порожні)"
    return True, ""


def _claim_healthcheck_source():
    now = djtz.now()
    due = now - HEALTHCHECK_INTERVAL
    have_subs = SourceSubscription.objects.filter(
        is_active=True, task__is_active=True,
        task__pipeline="infospace").values("source_id")
    with transaction.atomic():
        sid = (
            Source.objects.filter(is_active=True, kind__in=HEALTHCHECK_KINDS)
            .filter(Q(last_healthcheck_at__isnull=True) | Q(last_healthcheck_at__lt=due))
            .filter(id__in=have_subs)
            .order_by("last_healthcheck_at", "id")
            .select_for_update(skip_locked=True)
            .values_list("id", flat=True)
            .first()
        )
        if sid is None:
            return None
        # застовпити одразу (marker) — щоб повільна перевірка не пере-клеймилась
        Source.objects.filter(id=sid).update(last_healthcheck_at=now)
    return Source.objects.get(id=sid)


class _DetachedProbe:
    """Відчеплена копія джерела з ПОРОЖНІМ poll_cursor — для dry-run/healthcheck.
    Адаптер мутує poll_cursor цієї копії; реальний Source (і його polling-watermark)
    структурно недоторканий (не покладаємось на restore/update_fields)."""
    def __init__(self, src):
        for a in ("kind", "url", "config", "scraper_key", "region_subject", "tg_account"):
            setattr(self, a, getattr(src, a))
        self.poll_cursor = {}


def probe_fetch(source):
    """Dry-run: fetch на відчепленій копії (форсований backfill), реальний
    watermark недоторканий. Використовують healthcheck + адмін-дії."""
    return get_adapter(source.kind).fetch(_DetachedProbe(source))


def info_healthcheck_once():
    """TASKLESS: раз на добу dry-run по web/rss-джерелу → канарки → health.
    НЕ чіпає реальний watermark (fetch на відчепленій копії — див. probe_fetch)."""
    source = _claim_healthcheck_source()
    if source is None:
        return False
    try:
        items = probe_fetch(source)
        ok, note = evaluate_quality(source, items)
    except RateLimited:
        return True     # ліміт — не якісна проблема, пропускаємо прохід
    except Exception as e:  # noqa: BLE001
        ok, note = False, f"dry-run виняток {type(e).__name__}: {e}"
    source.quality_ok = ok
    source.quality_note = note[:200]
    source.save(update_fields=["quality_ok", "quality_note"])
    logger.info("info_healthcheck: %s (%s) → якість=%s %s",
                source.name, source.kind, ok, note)
    return True


STAGE_RUNNERS = {
    "info_collect": info_collect_once,       # taskless
    "info_screen": info_screen_once,
    "info_event": info_event_once,
    "info_retention": info_retention_once,
    "info_healthcheck": info_healthcheck_once,  # taskless
}
