"""Операції над ПІДКЛЮЧЕНИМ Telethon-клієнтом. Приватний модуль gateway.

Кожна операція — `async def op(ctx, **kwargs)`; `ctx.client` — живий клієнт,
`ctx.account` — знімок рядка TelegramAccount, `ctx.save(**fields)` — записати
поля рядка. Операції НЕ підключаються/відключаються і НЕ ловлять помилок
Telegram: класифікація і перехід стану — справа `live.LiveAccount`.
Логічні невдачі (бот не відповів, токен не знайдено) — це результат
`{"ok": False, "error": ...}`, а не виняток: акаунт тут ні до чого.

Перенесено з accounts/services/telegram_client.py (там було connect на кожен
виклик) і tgsearch_stages._stream_chat (читання від watermark + медіа).
"""
from __future__ import annotations

import asyncio
import io
import logging
import random
import re
from datetime import timezone as _tz

from telethon.errors import (ChannelPrivateError, FloodWaitError,
                             UsernameInvalidError, UsernameNotOccupiedError)
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.types import InputPeerChannel, MessageMediaDocument, MessageMediaPhoto

logger = logging.getLogger("accounts.gateway.ops")

TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


# ------------------------------------------------------------------ helpers

def _peer(value):
    """'@name' / 'name' / '-100123' / 123 → те, що приймає Telethon."""
    if isinstance(value, dict):
        return _entity_from_spec(value)
    if isinstance(value, int):
        return value
    s = str(value).strip().lstrip("@")
    return int(s) if s.lstrip("-").isdigit() else s


def _entity_from_spec(spec: dict):
    """Специфікація чату від споживача → сутність для Telethon.

    {"username": "x"} — публічний; {"channel_id", "access_hash"} — приватний,
    хеш ПІД ЦИМ акаунтом; {"id": 123} — голий id (Telethon сам вирішить);
    {"linked_parent": "x"} — розв'язується окремо (_resolve_linked).
    """
    if spec.get("username"):
        return spec["username"].strip().lstrip("@")
    if spec.get("channel_id") and spec.get("access_hash"):
        return InputPeerChannel(int(spec["channel_id"]), int(spec["access_hash"]))
    if spec.get("id") is not None:
        return int(spec["id"])
    return None


async def _resolve_linked(client, parent: str):
    """Група обговорення каналу: без юзернейма й хешу, але Telegram віддає її
    через батьківський канал. -> (chat, {"id", "access_hash"}) або (None, None)."""
    full = await client(GetFullChannelRequest(parent.strip().lstrip("@")))
    linked_id = getattr(full.full_chat, "linked_chat_id", None)
    chat = next((c for c in full.chats if c.id == linked_id), None)
    if chat is None:
        return None, None
    return chat, {"id": int(chat.id), "access_hash": int(chat.access_hash)}


async def _input_peer_meta(client, entity) -> dict | None:
    """(id, access_hash) сутності ПІД ЦИМ акаунтом — споживач кешує, щоб
    наступні операції не платили ResolveUsernameRequest."""
    try:
        ip = await client.get_input_entity(entity)
        cid, ah = getattr(ip, "channel_id", None), getattr(ip, "access_hash", None)
        if cid and ah:
            return {"id": int(cid), "access_hash": int(ah)}
    except Exception as e:  # noqa: BLE001 — кеш peer не має валити операцію
        logger.debug("input_peer: %r", e)
    return None


def _media_kind(m) -> str | None:
    return ("photo" if getattr(m, "photo", None)
            else "video" if getattr(m, "video", None) else None)


def _iso(dt):
    return dt.astimezone(_tz.utc).isoformat() if dt else None


async def _wait_reply(client, bot, after_id, after_text, timeout=15.0, interval=0.5):
    """Чекати нове вхідне від бота (не те саме, що after_id/after_text)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(interval)
        incoming = [m for m in await client.get_messages(bot, limit=5) if not m.out]
        if not incoming:
            continue
        newest = incoming[0]
        if newest.id != after_id or (newest.text or "") != after_text:
            return newest
    return None


# ------------------------------------------------------------------ читання

async def scan(ctx, chats: list[dict], patterns: list[str] | None = None,
               media: dict | None = None) -> list[dict]:
    """ЄДИНА операція читання (план §10).

    chats: [{"key", "entity": spec, "min_id", "limit", "reverse"}].
    patterns: регулярки; порожньо = повертати всі текстові повідомлення.
    media: None | {"forward_to": peer, "per_tick": N, "pause": сек,
                   "which": "all"|"matched"} — пересилати фото/відео цим же
                   акаунтом у службовий чат, поки повідомлення «в руках».
    -> [{"key", "hits": [...], "max_id", "n_seen", "n_media", "error",
         "resolved": {"id","access_hash"}|None}]
    Помилка ОДНОГО чату (приватний, видалений) — у його `error`, решта читається.
    FloodWait — виняток нагору: це стан акаунта, а не чату.
    """
    client = ctx.client
    compiled = []
    for p in patterns or []:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            logger.error("scan: битий патерн %r (%s) — пропущено", p, e)
    media = media or {}
    media_peer = None
    if media.get("forward_to") is not None:
        media_peer = await client.get_entity(_peer(media["forward_to"]))
    per_tick = int(media.get("per_tick") or 30)
    pause = float(media.get("pause") or 0.5)
    only_matched = media.get("which") == "matched"

    out = []
    for ch in chats:
        row = {"key": ch.get("key"), "hits": [], "max_id": int(ch.get("min_id") or 0),
               "n_seen": 0, "n_media": 0, "error": None, "resolved": None}
        out.append(row)
        spec = ch.get("entity") or {}
        try:
            if spec.get("linked_parent"):
                entity, row["resolved"] = await _resolve_linked(client, spec["linked_parent"])
                if entity is None:
                    row["error"] = "linked: у каналу немає групи обговорення"
                    continue
            else:
                entity = _entity_from_spec(spec)
            if entity is None:
                row["error"] = "немає юзернейма й access_hash"
                continue
            min_id = int(ch.get("min_id") or 0)
            kwargs = dict(limit=int(ch.get("limit") or 100))
            if min_id:
                kwargs["min_id"] = min_id
            if ch.get("reverse"):
                kwargs["reverse"] = True
            async for m in client.iter_messages(entity, **kwargs):
                row["max_id"] = max(row["max_id"], int(m.id))
                kind = _media_kind(m)
                text = (getattr(m, "message", None) or "").strip()
                term = None
                matched = False
                if text:
                    row["n_seen"] += 1
                    if compiled:
                        hit = next((p.pattern for p in compiled if p.search(text)), None)
                        matched = hit is not None
                        term = hit[:60] if hit else None
                    else:
                        matched = True
                if (media_peer is not None and kind and row["n_media"] < per_tick
                        and (matched or not only_matched)):
                    try:
                        await client.forward_messages(media_peer, m.id, entity)
                        row["n_media"] += 1
                        await asyncio.sleep(pause)
                    except FloodWaitError:
                        raise
                    except Exception as e:  # noqa: BLE001 — медіа не має валити читання
                        logger.warning("scan: медіа %s/%s не переслалось: %r",
                                       row["key"], m.id, e)
                if not matched:
                    continue
                row["hits"].append({
                    "mid": int(m.id), "text": text, "date": _iso(m.date),
                    "author_id": getattr(getattr(m, "from_id", None), "user_id", None),
                    "term": term,
                    "media": ({"kind": kind, "mid": int(m.id),
                               "group": getattr(m, "grouped_id", None)} if kind else None),
                })
            if row["resolved"] is None and not isinstance(entity, InputPeerChannel):
                row["resolved"] = await _input_peer_meta(client, entity)
        except FloodWaitError:
            raise
        except (ChannelPrivateError, UsernameInvalidError, UsernameNotOccupiedError,
                ValueError) as e:
            # «No user has X as username» — це ліміт резолву на АКАУНТІ, тож
            # летить нагору (RESOLVE); решта — вина чату
            if isinstance(e, ValueError) and ("as username" in str(e)
                                              or "Cannot find any entity" in str(e)):
                raise
            row["error"] = f"{type(e).__name__}: {str(e)[:90]}"
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ in ("ConnectionError", "TimeoutError") or isinstance(
                    e, (ConnectionError, TimeoutError, OSError)):
                raise
            row["error"] = f"{type(e).__name__}: {str(e)[:90]}"
    return out


async def search(ctx, chats: list[dict], terms: list[str], since: str | None = None,
                 limit: int = 50, pause: float = 1.0) -> list[dict]:
    """Серверний пошук Telegram (`search=term`) по чатах — для tgs_search.
    since — ISO-дата, старіше відкидаємо."""
    from datetime import datetime
    client = ctx.client
    since_dt = datetime.fromisoformat(since) if since else None
    out = []
    for ch in chats:
        row = {"key": ch.get("key"), "hits": [], "error": None}
        out.append(row)
        entity = _entity_from_spec(ch.get("entity") or {})
        if entity is None:
            row["error"] = "немає юзернейма й access_hash"
            continue
        found = {}
        try:
            for term in terms:
                msgs = await client.get_messages(entity, search=term, limit=limit)
                for m in msgs:
                    text = (getattr(m, "message", None) or "").strip()
                    if not text or not m.date or (since_dt and m.date < since_dt):
                        continue
                    found[m.id] = {
                        "mid": m.id, "text": text, "date": _iso(m.date),
                        "author_id": getattr(getattr(m, "from_id", None), "user_id", None),
                        "term": term,
                    }
                await asyncio.sleep(pause)
        except FloodWaitError:
            raise
        except Exception as e:  # noqa: BLE001
            row["error"] = f"{type(e).__name__}: {str(e)[:90]}"
        row["hits"] = list(found.values())
    return out


async def resolve(ctx, handle) -> dict:
    """Сутність за юзернеймом/id → {id, access_hash, username, title}."""
    ent = await ctx.client.get_entity(_peer(handle))
    meta = await _input_peer_meta(ctx.client, ent) or {}
    return {**meta, "tg_id": getattr(ent, "id", None),
            "username": getattr(ent, "username", None),
            "title": getattr(ent, "title", None) or getattr(ent, "first_name", None)}


async def channel_meta(ctx, handle) -> dict:
    ent = await ctx.client.get_entity(_peer(handle))
    full = None
    try:
        full = await ctx.client(GetFullChannelRequest(ent))
    except Exception:  # noqa: BLE001 — не канал / нема прав: базові поля все одно є
        pass
    return {
        "tg_id": getattr(ent, "id", None),
        "username": getattr(ent, "username", None),
        "title": getattr(ent, "title", None),
        "description": getattr(full.full_chat, "about", "") if full else "",
        "subscribers": getattr(full.full_chat, "participants_count", 0) if full else 0,
    }


async def message_date(ctx, handle, msg_id: int) -> str | None:
    msg = await ctx.client.get_messages(_peer(handle), ids=int(msg_id))
    return _iso(msg.date) if msg and msg.date else None


async def dialogs(ctx, kind: str = "", limit: int = 200) -> list[dict]:
    out = []
    async for d in ctx.client.iter_dialogs(limit=limit):
        k = "канал" if d.is_channel else ("група" if d.is_group else "приват")
        if kind and k != kind:
            continue
        out.append({"kind": k, "name": d.name,
                    "username": getattr(d.entity, "username", None), "id": d.id})
    return out


async def recent_messages(ctx, peer, limit: int = 20) -> list[dict]:
    entity = await ctx.client.get_entity(_peer(peer))
    out = []
    async for m in ctx.client.iter_messages(entity, limit=limit):
        out.append({"id": m.id, "out": bool(m.out), "text": m.text or "",
                    "date": _iso(m.date)})
    return out


async def get_me(ctx) -> dict:
    me = await ctx.client.get_me()
    return {"id": me.id, "username": me.username, "first_name": me.first_name,
            "phone": me.phone, "premium": bool(getattr(me, "premium", False))}


async def check_alive(ctx) -> dict:
    """Живість БЕЗ надсилання: get_me. Розлогінений — виняток (DEAUTH) з live."""
    me = await get_me(ctx)
    uname = f"@{me['username']}" if me["username"] else "—"
    prem = " · premium" if me["premium"] else ""
    return {"state": "живий", "ok": True, "detail": f"{uname}{prem}"}


# ------------------------------------------------------------------ дії

async def forward(ctx, from_chat, msg_id: int, to_chat) -> dict:
    """Копія на боці Telegram: файл не йде через нас. Акаунт має бути учасником
    приймача, джерело — без noforwards."""
    res = await ctx.client.forward_messages(_peer(to_chat), int(msg_id), _peer(from_chat))
    mid = res[0].id if isinstance(res, list) and res else getattr(res, "id", None)
    return {"ok": True, "message_id": mid}


async def send_post(ctx, to_chat, text: str, src_chat=None, src_msg_id: int = 0,
                    src_peer: dict | None = None) -> dict:
    """Пост від імені акаунта з медіа першоджерела в тому ж повідомленні.
    Медіа — посиланням на оригінал (msg.media): нічого не качається; ціна —
    підпис до 1024 символів, текст ріже викликач."""
    client = ctx.client
    dst = _peer(to_chat)
    media = None
    out_peer = None
    if src_chat and src_msg_id:
        if src_peer and src_peer.get("id") and src_peer.get("access_hash"):
            src = InputPeerChannel(int(src_peer["id"]), int(src_peer["access_hash"]))
        else:
            src = _peer(src_chat)
        try:
            orig = await client.get_messages(src, ids=int(src_msg_id))
            out_peer = await _input_peer_meta(client, src)
            # лише справжні вкладення: msg.photo містить і прев'ю посилання,
            # а send_file на MessageMediaWebPage падає
            if isinstance(getattr(orig, "media", None), (MessageMediaPhoto, MessageMediaDocument)):
                media = orig.media
        except FloodWaitError:
            raise
        except Exception as e:  # noqa: BLE001 — без медіа пост усе одно вийде
            logger.warning("send_post: медіа %s/%s не дістали: %r", src_chat, src_msg_id, e)
    if media is not None:
        res = await client.send_file(dst, media, caption=text, parse_mode="html")
    else:
        res = await client.send_message(dst, text, parse_mode="html", link_preview=False)
    return {"ok": True, "message_id": getattr(res, "id", None),
            "with_media": media is not None, "src_peer": out_peer}


async def join(ctx, handles: list[str]) -> dict:
    """Підписати на канали з паузами (прогрів). FloodWait тут — результат, не
    виняток: частину вже підписали, і це треба повернути."""
    client = ctx.client
    joined, failed, flood = [], [], None
    for handle in handles:
        clean = handle.strip().lstrip("@").replace("https://t.me/", "").strip("/")
        if not clean:
            continue
        try:
            entity = await client.get_entity(clean)
            await client(JoinChannelRequest(entity))
            joined.append(clean)
        except FloodWaitError as e:
            failed.append(f"{clean}: flood-wait {e.seconds}с")
            flood = int(e.seconds)
            break
        except (ChannelPrivateError, UsernameInvalidError, UsernameNotOccupiedError) as e:
            failed.append(f"{clean}: {type(e).__name__}")
        except Exception as e:  # noqa: BLE001
            failed.append(f"{clean}: {type(e).__name__}: {str(e)[:80]}")
        await asyncio.sleep(random.uniform(3, 8))
    return {"ok": True, "joined": joined, "failed": failed, "flood_wait": flood}


# ------------------------------------------------------------------ сервіс

async def spam_status(ctx) -> dict:
    """/start до @SpamBot — офіційний спосіб дізнатись про обмеження."""
    client = ctx.client
    bot = await client.get_entity("SpamBot")
    await client.send_message(bot, "/start")
    deadline = asyncio.get_event_loop().time() + 15
    reply = None
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1)
        msgs = [m for m in await client.get_messages(bot, limit=3) if not m.out]
        if msgs and (msgs[0].text or "").strip():
            reply = msgs[0].text.strip()
            break
    if not reply:
        return {"status": "unknown", "detail": "немає відповіді за 15с", "ok": False}
    low = reply.lower()
    if "good news" in low or "no limits" in low or "free of any limitations" in low:
        status = "free"
    elif "frozen" in low:
        status = "frozen"
    elif "limited" in low or "restrict" in low:
        status = "limited"
    else:
        status = "unknown"
    return {"status": status, "detail": reply[:300], "ok": True}


async def test_bot(ctx, bot_username: str, feedback_text: str = "",
                   choices: list | None = None, max_steps: int = 15) -> dict:
    """/start → кнопкові кроки → відгук. Фіксує тексти/кнопки для оператора."""
    client = ctx.client
    steps = []
    bot = await client.get_entity(bot_username)
    await client.send_message(bot, "/start")
    last_id, last_text, q_index, sent_feedback = 0, "", 0, False
    for i in range(max_steps):
        msg = await _wait_reply(client, bot, last_id, last_text)
        if msg is None:
            steps.append({"step": i + 1, "error": "немає відповіді за 15с"})
            return {"ok": False, "error": "таймаут очікування відповіді бота", "steps": steps}
        buttons = [b.text for row in (msg.buttons or []) for b in row]
        step = {"step": i + 1, "text": msg.text or "", "buttons": buttons}
        steps.append(step)
        last_id, last_text = msg.id, msg.text or ""
        if buttons:
            idx = (choices[q_index] if choices and q_index < len(choices)
                   else random.randint(0, min(3, len(buttons) - 1)))
            idx = max(0, min(idx, len(buttons) - 1))
            step["clicked"] = buttons[idx]
            q_index += 1
            await msg.click(idx)
        elif not sent_feedback and feedback_text:
            await client.send_message(bot, feedback_text)
            step["sent_feedback"] = feedback_text
            sent_feedback = True
        else:
            break
    return {"ok": True, "steps": steps}


async def _set_bot_photo(client, bot, last_msg, username: str, photo_bytes: bytes) -> dict:
    await client.send_message(bot, "/setuserpic")
    pm = await _wait_reply(client, bot, last_msg.id, last_msg.text or "")
    if pm is None or not pm.buttons:
        return {"ok": False, "error": "BotFather не показав список ботів на /setuserpic"}
    if not await _click_bot_button(pm, username):
        return {"ok": False, "error": f"бот @{username} не знайдений у списку BotFather"}
    pm2 = await _wait_reply(client, bot, pm.id, pm.text or "")
    if pm2 is None:
        return {"ok": False, "error": "BotFather не відповів після вибору бота"}
    photo_file = io.BytesIO(photo_bytes)
    photo_file.name = "photo.jpg"   # без імені Telethon шле як файл, BotFather хоче Photo
    await client.send_file(bot, photo_file, force_document=False)
    pm3 = await _wait_reply(client, bot, pm2.id, pm2.text or "", timeout=20)
    if pm3 is None:
        return {"ok": False, "error": "BotFather не підтвердив отримання фото"}
    text = pm3.text or ""
    if "success" in text.lower():
        return {"ok": True}
    return {"ok": False, "error": "BotFather відхилив фото", "detail": text[:300]}


async def _click_bot_button(msg, username: str) -> bool:
    for row in msg.buttons or []:
        for b in row:
            if username.lower() in (b.text or "").lower():
                await msg.click(text=b.text)
                return True
    return False


async def botfather(ctx, op: str, **kw) -> dict:
    """create(name, username, photo_b64?) / sync() / set_name(username, new_name)
    / set_photo(username, photo_b64)."""
    import base64
    from types import SimpleNamespace
    client = ctx.client
    bot = await client.get_entity("BotFather")

    if op == "create":
        name, username = kw["name"], kw["username"]
        await client.send_message(bot, "/newbot")
        msg = await _wait_reply(client, bot, 0, "")
        if msg is None:
            return {"ok": False, "error": "BotFather не відповів на /newbot"}
        await client.send_message(bot, name)
        msg2 = await _wait_reply(client, bot, msg.id, msg.text or "")
        if msg2 is None:
            return {"ok": False, "error": "BotFather не відповів на назву",
                    "detail": (msg.text or "")[:300]}
        await client.send_message(bot, username)
        msg3 = await _wait_reply(client, bot, msg2.id, msg2.text or "", timeout=20)
        if msg3 is None:
            return {"ok": False, "error": "BotFather не відповів на username",
                    "detail": (msg2.text or "")[:300]}
        m = TOKEN_RE.search(msg3.text or "")
        if not m:
            return {"ok": False, "error": "не вдалось знайти токен у відповіді",
                    "detail": (msg3.text or "")[:400]}
        res = {"ok": True, "token": m.group(0), "username": username,
               "photo_ok": None, "photo_error": None}
        if kw.get("photo_b64"):
            pr = await _set_bot_photo(client, bot, msg3, username,
                                      base64.b64decode(kw["photo_b64"]))
            res["photo_ok"], res["photo_error"] = pr.get("ok"), pr.get("error")
        return res

    if op == "sync":
        await client.send_message(bot, "/token")
        msg = await _wait_reply(client, bot, 0, "")
        if msg is None:
            return {"ok": False, "error": "BotFather не відповів на /token", "bots": []}
        usernames = [b.text.lstrip("@") for row in (msg.buttons or []) for b in row if b.text]
        found = []
        for uname in usernames:
            if not await _click_bot_button(msg, uname):
                continue
            reply = await _wait_reply(client, bot, msg.id, msg.text or "")
            token = None
            if reply and reply.text:
                m = TOKEN_RE.search(reply.text)
                token = m.group(0) if m else None
            display_name = ""
            try:
                ent = await client.get_entity(uname)
                display_name = getattr(ent, "first_name", "") or ""
            except Exception:  # noqa: BLE001
                pass
            found.append({"username": uname, "token": token, "name": display_name})
            # список кнопок «з'їдається» відповіддю з токеном — /token знову
            await client.send_message(bot, "/token")
            msg2 = await _wait_reply(client, bot, reply.id if reply else msg.id,
                                     reply.text if reply else (msg.text or ""))
            if msg2 is None:
                break
            msg = msg2
        return {"ok": True, "bots": found}

    if op == "set_name":
        username, new_name = kw["username"], kw["new_name"]
        await client.send_message(bot, "/setname")
        msg = await _wait_reply(client, bot, 0, "")
        if msg is None or not msg.buttons:
            return {"ok": False, "error": "BotFather не показав список ботів"}
        if not await _click_bot_button(msg, username):
            return {"ok": False, "error": f"бот @{username} не знайдений у списку BotFather"}
        msg2 = await _wait_reply(client, bot, msg.id, msg.text or "")
        if msg2 is None:
            return {"ok": False, "error": "BotFather не відповів після вибору бота"}
        await client.send_message(bot, new_name)
        msg3 = await _wait_reply(client, bot, msg2.id, msg2.text or "")
        if msg3 is None:
            return {"ok": False, "error": "BotFather не підтвердив нову назву"}
        text = msg3.text or ""
        return {"ok": "success" in text.lower(), "detail": text[:300]}

    if op == "set_photo":
        return await _set_bot_photo(client, bot, SimpleNamespace(id=0, text=""),
                                    kw["username"], base64.b64decode(kw["photo_b64"]))

    return {"ok": False, "error": f"невідома операція botfather: {op}"}


# ------------------------------------------------------------------ авторизація

async def send_code(ctx) -> dict:
    """Крок 1 входу. Сесія свіжа — зберігаємо її, бо в ній DC/auth key для кроку 2."""
    sent = await ctx.client.send_code_request(ctx.account.phone_number)
    await ctx.save(session_string=ctx.client.session.save(),
                   auth_code_hash=sent.phone_code_hash)
    return {"success": True, "code_type": type(sent.type).__name__,
            "next_type": type(sent.next_type).__name__ if sent.next_type else None}


async def verify_code(ctx, code: str, password: str | None = None) -> dict:
    client = ctx.client
    try:
        await client.sign_in(ctx.account.phone_number, code,
                             phone_code_hash=ctx.account.auth_code_hash)
    except Exception as e:
        if "password" in str(e).lower() and password:
            await client.sign_in(password=password)
        else:
            raise
    await ctx.save(session_string=client.session.save(), is_authenticated=True,
                   auth_code_hash="", state="ready")
    return {"success": True}


# ------------------------------------------------------------------ реєстр

# op → (функція, таймаут с, чи потрібна авторизація)
OPS = {
    "scan": (scan, 120, True),
    "search": (search, 300, True),
    "resolve": (resolve, 60, True),
    "channel_meta": (channel_meta, 60, True),
    "message_date": (message_date, 60, True),
    "dialogs": (dialogs, 60, True),
    "recent_messages": (recent_messages, 30, True),
    "get_me": (get_me, 30, True),
    "check_alive": (check_alive, 30, True),
    "forward": (forward, 60, True),
    "send_post": (send_post, 90, True),
    "join": (join, 300, True),
    "spam_status": (spam_status, 60, True),
    "test_bot": (test_bot, 180, True),
    "botfather": (botfather, 180, True),
    "send_code": (send_code, 60, False),
    "verify_code": (verify_code, 60, False),
}
