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
import re
import time
from datetime import datetime, timedelta, timezone

from asgiref.sync import sync_to_async
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


def _assign_accounts(chats, skip_ids=()):
    """Чат читає ТОЙ акаунт, що вже його читав: резолв кешується в сесії, і читати
    іншим акаунтом означає платити резолв удруге (а він має добовий ліміт).

    skip_ids — акаунти в паузі після FloodWait: їм не даємо НОВИХ чатів, а вже
    прив'язані читаємо (їхня черга все одно відсунеться самим FloodWait-ом).
    """
    pool = list(TelegramAccount.objects.filter(is_authenticated=True, is_active=True)
                .exclude(session_string="").select_related("proxy").order_by("id"))
    if not pool:
        return None, None
    # Спершу акаунти, які @SpamBot підтвердив як ВІЛЬНІ: резолв нового юзернейма
    # обмеженому акаунту не дається («No user has X as username»), і чат виглядає
    # неіснуючим. На проді 18.09 так «зникали» живі чати.
    free = [a for a in pool if a.id not in skip_ids] or pool
    free.sort(key=lambda a: (a.spam_status != "free", a.id))
    # Той самий чат може читатись в ІНШІЙ задачі вже призначеним акаунтом — у
    # його сесії резолв юзернейма закешований. Беремо саме його, інакше платимо
    # резолв удруге (а обмеженому акаунту він просто не дається).
    ids = [mc.channel_id for mc in chats]
    donor = {}
    for other in (MonitorChat.objects.filter(channel_id__in=ids)
                  .exclude(tg_account=None).exclude(id__in=[mc.id for mc in chats])
                  .select_related("tg_account", "tg_account__proxy")
                  .order_by("channel_id", "id")):
        if other.tg_account.is_authenticated and other.tg_account.is_active:
            donor.setdefault(other.channel_id, other.tg_account)

    by_acc: dict[int, list] = {}
    for i, mc in enumerate(chats):
        acc = mc.tg_account
        if acc is None or not acc.is_authenticated:
            acc = donor.get(mc.channel_id) or free[i % len(free)]
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


# ===========================================================================
# СТРІМ: полінг чату + регулярка (альтернатива пошуку за словами)
#
# Пошук питає індекс Telegram «де слово X» — дешево по запитах, але бачить лише
# те, що індекс уміє, і має стелю влучень на слово в жвавому чаті. Стрім читає
# чат суцільно і фільтрує регулярками НА НАШОМУ БОЦІ: повнота повна, затримка —
# хвилини, патерни безкоштовні (їх може бути хоч сотня). Ціна — один запит на
# чат на кожен полінг, тому вирішує не частота, а скільки чатів на одному
# акаунті: тримай 1 чат = 1 акаунт.
# ===========================================================================
STREAM_CONCURRENCY = 40     # скільки акаунтів читають одночасно
# Стеля на акаунт за прохід. Без неї один акаунт із мертвим проксі тримав увесь
# прохід: connect висів на ретраях, а стрім із інтервалом 3 хв не встигав
# зробити жодного повного кола за 12 хвилин (ловили на проді).
STREAM_ACCOUNT_TIMEOUT = 75
STREAM_LIMIT = 300          # стеля повідомлень за один полінг чату
STREAM_BACKFILL = 100       # перший полінг: скільки останніх забрати
MEDIA_PER_TICK = 30         # стеля пересилань медіа з одного чату за прохід
MEDIA_PAUSE = 0.5

# Акаунти в паузі після FloodWait: {id: коли звільниться}. Живе в пам'яті
# воркера — стрім-воркер один, а після рестарту пауза все одно відновиться з
# першого ж FloodWait (вони приходять миттєво, не після роботи).
_FLOOD_UNTIL: dict[int, float] = {}


def _flooded_ids() -> set:
    now = time.time()
    return {aid for aid, until in _FLOOD_UNTIL.items() if until > now}


async def _mark_flood(acc_id, seconds, chats):
    """Акаунт у паузу, його чати — іншим (інакше чат мовчить весь FloodWait)."""
    _FLOOD_UNTIL[acc_id] = time.time() + max(int(seconds or 60), 60)
    logger.warning("tgs_stream: акаунт #%s FloodWait %ss — пауза, %d чатів "
                   "віддаю іншим", acc_id, seconds, len(chats))
    await sync_to_async(MonitorChat.objects.filter(
        id__in=[mc.id for mc in chats]).update)(tg_account=None)


def _media_meta(m, mc):
    """Позначка про медіа повідомлення: {kind, chat, mid} або None.

    Файл НЕ качаємо. Публікація перешле оригінал акаунтом — це копія на боці
    Telegram: нуль трафіку через нас, цілий альбом, збережена атрибуція.
    Спроба качати сама собою ще й ненадійна: медіа живе в іншому DC, і через
    проксі акаунта download_media падав на InvalidBufferError(404).
    """
    kind = ("photo" if getattr(m, "photo", None)
            else "video" if getattr(m, "video", None) else None)
    if not kind:
        return None
    return {"kind": kind, "chat": (mc.channel.username or "").strip(),
            "mid": int(m.id), "group": getattr(m, "grouped_id", None)}


def _is_resolve_error(err: str) -> bool:
    low = (err or "").lower()
    return ("no user has" in low or "usernameinvalid" in low
            or "cannot find any entity" in low or "usernamenotoccupied" in low)


def _patterns(task):
    """Скомпільовані патерни задачі. Порожньо = стрім вимкнено (свідомо: інакше
    пустий список матчив би все і вилив би весь потік чатів у LLM)."""
    out = []
    for line in (task.stream_regex or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(re.compile(line, re.IGNORECASE))
        except re.error as e:
            logger.error("tgs_stream: битий патерн %r (%s) — пропущено", line, e)
    return out


def _due_stream_chats(task, limit):
    cutoff = dj_tz.now() - timedelta(minutes=task.stream_interval_min or 3)
    return list(MonitorChat.objects
                .filter(task=task, is_active=True, stream_enabled=True)
                .filter(Q(last_streamed_at__isnull=True) | Q(last_streamed_at__lt=cutoff))
                .select_related("channel", "tg_account", "tg_account__proxy")
                .order_by("priority", "id")[:limit])


async def _resolve_linked(client, channel):
    """`linked:<батьківський канал>` -> сутність групи обговорення.

    Такі групи не мають ні юзернейма, ні access_hash, тож за голим tg_id
    Telethon їх не бере. Але батьківський канал названий у самому полі, і через
    нього Telegram сам віддає linked_chat — access_hash кешуємо в raw_meta,
    щоб наступні проходи не платили резолв удруге.
    """
    from telethon.tl.functions.channels import GetFullChannelRequest
    parent = (channel.username or "").split(":", 1)[1].strip()
    if not parent:
        return None
    full = await client(GetFullChannelRequest(parent))
    linked_id = getattr(full.full_chat, "linked_chat_id", None)
    chat = next((c for c in full.chats if c.id == linked_id), None)
    if chat is None:
        return None
    meta = dict(channel.raw_meta or {})
    meta["access_hash"] = chat.access_hash
    await sync_to_async(type(channel).objects.filter(id=channel.id).update)(
        tg_id=chat.id, raw_meta=meta)
    return chat


async def _stream_chat(client, mc, patterns, media_peer):
    """-> (список збігів, новий watermark, скільки медіа переслано, помилка)."""
    entity = _entity(mc.channel)
    if entity is None and (mc.channel.username or "").startswith("linked:"):
        try:
            entity = await _resolve_linked(client, mc.channel)
        except FloodWaitError:
            raise
        except Exception as e:  # noqa: BLE001
            return None, mc.stream_last_msg_id, 0, f"linked: {type(e).__name__}: {str(e)[:70]}", 0
    if entity is None:
        return None, mc.stream_last_msg_id, 0, "немає юзернейма й access_hash", 0
    first = not mc.stream_last_msg_id
    # перший полінг — найновіші N; далі — від watermark уперед (reverse), щоб
    # сплеск, більший за ліміт, не лишив діри в середині
    kwargs = dict(limit=STREAM_BACKFILL) if first else dict(
        min_id=mc.stream_last_msg_id, limit=STREAM_LIMIT, reverse=True)
    found, max_id, n_media, n_seen = [], mc.stream_last_msg_id, 0, 0
    try:
        async for m in client.iter_messages(entity, **kwargs):
            max_id = max(max_id, int(m.id))
            if media_peer is not None and n_media < MEDIA_PER_TICK and (
                    getattr(m, "photo", None) or getattr(m, "video", None)):
                try:
                    await client.forward_messages(media_peer, m.id, entity)
                    n_media += 1
                    await asyncio.sleep(MEDIA_PAUSE)
                except FloodWaitError:
                    raise
                except Exception as e:  # noqa: BLE001 — медіа не має валити збір тексту
                    logger.warning("tgs_stream: медіа з @%s не переслалось: %r",
                                   mc.channel.username, e)
            text = (getattr(m, "message", None) or "").strip()
            if not text:
                continue
            n_seen += 1
            hit = next((p.pattern for p in patterns if p.search(text)), None)
            if not hit:
                continue
            # Медіа качаємо ТУТ, поки клієнт відкритий і повідомлення в руках:
            # на етапі публікації бот до чужого чату доступу не має, а
            # перевідкривати сесію заради одного файлу дорожче за сам файл.
            media = _media_meta(m, mc)
            found.append({
                "mid": int(m.id), "text": text,
                "date": m.date.astimezone(timezone.utc) if m.date else None,
                "author_id": getattr(getattr(m, "from_id", None), "user_id", None),
                "term": hit[:60], "media": media,
            })
    except FloodWaitError:
        raise
    except Exception as e:  # noqa: BLE001
        return None, max_id, n_media, f"{type(e).__name__}: {str(e)[:90]}", n_seen
    return found, max_id, n_media, None, n_seen


async def _stream_account(acc, chats, patterns, media_chat_id, out):
    # Ретраї тут навмисно скупіші за пошукові: стрім ходить кожні 3 хвилини, тож
    # мертвий акаунт вигідніше пропустити зараз і віддати його чати живому, ніж
    # чекати на ньому весь бюджет проходу.
    client = TelegramClient(
        StringSession(acc.session_string), int(acc.api_id), acc.api_hash,
        proxy=acc.proxy.to_telethon_proxy() if acc.proxy else None,
        connection_retries=1, retry_delay=1, timeout=10, **acc.client_kwargs())
    try:
        await client.connect()
        if not await client.is_user_authorized():
            # Сесія протухла. Якщо просто вийти, чати цього акаунта лишаться
            # прив'язаними до нього і НІКОЛИ не прочитаються (тиха німота).
            # Тому відв'язуємо — наступний прохід роздасть їх живим акаунтам.
            logger.warning("tgs_stream: акаунт #%s не авторизований — відв'язую %d чатів",
                           acc.id, len(chats))
            await sync_to_async(MonitorChat.objects.filter(
                id__in=[mc.id for mc in chats]).update)(tg_account=None)
            return
        media_peer = None
        if media_chat_id and any(mc.forward_media for mc in chats):
            try:
                media_peer = await client.get_entity(int(media_chat_id))
            except Exception as e:  # noqa: BLE001
                logger.warning("tgs_stream: акаунт #%s не бачить чат медіа %s (%r) — "
                               "він має бути учасником", acc.id, media_chat_id, e)
        for mc in chats:
            try:
                msgs, max_id, n_media, err, n_seen = await _stream_chat(
                    client, mc, patterns, media_peer if mc.forward_media else None)
            except FloodWaitError as e:
                # FloodWait буває на ГОДИНИ. Без паузи стадія поверталась до
                # цього ж акаунта кожні кілька секунд: його чати не оновлювали
                # last_streamed_at, тож лишались «пора читати» — і довбали
                # флуднутий акаунт замість того, щоб піти до вільного.
                await _mark_flood(acc.id, e.seconds, chats)
                return
            out.append((mc, msgs, max_id, n_media, err, n_seen))
    except FloodWaitError as e:
        await _mark_flood(acc.id, getattr(e, "seconds", 60), chats)
    except Exception as e:  # noqa: BLE001
        # Мертва сесія / битий проксі: чати відв'язуємо, інакше вони назавжди
        # лишаться за цим акаунтом і не читатимуться (та сама німота, що й при
        # протухлій авторизації).
        logger.warning("tgs_stream: акаунт #%s впав: %r — відв'язую %d чатів",
                       acc.id, e, len(chats))
        await sync_to_async(MonitorChat.objects.filter(
            id__in=[mc.id for mc in chats]).update)(tg_account=None)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _stream_all(pool, by_acc, patterns, media_chat_id, out):
    sem = asyncio.Semaphore(STREAM_CONCURRENCY)

    async def guarded(aid, chats):
        from accounts.services.telegram_client import (AccountBusy,
                                                       account_exclusive_async)
        async with sem:
            try:
                # один акаунт = один клієнт: якщо його зараз тримає збирач або
                # публікація, пропускаємо прохід. Два конекшени з тим самим
                # auth key Telegram убиває назавжди (AuthKeyDuplicated).
                async with account_exclusive_async(pool[aid]):
                    await asyncio.wait_for(
                        _stream_account(pool[aid], chats, patterns, media_chat_id, out),
                        timeout=STREAM_ACCOUNT_TIMEOUT)
            except AccountBusy:
                logger.debug("tgs_stream: акаунт #%s зайнятий — прохід пропущено", aid)
            except asyncio.TimeoutError:
                logger.warning("tgs_stream: акаунт #%s не вклався в %ss — відв'язую "
                               "%d чатів", aid, STREAM_ACCOUNT_TIMEOUT, len(chats))
                await sync_to_async(MonitorChat.objects.filter(
                    id__in=[mc.id for mc in chats]).update)(tg_account=None)

    await asyncio.gather(*(guarded(a, ch) for a, ch in by_acc.items()))


def tgs_stream_once(task) -> bool:
    """Полінг чатів задачі + регулярка. -> True якщо була робота."""
    patterns = _patterns(task)
    if not patterns:
        return False
    chats = _due_stream_chats(task, 500)
    if not chats:
        return False
    pool, by_acc = _assign_accounts(chats, skip_ids=_flooded_ids())
    if not pool:
        logger.warning("tgs_stream: немає авторизованих акаунтів")
        return False

    out: list = []
    asyncio.run(_stream_all(pool, by_acc, patterns, task.stream_media_chat_id, out))

    n_new = n_media = n_rebind = n_seen_total = 0
    for mc, msgs, max_id, media, err, seen in out:
        n_seen_total += seen
        n_media += media
        fields = ["last_streamed_at"]
        if err:
            mc.notes = f"[tgs_stream] {err}"[:500]
            fields.append("notes")
            # «No user has X as username» — це НЕ мертвий чат, а провал резолву
            # ЦИМ акаунтом: обмежені (SpamBot) акаунти не резолвлять нові
            # юзернейми. Тому відв'язуємо акаунт і даємо чату інший шанс —
            # інакше живий чат назавжди лишається «мертвим» (ловили на проді:
            # @reduktorny «не існував», хоч уже давав пости).
            if _is_resolve_error(err):
                mc.tg_account = None
                fields.append("tg_account")
                n_rebind += 1
        elif msgs:
            n_new += _store(task, mc, msgs)
        if max_id > mc.stream_last_msg_id:
            mc.stream_last_msg_id = max_id
            fields.append("stream_last_msg_id")
        mc.last_streamed_at = dj_tz.now()
        mc.save(update_fields=fields)
    # n_seen — скільки текстових повідомлень взагалі проглянуто: без нього
    # «збігів 2» не відрізнити від «чати мовчать» і «регулярка вузька».
    logger.info("tgs_stream: чатів %d, прочитано %d, збігів %d, медіа %d, перепризначено %d",
                len(out), n_seen_total, n_new, n_media, n_rebind)
    return True


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
            media=m.get("media"),
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
        # Перетегування — звична операція при тюнінгу промпта, тож спершу
        # знімаємо попередні теги: `add` їх не чистив, і на пості лишався слід
        # старого вердикту (він же псував статистику тегів).
        p.tags.clear()
        if attach:
            from analysis.services import tags as tag_service
            attach = tag_service.collapse_scales(attach)
            p.tags.add(*attach)
        # Регіон чату — дефолт, але текст сильніший: повідомлення про Абакан у
        # тувинському чаті не має ставати «Тивою» (ловили на проді 18.09).
        if v.get("region"):
            from analysis.services.normalize import resolve_region
            reg, sett = resolve_region(str(v["region"]))
            if reg and reg.id != p.region_subject_id:
                p.region_subject = reg
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
               "stage_locked_at", "region_subject"], batch_size=200)
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
    "tgs_stream": tgs_stream_once,
    "tgs_screen": tgs_screen_once,
    "tgs_tag": tgs_tag_once,
}
