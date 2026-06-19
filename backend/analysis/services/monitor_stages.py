"""
Stage-machine для opinion-monitor конвеєра (pipeline="monitor").

Той самий патерн, що stages.py для подій: Post.stage + claim через
select_for_update(skip_locked), кожен етап — функція mon_xxx_once(task),
воркер run_worker --stage mon_xxx крутить її в циклі.

  mon_collect    CollectChunk -> Post(stage=mon_collected)   (whitelist чатів MonitorChat)
  mon_filter     mon_collected -> mon_filtered | done         (regex/довжина; дешево, без LLM)
  mon_prescreen  mon_filtered  -> mon_prescreened | done      (LLM compact yes/no, ~4% positive)
  mon_tag        mon_prescreened -> done                      (LLM tagger -> Post.tags + is_relevant)

Надійність, яку дає ця механіка проти ручних команд:
  * пост без LLM-вердикту (обрізаний батч) просто лишається на своїй стадії —
    наступний тік перезахопить і дожене (раніше це був ручний mop-up);
  * після MAX_STAGE_ATTEMPTS невдач пост іде в STAGE_FAILED зі stage_error
    (poison-pill не блокує чергу);
  * мертвий воркер -> stale lock -> перезахоплення через LOCK_TIMEOUT;
  * 500-ки TeleZip на широких вікнах -> _split_chunk дробить чанк на 1-денні.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, time as dtime, timedelta, timezone

from django.conf import settings
from django.db import transaction
from django.db.models import F as DBF, Q
from django.utils import timezone as djtz

from analysis.models import MonitorChat, Post, Tag
from analysis.pilot import filters as PF
from analysis.pilot.prompts import (
    PRESCREEN_SYSTEM_PROMPT_COMPACT,
    TAGGER_SYSTEM_PROMPT,
    build_user_prompt,
)
from . import llm
from .stages import (
    LOCK_TIMEOUT,
    _claim_chunk,
    _is_transient_error,
    _maybe_finish_job,
    _split_chunk,
)
from .telezip import TelezipClient

logger = logging.getLogger(__name__)

# Маркери «хост НЕДОСЯЖНИЙ» (мережа/VPN/DNS лежить) — підмножина transient, але
# на відміну від 500/timeout це НЕ вина дня: інфра тимчасово відпала. Такі чанки
# не списуємо в failed ніколи — лишаємо pending з довгим backoff (див. _chunk_failure).
_CONNECTION_MARKERS = (
    "Cannot connect to host", "Connection reset", "Connection refused",
    "ServerDisconnected", "ClientConnectorError", "ClientOSError",
    "Name or service not known", "Temporary failure in name",
)


def _is_connection_error(e: Exception) -> bool:
    msg = f"{type(e).__name__}: {e}"
    return any(m in msg for m in _CONNECTION_MARKERS)


# --- tick sizes / limits ----------------------------------------------------
FILTER_BATCH = 1000            # regex-фільтр дешевий, можна великими пачками
PRESCREEN_TICK = 600           # постів за тік prescreen-воркера
PRESCREEN_SUB = 50             # постів на один LLM-виклик (50 — стеля Gemini Flash)
PRESCREEN_CONCURRENCY = 12     # паралельних LLM-викликів усередині тіка
TAG_TICK = 100
TAG_SUB = 10                   # 25 давав битий/обрізаний JSON на Gemini Flash →
                               # строгий контракт довжини відкидав увесь батч.
                               # 10 → стабільний повний JSON + менший blast-radius
                               # (одна «отруйна» пачка валить 10, а не 25).
TAG_CONCURRENCY = 6
MAX_STAGE_ATTEMPTS = 6         # після стількох claim'ів без вердикту -> failed
MAX_COLLECT_ATTEMPTS = 8       # cap на ТРАНЗІЄНТНІ ретраї збору: «отруйний» день
                               # (важкий запит / TeleZip давиться однією датою) що
                               # вічно 500-ить, не має блокувати чергу — після
                               # стількох спроб лишаємо failed і йдемо далі.
MIN_LEN, MAX_LEN = 25, 600     # довжини як у monitor_filter

VALID_TAG_CATEGORIES = {"criticism_target", "topic", "opinion"}


# --------------------------------------------------------------------------- shared helpers

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:32]


def _parse_dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:  # noqa: BLE001
        return None


def _parse_object(text: str):
    """JSON-ОБ'ЄКТ з відповіді моделі (толерує ``` fences). Не llm.extract_json —
    той хапає перший `[...]`, і {"positive": []} перетворюється на порожній list."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b != -1 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except Exception:  # noqa: BLE001
            return None
    return None


def _region_of(task) -> str:
    """Регіональний ключ для pilot.filters та контексту tagger'а.
    Конвенція: slug починається з регіону ('dagestan-criticism-monitor')."""
    return (task.slug or "").split("-")[0]


def _claim(task, stage, limit):
    """Claim постів стадії (як stages._claim_posts) + інкремент stage_attempts,
    щоб зациклений пост зрештою пішов у failed, а не крутився вічно."""
    cutoff = djtz.now() - LOCK_TIMEOUT
    with transaction.atomic():
        ids = list(
            Post.objects.filter(task=task, stage=stage)
            .filter(Q(stage_locked_at__isnull=True) | Q(stage_locked_at__lt=cutoff))
            .order_by("posted_at", "id")
            .select_for_update(skip_locked=True)
            .values_list("id", flat=True)[:limit]
        )
        if ids:
            Post.objects.filter(id__in=ids).update(
                stage_locked_at=djtz.now(),
                stage_attempts=DBF("stage_attempts") + 1)
    return ids


def _release_for_retry(ids, stage_label):
    """Пости без вердикту: зняти lock (наступний тік перезахопить); хто вже
    вичерпав MAX_STAGE_ATTEMPTS — у failed, щоб не блокувати чергу вічно."""
    if not ids:
        return
    Post.objects.filter(id__in=ids, stage_attempts__gte=MAX_STAGE_ATTEMPTS).update(
        stage=Post.STAGE_FAILED, stage_locked_at=None,
        stage_error=f"{stage_label}: no LLM verdict after {MAX_STAGE_ATTEMPTS} attempts")
    Post.objects.filter(id__in=ids).exclude(stage=Post.STAGE_FAILED).update(
        stage_locked_at=None)


# --------------------------------------------------------------------------- mon_collect

async def _fetch_chunk(task, dfrom, dto, channel_ids):
    """ПО ОДНОМУ чату. telezip_query — це чиста-негація `-(##cluster)`, яка
    змушує TeleZip сканувати весь корпус; над БАГАТЬМА каналами одразу вона
    стабільно 429/timeout-ить (виміряно: 1 чат OK 654 msgs, 5 чатів — FAIL).
    Один канал за раз тримає кожен запит легким. Семафор клієнта (=1) і так
    серіалізує виклики, тож сумарне навантаження на TeleZip не зростає."""
    q = task.telezip_query or ""
    langs = task.languages or ["ru"]
    # Короткий timeout: важкий day-запит TeleZip обробляє ~1.5-2хв перш ніж
    # віддати 500. Кидаємо його через 60с (-> TimeoutError -> overload), щоб
    # find_posts_range одразу дробив, а не чекав серверну 500. Легітимні 4-6год
    # вікна повертаються за секунди, тож їх це не зачіпає.
    timeout = int(getattr(settings, "TELEZIP_TIMEOUT", 120) or 120)
    async with TelezipClient(settings.TELEZIP_API_KEY,
                             settings.TELEZIP_BASE_URL, timeout=timeout) as tz:
        # Розпаралелюємо по чатах: клієнтський семафор _gate (=TELEZIP_MAX_
        # CONCURRENCY) сам тримає ≤N одночасних. При =2 збір іде у 2 потоки
        # (~2× швидше), кожен per-chat запит лишається легким.
        async def fetch_one(cid):
            # Adaptive range-query: повне вікно за раз, а на 500/timeout (важкий
            # чат×день перевищує ~6год search-budget TeleZip) воно само ділиться
            # навпіл →4→8… Легкі дні = 1 запит; важкі — рівно стільки дроблення,
            # скільки треба (мінімум зайвих запитів → менше 429).
            # min_window=1h: дні-монстри (важка негація × завантажений день) навіть
            # за 3год не вкладались у 2-хв бюджет TeleZip і таймаутили назавжди.
            # Дроблення зупиняється на span < 2×min_window, тож 1h дозволяє йти аж
            # до ~1-2год шматків. Якщо й це тайматиме — знизити до 30хв.
            return await tz.find_posts_range(q, dfrom, dto, langs, unique=True,
                                             channel_ids=[cid],
                                             min_window=timedelta(hours=1))
        results = await asyncio.gather(*[fetch_one(c) for c in channel_ids])
    out, seen = [], set()
    for batch in results:
        for r in batch:
            u = r.get("message_url")
            if u and u in seen:
                continue
            if u:
                seen.add(u)
            out.append(r)
    return out


def _chunk_failure(chunk, e):
    """Та сама політика, що в stages.collect_once: мультиденний чанк дробимо,
    1-денний — транзієнтні помилки ретраїмо з backoff, постійні -> failed.

    УВАГА: TeleZip 500/timeout/429 — це НЕ завжди їхній даун. Часто це наш
    надважкий запит: telezip_query = чиста-негація `-(##cluster)`, яка над
    багатьма каналами одразу змушує бек сканувати весь корпус і падати.
    Виміряно: cluster×1чат OK, cluster×5чатів FAIL. Тому _fetch_chunk збирає
    ПО ОДНОМУ чату. Якщо 500/timeout повертаються попри це — спершу перевіряй
    вагу запиту (звузь вікно / спрости query), а не лише чи TeleZip живий;
    вічний retry на надважкому запиті ніколи не зійдеться."""
    logger.warning("mon_collect %s..%s failed: %s", chunk.date_from, chunk.date_to, e)
    if chunk.date_to > chunk.date_from:
        n = _split_chunk(chunk)
        logger.info("mon_collect: split chunk into %d daily chunks", n)
        return
    transient = _is_transient_error(e)
    connection = _is_connection_error(e)
    chunk.locked_at = None
    chunk.error = str(e)[:1000]
    if connection:
        # Хост НЕДОСЯЖНИЙ (VPN/мережа лежить) — це НЕ «отруйний» день, а тимчасова
        # інфра. День у цьому не винен, тому НІКОЛИ не списуємо його в failed:
        # тримаємо pending з довгим backoff (5 хв). _claim_chunk поважає next_retry_at,
        # тож воркер пропускає цей чанк і пробує інші (досяжні), а коли лінк
        # повертається — збір сам докачує діру без ручних скидань.
        chunk.status = "pending"
        chunk.next_retry_at = djtz.now() + timedelta(seconds=300)
        chunk.save(update_fields=["status", "locked_at", "error", "next_retry_at"])
        logger.info("mon_collect %s: лінк недосяжний, лишаю pending, retry за 300с (attempt %d)",
                    chunk.date_from, chunk.attempts)
    elif transient and chunk.attempts >= MAX_COLLECT_ATTEMPTS:
        # Вічний retry на «отруйному» дні (важкий запит -> 500/timeout) ніколи не
        # зійдеться і блокує чергу (claim бере найранішу дату першою). Здаємось і
        # пускаємо інші чанки. Connection-помилки сюди НЕ доходять (гілка вище).
        chunk.status = "failed"
        chunk.save(update_fields=["status", "locked_at", "error"])
        logger.warning("mon_collect %s: giving up after %d transient attempts -> failed",
                       chunk.date_from, chunk.attempts)
        _maybe_finish_job(chunk.job)
    elif transient:
        delay = (15, 30, 60)[min(chunk.attempts - 1, 2)]
        chunk.status = "pending"
        chunk.next_retry_at = djtz.now() + timedelta(seconds=delay)
        chunk.save(update_fields=["status", "locked_at", "error", "next_retry_at"])
        logger.info("mon_collect %s: transient, retry in %ds (attempt %d)",
                    chunk.date_from, delay, chunk.attempts)
    else:
        chunk.status = "failed" if chunk.attempts >= 4 else "pending"
        chunk.save(update_fields=["status", "locked_at", "error"])
        if chunk.status == "failed":
            _maybe_finish_job(chunk.job)


def mon_collect_once(task):
    """Обробити ОДИН pending CollectChunk задачі-монітора. True якщо була робота."""
    chunk = _claim_chunk(task)
    if not chunk:
        return False

    enrolled = (MonitorChat.objects.filter(task=task, is_active=True)
                .select_related("channel"))
    chat_by_id = {m.channel.tg_id: m.channel for m in enrolled if m.channel.tg_id}
    if not chat_by_id:
        chunk.status = "failed"
        chunk.error = "no active MonitorChat rows for task"
        chunk.locked_at = None
        chunk.save(update_fields=["status", "error", "locked_at"])
        logger.error("mon_collect %s: no active MonitorChat", task.slug)
        return True

    dfrom = datetime.combine(chunk.date_from, dtime.min, tzinfo=timezone.utc)
    dto = datetime.combine(chunk.date_to, dtime.max, tzinfo=timezone.utc)
    try:
        rows = asyncio.run(_fetch_chunk(task, dfrom, dto, list(chat_by_id)))
    except Exception as e:  # noqa: BLE001
        _chunk_failure(chunk, e)
        return True

    existing_urls = set(Post.objects.filter(task=task).values_list("url", flat=True))
    to_create = []
    for r in rows:
        text = (r.get("content") or "").strip()
        url = r.get("message_url") or ""
        ch = chat_by_id.get(r.get("channel_id"))
        if not text or not url or not ch or url in existing_urls:
            continue
        existing_urls.add(url)
        to_create.append(Post(
            task=task, url=url, stage=Post.STAGE_MON_COLLECTED,
            channel=ch, channel_name=ch.username or f"id{ch.tg_id}",
            posted_at=_parse_dt(r.get("date")),
            telezip_date=_parse_dt(r.get("date")),
            text=text, content_hash=_content_hash(text),
            telezip_mid=r.get("mid"),
            author_name=r.get("from_user_name") or "",
            author_tg_id=r.get("from_user_id"),
            reply_to_msg=r.get("reply_to"),
            classification={"_monitor": True, "_collect_source": "mon_collect"},
        ))
    if to_create:
        # flag chat messages that merely echo a channel post (same content_hash) so they
        # are ignored in chat-activity analysis (is_channel_repost). Set pre-save.
        from analysis.services.stages import channel_post_hashes
        known = channel_post_hashes({p.content_hash for p in to_create})
        for p in to_create:
            if (p.content_hash in known and p.channel
                    and p.channel.chat_type in ("chat", "discussion")):
                p.is_channel_repost = True
        Post.objects.bulk_create(to_create, batch_size=1000, ignore_conflicts=True)

    chunk.status = "done"
    chunk.posts_collected = len(to_create)
    chunk.locked_at = None
    chunk.next_retry_at = None
    chunk.finished_at = djtz.now()
    chunk.save(update_fields=["status", "posts_collected", "locked_at",
                              "next_retry_at", "finished_at"])
    logger.info("mon_collect %s..%s: +%d posts (raw %d)",
                chunk.date_from, chunk.date_to, len(to_create), len(rows))
    _maybe_finish_job(chunk.job)
    return True


# --------------------------------------------------------------------------- mon_filter

def mon_filter_once(task):
    """Regex/довжина: kept -> mon_filtered, шум -> done (is_relevant=False)."""
    ids = _claim(task, Post.STAGE_MON_COLLECTED, FILTER_BATCH)
    if not ids:
        return False
    region = _region_of(task)
    posts = list(Post.objects.filter(id__in=ids))
    n_kept = n_drop = 0
    for p in posts:
        text = p.text or ""
        if len(text) < MIN_LEN:
            reason = ("too_short", f"len<{MIN_LEN}")
        elif len(text) >= MAX_LEN:
            reason = ("too_long", f"len≥{MAX_LEN} — likely a post, not a comment")
        else:
            reason = PF.classify(text, region)
        cl = dict(p.classification or {})
        if reason:
            cl["is_filtered"] = True
            cl["exclusion_label"] = reason[0]
            cl["exclusion_description"] = reason[1]
            p.is_relevant = False
            p.stage = Post.STAGE_DONE
            n_drop += 1
        else:
            cl["is_filtered"] = False
            p.stage = Post.STAGE_MON_FILTERED
            n_kept += 1
        p.classification = cl
        p.stage_locked_at = None
    Post.objects.bulk_update(
        posts, ["classification", "is_relevant", "stage", "stage_locked_at"],
        batch_size=500)
    logger.info("mon_filter: %d kept -> mon_filtered, %d dropped", n_kept, n_drop)
    return True


# --------------------------------------------------------------------------- mon_prescreen

async def _llm_prescreen(batches, model):
    sem = asyncio.Semaphore(PRESCREEN_CONCURRENCY)
    client = llm.make_client()
    out = {}

    async def one(batch):
        async with sem:
            user = "\n\n".join(
                f"[{i}] " + (p.text or "").strip().replace("\n", " ")[:1000]
                for i, p in enumerate(batch))
            raw = await llm.query(
                [{"role": "system", "content": PRESCREEN_SYSTEM_PROMPT_COMPACT},
                 {"role": "user", "content": user}],
                model=model, client=client, max_tokens=2500, json_mode=True)
            data = _parse_object(raw)
            if not isinstance(data, dict) or "positive" not in data:
                return  # битий батч — пости лишаться без вердикту -> retry
            pos = {}
            for e in (data.get("positive") or []):
                if isinstance(e, dict) and "i" in e:
                    try:
                        pos[int(e["i"])] = float(e.get("c") or 0.0)
                    except (TypeError, ValueError):
                        continue
            for i, p in enumerate(batch):
                out[p.id] = {"could_be_criticism": i in pos,
                             "confidence": pos.get(i, 0.0)}

    try:
        await asyncio.gather(*[one(b) for b in batches])
    finally:
        await client.close()
    return out


def mon_prescreen_once(task):
    """LLM compact yes/no: positive -> mon_prescreened, negative -> done."""
    ids = _claim(task, Post.STAGE_MON_FILTERED, PRESCREEN_TICK)
    if not ids:
        return False
    model = task.llm_model or settings.LLM_MODEL
    posts = list(Post.objects.filter(id__in=ids).order_by("posted_at", "id"))
    batches = [posts[i:i + PRESCREEN_SUB] for i in range(0, len(posts), PRESCREEN_SUB)]
    verdicts = asyncio.run(_llm_prescreen(batches, model))

    decided, missing = [], []
    n_pos = 0
    for p in posts:
        v = verdicts.get(p.id)
        if v is None:
            missing.append(p.id)
            continue
        cl = dict(p.classification or {})
        cl["_prescreen"] = {**v, "_model": model, "_mode": "compact"}
        p.classification = cl
        p.stage_locked_at = None
        if v["could_be_criticism"]:
            p.stage = Post.STAGE_MON_PRESCREENED
            n_pos += 1
        else:
            p.stage = Post.STAGE_DONE
            p.is_relevant = False
        decided.append(p)
    Post.objects.bulk_update(
        decided, ["classification", "is_relevant", "stage", "stage_locked_at"],
        batch_size=500)
    _release_for_retry(missing, "mon_prescreen")
    logger.info("mon_prescreen: %d positive, %d negative, %d retry",
                n_pos, len(decided) - n_pos, len(missing))
    return True


# --------------------------------------------------------------------------- mon_tag

async def _llm_tag(batches, region, model):
    sem = asyncio.Semaphore(TAG_CONCURRENCY)
    client = llm.make_client()
    out = {}

    async def one(batch):
        async with sem:
            # ID-MAPPING: кожен коментар підписуємо реальним post.id і просимо
            # модель повернути його у полі "id". Тоді часткова/збита/переставлена
            # відповідь РЯТУЄТЬСЯ — маплимо вердикти за id, а не zip-ом за позицією.
            # Раніше строгий len(items)==len(batch) валив увесь батч (×10 постів
            # у failed) через один зайвий/відсутній елемент. Тепер у retry йдуть
            # ЛИШЕ пости без вердикту.
            lines = []
            for p in batch:
                chat = (p.channel.username if p.channel else "") or p.channel_name
                txt = (p.text or "").strip().replace("\n", " ")[:1500]
                lines.append(f"[id={p.id}] chat=@{chat or '-'} | region={region}\n{txt}")
            user = ("\n\n".join(lines) +
                    "\n\nУ КОЖНОМУ елементі items ОБОВ'ЯЗКОВО додай поле "
                    '"id" = число з [id=...] відповідного коментаря.')
            raw = await llm.query(
                [{"role": "system", "content": TAGGER_SYSTEM_PROMPT},
                 {"role": "user", "content": user}],
                model=model, client=client, max_tokens=4000, json_mode=True)
            data = _parse_object(raw)
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return  # зовсім не розпарсилось -> весь батч у retry
            ids = {p.id for p in batch}
            for v in items:
                if isinstance(v, dict) and v.get("id") in ids:
                    out[v["id"]] = v
            # пости без вердикту (id не повернувся) самі впадуть у retry через
            # _release_for_retry у викликаючому mon_tag_once

    try:
        await asyncio.gather(*[one(b) for b in batches])
    finally:
        await client.close()
    return out


def _get_tag(cache, category, name):
    """ЄДИНИЙ шлях створення/пошуку тега — services.tags.resolve. Для closed-
    категорій (criticism_target і т.д.) невідомий варіант повертає None і тег
    ПРОСТО НЕ ДОДАЄТЬСЯ — раніше сліпий get_or_create наплодив ~450 варіантів
    criticism_target замість 18 канонічних."""
    from analysis.services import tags as tag_service
    name = (name or "").strip()
    if not name or category not in VALID_TAG_CATEGORIES:
        return None
    key = (category, name)
    if key not in cache:
        cache[key] = tag_service.resolve(category, name)
    return cache[key]


def mon_tag_once(task):
    """Повний tagger: Post.tags + classification.opinion + is_relevant -> done."""
    ids = _claim(task, Post.STAGE_MON_PRESCREENED, TAG_TICK)
    if not ids:
        return False
    model = task.llm_model or settings.LLM_MODEL
    region = _region_of(task)
    posts = list(Post.objects.filter(id__in=ids)
                 .select_related("channel").order_by("posted_at", "id"))
    batches = [posts[i:i + TAG_SUB] for i in range(0, len(posts), TAG_SUB)]
    verdicts = asyncio.run(_llm_tag(batches, region, model))

    done, missing = [], []
    n_rel = 0
    tag_cache = {}
    for p in posts:
        v = verdicts.get(p.id)
        if not v:
            missing.append(p.id)
            continue
        attach = []
        for cat in VALID_TAG_CATEGORIES:
            for name in (v.get(cat) or []):
                t = _get_tag(tag_cache, cat, name)
                if t:
                    attach.append(t)
        for pt in (v.get("proposed_tags") or []):
            if isinstance(pt, dict):
                t = _get_tag(tag_cache, pt.get("category"), pt.get("name"))
                if t:
                    attach.append(t)
        if attach:
            p.tags.add(*attach)
        cl = dict(p.classification or {})
        cl["opinion"] = {**v, "_model": model}
        p.classification = cl
        p.is_classified = True
        p.is_relevant = bool(v.get("criticism_target"))
        n_rel += int(p.is_relevant)
        p.stage = Post.STAGE_DONE
        p.stage_locked_at = None
        done.append(p)
    Post.objects.bulk_update(
        done, ["classification", "is_classified", "is_relevant",
               "stage", "stage_locked_at"], batch_size=200)
    _release_for_retry(missing, "mon_tag")
    logger.info("mon_tag: %d tagged (%d relevant), %d retry",
                len(done), n_rel, len(missing))
    return True


# --------------------------------------------------------------------------- registry

STAGE_RUNNERS = {
    "mon_collect": mon_collect_once,
    "mon_filter": mon_filter_once,
    "mon_prescreen": mon_prescreen_once,
    "mon_tag": mon_tag_once,
}
