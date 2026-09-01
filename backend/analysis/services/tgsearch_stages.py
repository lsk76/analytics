"""Стадії конвеєра `tgsearch` — пошук у чатах через Telegram.

    канали -> пошук у Telegram (балансування по акаунтах)
           -> фільтр релевантності (ШІ API)
           -> тегування (ШІ API)
           -> Event (1 повідомлення = 1 подія, текст видно в картці події)

Дизайн і межі: docs/tgsearch-pipeline.md.

Свідомо НЕ дублюємо monitor: прескрін, тегувальник і дзеркало події беремо
звідти як є. Новий тут рівно один крок — сам пошук. Це й був сенс окремого
конвеєра: інший СПОСІБ збору, а не інша обробка.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from django.db import transaction
from django.db.models import Q
from django.utils import timezone as dj_tz
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import InputPeerChannel

from accounts.models import TelegramAccount
from analysis.models import MonitorChat, Post
from django.conf import settings

from analysis.services import llm
from analysis.services.monitor_stages import (_claim, _content_hash, _parse_object,
                                              _release_for_retry, mon_prescreen_once,
                                              sync_comment_event)

TAG_TICK, TAG_SUB, TAG_CONCURRENCY = 200, 12, 4

logger = logging.getLogger(__name__)

# Як часто має сенс перешукувати той самий чат. Пошук віддає ті самі повідомлення,
# що й учора, тож частіше — марні запити до Telegram і зайвий ризик FloodWait.
RESEARCH_EVERY = timedelta(hours=12)
PAUSE = 1.2                 # між запитами в межах одного акаунта
CHATS_PER_TICK = 40         # скільки чатів беремо за один прохід стадії
ACCOUNT_CONCURRENCY = 8


def _terms(task) -> list[str]:
    return [t.strip() for t in (task.search_terms or "").splitlines() if t.strip()]


def _due_chats(task, limit):
    """Чати, які пора обшукати, з уже призначеним акаунтом."""
    cutoff = dj_tz.now() - RESEARCH_EVERY
    return list(MonitorChat.objects
                .filter(task=task, is_active=True)
                .filter(Q(last_searched_at__isnull=True) | Q(last_searched_at__lt=cutoff))
                .select_related("channel", "tg_account", "tg_account__proxy")
                .order_by("priority", "id")[:limit])


def _assign_accounts(chats):
    """Чат читає ТОЙ акаунт, що вже його читав: резолв кешується в сесії, і читати
    іншим акаунтом означає платити резолв удруге (а він має добовий ліміт)."""
    pool = list(TelegramAccount.objects.filter(is_authenticated=True, is_active=True)
                .exclude(session_string="").select_related("proxy").order_by("id"))
    if not pool:
        return None, None
    by_acc: dict[int, list] = {}
    for i, mc in enumerate(chats):
        acc = mc.tg_account
        if acc is None or not acc.is_authenticated:
            acc = pool[i % len(pool)]
            mc.tg_account = acc
            mc.save(update_fields=["tg_account"])
        by_acc.setdefault(acc.id, []).append(mc)
    return {a.id: a for a in pool}, by_acc


def _entity(channel):
    """Публічний чат — за юзернеймом; приватна linked-група — через access_hash.
    Голий числовий id Telethon приймає за PeerUser і падає."""
    u = (channel.username or "").strip()
    if u and not u.startswith(("linked:", "+")):
        return u
    ah = (channel.raw_meta or {}).get("tg_flags", {}).get("access_hash") \
        or (channel.raw_meta or {}).get("access_hash")
    if channel.tg_id and ah:
        return InputPeerChannel(int(channel.tg_id), int(ah))
    return None


async def _search_chat(client, mc, terms, since, limit):
    """-> список знайдених повідомлень (dict). Порожньо — не знайшлось."""
    entity = _entity(mc.channel)
    if entity is None:
        return None, "немає юзернейма й access_hash"
    found = {}
    for term in terms:
        try:
            msgs = await client.get_messages(entity, search=term, limit=limit)
        except FloodWaitError:
            raise
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {str(e)[:90]}"
        for m in msgs:
            text = (getattr(m, "message", None) or "").strip()
            if not text or not m.date or m.date < since:
                continue
            found[m.id] = {
                "mid": m.id, "text": text,
                "date": m.date.astimezone(timezone.utc),
                "author_id": getattr(getattr(m, "from_id", None), "user_id", None),
                "term": term,
            }
        await asyncio.sleep(PAUSE)
    return list(found.values()), None


def _url(channel, mid):
    u = (channel.username or "").strip()
    if u and not u.startswith(("linked:", "+")):
        return f"https://t.me/{u}/{mid}"
    internal = str(abs(channel.tg_id or 0)).removeprefix("100")
    return f"https://t.me/c/{internal}/{mid}"


async def _run_account(acc, chats, terms, since, limit, out):
    client = TelegramClient(
        StringSession(acc.session_string), int(acc.api_id), acc.api_hash,
        proxy=acc.proxy.to_telethon_proxy() if acc.proxy else None,
        connection_retries=2, retry_delay=2, timeout=20, **acc.client_kwargs())
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning("tgs_search: акаунт #%s не авторизований", acc.id)
            return
        for mc in chats:
            try:
                msgs, err = await _search_chat(client, mc, terms, since, limit)
            except FloodWaitError as e:
                logger.warning("tgs_search: акаунт #%s FloodWait %ss — стоп", acc.id, e.seconds)
                return
            out.append((mc, msgs, err))
    except Exception as e:  # noqa: BLE001
        logger.warning("tgs_search: акаунт #%s впав: %r", acc.id, e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _search_all(pool, by_acc, terms, since, limit, out):
    sem = asyncio.Semaphore(ACCOUNT_CONCURRENCY)

    async def guarded(aid, chats):
        async with sem:
            await _run_account(pool[aid], chats, terms, since, limit, out)

    await asyncio.gather(*(guarded(a, ch) for a, ch in by_acc.items()))


def _contract_ok(prompt, marker, stage, what) -> bool:
    """Позичені й власні LLM-стадії мають КОНТРАКТ на формат відповіді. Якщо
    промпт задачі його не задовольняє, стадія раніше просто нічого не робила:
    ні виключення, ні попередження — нуль вердиктів і всі пости в нескінченний
    повтор. Ловили двічі за один прогін, тож перевіряємо на старті й голосно.
    """
    if not prompt:
        return True                      # порожній = дефолт із коду, він валідний
    if marker in prompt:
        return True
    logger.error(
        "%s: промпт задачі не містить %s — стадія зупинена. %s "
        "Виправ промпт в адмінці, інакше вердиктів не буде і пости зациклять "
        "у повторі.", stage, marker, what)
    return False


def tgs_search_once(task) -> bool:
    """Пошук у чатах задачі. -> True якщо була робота."""
    terms = _terms(task)
    if not terms:
        logger.info("tgs_search: у задачі %s не задано слів пошуку", task.slug)
        return False
    chats = _due_chats(task, CHATS_PER_TICK)
    if not chats:
        return False

    pool, by_acc = _assign_accounts(chats)
    if not pool:
        logger.warning("tgs_search: немає авторизованих акаунтів")
        return False

    since = datetime.now(timezone.utc) - timedelta(days=task.search_days or 7)
    out: list = []
    asyncio.run(_search_all(pool, by_acc, terms, since,
                            task.search_limit_per_term or 100, out))

    n_new = 0
    for mc, msgs, err in out:
        if err:
            mc.notes = (f"[tgs_search] {err}"[:500])
        elif msgs:
            n_new += _store(task, mc, msgs)
        mc.last_searched_at = dj_tz.now()
        mc.save(update_fields=["last_searched_at", "notes"] if err
                else ["last_searched_at"])
    logger.info("tgs_search: чатів %d, нових повідомлень %d", len(out), n_new)
    return True


@transaction.atomic
def _store(task, mc, msgs) -> int:
    """Пише знайдене як Post. unique(task, url) сам відсіює повтори між прогонами."""
    ch = mc.channel
    rows = []
    for m in msgs:
        rows.append(Post(
            task=task, stage=Post.STAGE_TGS_COLLECTED,
            url=_url(ch, m["mid"]), channel=ch,
            channel_name=ch.username or ch.title or "",
            text=m["text"], content_hash=_content_hash(m["text"]),
            posted_at=m["date"],
            region_subject_id=ch.region_subject_id,
            author_tg_id=m.get("author_id"),
            classification={"_tgs": {"term": m["term"]}},
        ))
    created = Post.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
    return len(created)


def tgs_screen_once(task) -> bool:
    """Фільтр релевантності через ШІ API.

    Свідомо перевикористовує monitor-прескрін: логіка «батч -> дешева LLM -> так/ні»
    там уже вилизана (ретраї, часткові відповіді, повернення в чергу). Відрізняється
    лише промпт, а він береться з поля задачі.
    """
    if not _contract_ok(task.prescreen_prompt, '"positive"', "tgs_screen",
                        'Очікується {"positive":[{"i":0,"c":0.9}]} — список '
                        'індексів РЕЛЕВАНТНИХ із впевненістю.'):
        return False
    return _reuse(task, Post.STAGE_TGS_COLLECTED, Post.STAGE_MON_FILTERED,
                  mon_prescreen_once, Post.STAGE_MON_PRESCREENED,
                  Post.STAGE_TGS_SCREENED)


def tgs_tag_once(task) -> bool:
    """Тегування через ШІ API + матеріалізація події (1 повідомлення = 1 подія).

    ЧОМУ ВЛАСНА СТАДІЯ, а не позичена monitor-івська (як прескрін): у mon_tag
    ЗАХАРДКОЖЕНІ і список категорій (criticism_target/topic/opinion — нашої
    border_problem там немає, теги просто відкидались би), і правило
    релевантності (is_relevant = є criticism_target — у нас подій не було б
    ніколи). Прескрін доменно-нейтральний, тому його реюз лишається.
    """
    if not _contract_ok(task.tagger_prompt, '"items"', "tgs_tag",
                        'Очікується {"items":[{"id":123,"<категорія>":["тег"]}]} '
                        "з полем id кожного повідомлення."):
        return False
    if not task.tag_categories.exists():
        logger.error("tgs_tag: у задачі не обрано жодної категорії тегів — "
                     "теги нікуди писати, стадія зупинена")
        return False
    ids = _claim(task, Post.STAGE_TGS_SCREENED, TAG_TICK)
    if not ids:
        return False
    posts = list(Post.objects.filter(id__in=ids)
                 .select_related("channel").order_by("posted_at", "id"))
    model = task.llm_model or settings.LLM_MODEL
    system = task.tagger_prompt or ""
    cats = list(task.tag_categories.values_list("key", flat=True))
    batches = [posts[i:i + TAG_SUB] for i in range(0, len(posts), TAG_SUB)]
    verdicts = asyncio.run(_llm_tag_batches(batches, model, system, cats))

    done, missing, n_rel = [], [], 0
    cache = {}
    for p in posts:
        v = verdicts.get(p.id)
        if not v:
            missing.append(p.id)
            continue
        attach = []
        for cat in cats:
            for name in (v.get(cat) or []):
                tg = _resolve_tag(cache, cat, name)
                if tg:
                    attach.append(tg)
        if attach:
            p.tags.add(*attach)
        cl = dict(p.classification or {})
        cl["border"] = {**v, "_model": model}
        p.classification = cl
        p.is_classified = True
        # релевантність = знайдено бодай одну проблему на кордоні
        p.is_relevant = bool(attach)
        n_rel += int(p.is_relevant)
        p.stage = Post.STAGE_DONE
        p.stage_locked_at = None
        done.append(p)
    Post.objects.bulk_update(
        done, ["classification", "is_classified", "is_relevant", "stage",
               "stage_locked_at"], batch_size=200)
    for p in done:
        sync_comment_event(p)          # 1 повідомлення = 1 подія, БЕЗ дедупу
    _release_for_retry(missing, "tgs_tag")
    logger.info("tgs_tag: %d протеговано (%d релевантних), %d у повтор",
                len(done), n_rel, len(missing))
    return True


def _resolve_tag(cache, category, name):
    """Єдиний шлях створення тега — services.tags.resolve: для закритої категорії
    невідомий варіант поверне None, для відкритої (наша border_problem) створить."""
    from analysis.services import tags as tag_service
    name = (name or "").strip()
    if not name or not category:
        return None
    key = (category, name)
    if key not in cache:
        cache[key] = tag_service.resolve(category, name)
    return cache[key]


async def _llm_tag_batches(batches, model, system, cats):
    """ID-mapping як у monitor: просимо повернути id кожного повідомлення, щоб
    часткова чи переставлена відповідь не валила весь батч."""
    sem = asyncio.Semaphore(TAG_CONCURRENCY)
    client = llm.make_client()
    out = {}

    async def one(batch):
        async with sem:
            lines = []
            for p in batch:
                chat = (p.channel.username if p.channel else "") or p.channel_name
                txt = (p.text or "").strip().replace("\n", " ")[:1200]
                lines.append(f"[id={p.id}] chat=@{chat or '-'}\n{txt}")
            user = ("\n\n".join(lines) +
                    "\n\nУ КОЖНОМУ елементі items ОБОВ'ЯЗКОВО поле \"id\" = число "
                    "з [id=...]. Категорії тегів: " + ", ".join(cats) +
                    '. Формат: {"items":[{"id":123,"' + (cats[0] if cats else "tags") +
                    '":["черги"],"punkt":"Бугристе"}]}')
            try:
                raw = await llm.query(
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    model=model, client=client, max_tokens=4000, json_mode=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("tgs_tag: батч впав: %r", e)
                return
            data = _parse_object(raw)
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return
            ids = {p.id for p in batch}
            for v in items:
                if isinstance(v, dict) and v.get("id") in ids:
                    out[v["id"]] = v

    try:
        await asyncio.gather(*[one(b) for b in batches])
    finally:
        await client.close()
    return out


def _reuse(task, my_stage, borrowed_stage, runner, borrowed_out, my_out) -> bool:
    """Пускає наші пости через monitor-стадію, тимчасово перейменувавши стадію.

    Чому так, а не копіпаста: у monitor-стадіях сидить уся робота з частковими
    відповідями LLM і поверненням постів у чергу. Дублювати її означало б
    завести другий екземпляр тих самих граблів.
    """
    ids = list(Post.objects.filter(task=task, stage=my_stage, stage_locked_at=None)
               .values_list("id", flat=True)[:500])
    if not ids:
        return False
    Post.objects.filter(id__in=ids).update(stage=borrowed_stage)
    try:
        runner(task)
    finally:
        # усе, що стадія просунула у свій вихід, переводимо в наш
        Post.objects.filter(id__in=ids, stage=borrowed_out).update(stage=my_out)
        # усе, що лишилось у позиченій стадії (не оброблене), повертаємо собі
        Post.objects.filter(id__in=ids, stage=borrowed_stage).update(stage=my_stage)
    return True


STAGE_RUNNERS = {
    "tgs_search": tgs_search_once,
    "tgs_screen": tgs_screen_once,
    "tgs_tag": tgs_tag_once,
}
