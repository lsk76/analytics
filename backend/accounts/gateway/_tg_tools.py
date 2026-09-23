"""Універсальні Telegram-операції для MCP (`tg_*`): чати, повідомлення,
учасники й адміністрування, контакти, профіль, теки, чернетки.

Приватний модуль gateway — єдине місце, де для цих операцій живе Telethon.
Той самий контракт, що й `_telethon.OPS`: `async def op(ctx, **kwargs)`,
результат — JSON-сумісний словник/список; помилки Telegram не ловимо
(класифікує `live.LiveAccount`). Логічні невдачі («немає такого повідомлення»)
— результат `{"ok": False, "error": …}`, бо акаунт тут ні до чого.

Реєструється в `_telethon.OPS` через `register()`.
"""
from __future__ import annotations

import base64
import io
from datetime import datetime, timezone as _tz

from telethon.tl import functions as fn, types as t

from ._telethon import _iso, _peer

MAX_DOWNLOAD = 8 * 1024 * 1024      # base64 у JSON-відповіді: більше — лише в файл


# ------------------------------------------------------------------ серіалізація

def _name(u) -> str:
    if u is None:
        return ""
    if getattr(u, "title", None):
        return u.title
    return " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x)


def _user(u) -> dict:
    if u is None:
        return {}
    st = getattr(u, "status", None)
    return {"id": u.id, "username": getattr(u, "username", None), "name": _name(u),
            "phone": getattr(u, "phone", None), "bot": bool(getattr(u, "bot", False)),
            "premium": bool(getattr(u, "premium", False)),
            "verified": bool(getattr(u, "verified", False)),
            "deleted": bool(getattr(u, "deleted", False)), "status": _status(st)}


def _status(st) -> str:
    if st is None:
        return ""
    n = type(st).__name__
    if n == "UserStatusOnline":
        return "online"
    if n == "UserStatusOffline":
        return f"offline, був {_iso(st.was_online)}"
    return {"UserStatusRecently": "нещодавно", "UserStatusLastWeek": "цього тижня",
            "UserStatusLastMonth": "цього місяця"}.get(n, n)


def _chat(e) -> dict:
    if e is None:
        return {}
    kind = ("user" if isinstance(e, t.User) else
            "channel" if isinstance(e, t.Channel) and e.broadcast else
            "supergroup" if isinstance(e, t.Channel) else "group")
    return {"id": e.id, "kind": kind, "title": _name(e), "username": getattr(e, "username", None),
            "members": getattr(e, "participants_count", None),
            "forum": bool(getattr(e, "forum", False)),
            "restricted": bool(getattr(e, "restricted", False)),
            "scam": bool(getattr(e, "scam", False) or getattr(e, "fake", False))}


def _media(m) -> dict | None:
    md = getattr(m, "media", None)
    if md is None:
        return None
    if getattr(m, "photo", None):
        return {"kind": "photo"}
    doc = getattr(m, "document", None)
    if doc is not None:
        name = next((a.file_name for a in doc.attributes if isinstance(a, t.DocumentAttributeFilename)), "")
        kind = ("video" if getattr(m, "video", None) else "voice" if getattr(m, "voice", None)
                else "audio" if getattr(m, "audio", None) else "sticker" if getattr(m, "sticker", None)
                else "gif" if getattr(m, "gif", None) else "document")
        return {"kind": kind, "name": name, "mime": doc.mime_type, "size": doc.size}
    if isinstance(md, t.MessageMediaPoll):
        return {"kind": "poll", "question": md.poll.question.text,
                "options": [a.text.text for a in md.poll.answers]}
    if isinstance(md, t.MessageMediaWebPage):
        return {"kind": "webpage", "url": getattr(md.webpage, "url", None)}
    return {"kind": type(md).__name__.replace("MessageMedia", "").lower()}


def _buttons(m) -> list:
    out = []
    for i, row in enumerate(getattr(m, "buttons", None) or []):
        for j, b in enumerate(row):
            out.append({"row": i, "col": j, "text": b.text, "url": getattr(b, "url", None),
                        "data": (b.data.decode("utf-8", "replace") if getattr(b, "data", None) else None)})
    return out


def _reactions(m) -> dict:
    r = getattr(m, "reactions", None)
    if not r or not r.results:
        return {}
    return {getattr(x.reaction, "emoticon", "custom"): x.count for x in r.results}


def _msg(m, full: bool = False) -> dict:
    if m is None:
        return {}
    sender = getattr(m, "sender", None)
    d = {"id": m.id, "date": _iso(m.date), "out": bool(m.out),
         "sender_id": getattr(m, "sender_id", None), "sender": _name(sender),
         "sender_username": getattr(sender, "username", None) if sender else None,
         "text": m.text or "", "reply_to": getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None),
         "media": _media(m), "views": getattr(m, "views", None),
         "forwards": getattr(m, "forwards", None), "pinned": bool(getattr(m, "pinned", False)),
         "edited": _iso(getattr(m, "edit_date", None)), "reactions": _reactions(m),
         "fwd_from": _name(getattr(getattr(m, "forward", None), "chat", None)
                           or getattr(getattr(m, "forward", None), "sender", None))
                     if getattr(m, "forward", None) else None}
    if full or getattr(m, "buttons", None):
        d["buttons"] = _buttons(m)
    return d


def _dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    d = datetime.fromisoformat(str(value))
    return d if d.tzinfo else d.replace(tzinfo=_tz.utc)


async def _ent(client, peer):
    return await client.get_entity(_peer(peer))


# ------------------------------------------------------------------ чати

async def chat_info(ctx, chat) -> dict:
    """Сутність + повна картка: опис, учасники, linked-чат, закріплене, права."""
    client = ctx.client
    e = await _ent(client, chat)
    out = _chat(e)
    try:
        if isinstance(e, t.Channel):
            full = (await client(fn.channels.GetFullChannelRequest(e))).full_chat
            out.update(about=full.about, members=full.participants_count,
                       admins=getattr(full, "admins_count", None),
                       online=getattr(full, "online_count", None),
                       linked_chat_id=getattr(full, "linked_chat_id", None),
                       pinned_msg_id=getattr(full, "pinned_msg_id", None),
                       slowmode_seconds=getattr(full, "slowmode_seconds", None),
                       can_view_participants=bool(getattr(full, "can_view_participants", False)),
                       invite_link=getattr(getattr(full, "exported_invite", None), "link", None))
        elif isinstance(e, t.Chat):
            full = (await client(fn.messages.GetFullChatRequest(e.id))).full_chat
            out.update(about=full.about, pinned_msg_id=getattr(full, "pinned_msg_id", None),
                       invite_link=getattr(getattr(full, "exported_invite", None), "link", None))
        elif isinstance(e, t.User):
            fu = await client(fn.users.GetFullUserRequest(e))
            out = {**_user(e), "kind": "user", "about": fu.full_user.about,
                   "common_chats": fu.full_user.common_chats_count,
                   "blocked": bool(fu.full_user.blocked),
                   "contact": bool(getattr(e, "contact", False))}
    except Exception as ex:  # noqa: BLE001 — базові поля є, повне — не обовʼязкове
        out["full_error"] = f"{type(ex).__name__}: {str(ex)[:120]}"
    return out


async def dialogs_ex(ctx, kind: str = "", limit: int = 100, unread_only: bool = False,
                     archived: bool = False, query: str = "") -> list[dict]:
    """Діалоги з непрочитаним і останнім повідомленням."""
    out = []
    async for d in ctx.client.iter_dialogs(limit=limit, archived=archived or None):
        k = ("channel" if d.is_channel and not d.is_group else "group" if d.is_group
             else "bot" if getattr(d.entity, "bot", False) else "user")
        if kind and k != kind:
            continue
        if unread_only and not d.unread_count:
            continue
        if query and query.lower() not in (d.name or "").lower():
            continue
        out.append({"id": d.id, "kind": k, "name": d.name,
                    "username": getattr(d.entity, "username", None),
                    "unread": d.unread_count, "pinned": bool(d.pinned),
                    "last": _iso(d.date), "last_text": (d.message.text or "")[:120] if d.message else ""})
    return out


async def search_global(ctx, query: str, limit: int = 30) -> list[dict]:
    out = []
    async for m in ctx.client.iter_messages(None, search=query, limit=limit):
        d = _msg(m)
        d["chat"] = _name(getattr(m, "chat", None))
        d["chat_id"] = getattr(m, "chat_id", None)
        out.append(d)
    return out


async def history(ctx, chat, limit: int = 30, offset_id: int = 0, min_id: int = 0, max_id: int = 0,
                  search: str = "", from_user=None, reverse: bool = False, offset_date=None,
                  media_only: bool = False) -> list[dict]:
    kw = {}
    if from_user:
        kw["from_user"] = _peer(from_user)
    if media_only:
        kw["filter"] = t.InputMessagesFilterPhotoVideo
    out = []
    async for m in ctx.client.iter_messages(_peer(chat), limit=limit, offset_id=offset_id,
                                            min_id=min_id, max_id=max_id, search=search or None,
                                            reverse=reverse, offset_date=_dt(offset_date), **kw):
        out.append(_msg(m))
    return out


async def messages(ctx, chat, ids: list[int]) -> list[dict]:
    got = await ctx.client.get_messages(_peer(chat), ids=[int(i) for i in ids])
    return [_msg(m, full=True) for m in got if m is not None]


async def context(ctx, chat, msg_id: int, around: int = 5) -> list[dict]:
    peer = _peer(chat)
    before = [m async for m in ctx.client.iter_messages(peer, limit=around, max_id=int(msg_id))]
    after = [m async for m in ctx.client.iter_messages(peer, limit=around, min_id=int(msg_id), reverse=True)]
    center = await ctx.client.get_messages(peer, ids=int(msg_id))
    rows = list(reversed(before)) + ([center] if center else []) + after
    return [_msg(m) for m in rows]


async def message_link(ctx, chat, msg_id: int) -> dict:
    e = await _ent(ctx.client, chat)
    u = getattr(e, "username", None)
    if u:
        return {"link": f"https://t.me/{u}/{int(msg_id)}"}
    if isinstance(e, t.Channel):
        return {"link": f"https://t.me/c/{e.id}/{int(msg_id)}"}
    return {"ok": False, "error": "приватний чат без посилань на повідомлення"}


# ------------------------------------------------------------------ повідомлення: дії

async def send(ctx, chat, text: str, reply_to: int = 0, parse_mode: str = "md",
               link_preview: bool = True, silent: bool = False, schedule=None) -> dict:
    m = await ctx.client.send_message(_peer(chat), text, reply_to=int(reply_to) or None,
                                      parse_mode=parse_mode or None, link_preview=link_preview,
                                      silent=silent or None, schedule=_dt(schedule))
    return {"ok": True, "message_id": getattr(m, "id", None), "date": _iso(getattr(m, "date", None))}


async def edit(ctx, chat, msg_id: int, text: str, parse_mode: str = "md",
               link_preview: bool = True) -> dict:
    m = await ctx.client.edit_message(_peer(chat), int(msg_id), text, parse_mode=parse_mode or None,
                                      link_preview=link_preview)
    return {"ok": True, "message_id": m.id, "edited": _iso(m.edit_date)}


async def delete(ctx, chat, ids: list[int], revoke: bool = True) -> dict:
    res = await ctx.client.delete_messages(_peer(chat), [int(i) for i in ids], revoke=revoke)
    return {"ok": True, "deleted": sum(getattr(r, "pts_count", 0) for r in res)}


async def forward_many(ctx, from_chat, ids: list[int], to_chat, silent: bool = False) -> dict:
    res = await ctx.client.forward_messages(_peer(to_chat), [int(i) for i in ids], _peer(from_chat),
                                            silent=silent or None)
    res = res if isinstance(res, list) else [res]
    return {"ok": True, "message_ids": [getattr(m, "id", None) for m in res if m]}


async def pin(ctx, chat, msg_id: int, notify: bool = False, unpin: bool = False) -> dict:
    if unpin:
        await ctx.client.unpin_message(_peer(chat), int(msg_id) or None)
    else:
        await ctx.client.pin_message(_peer(chat), int(msg_id), notify=notify)
    return {"ok": True}


async def mark_read(ctx, chat, max_id: int = 0) -> dict:
    await ctx.client.send_read_acknowledge(_peer(chat), max_id=int(max_id))
    return {"ok": True}


async def react(ctx, chat, msg_id: int, emoji: str = "", remove: bool = False, big: bool = False) -> dict:
    peer = _peer(chat)
    reaction = [] if remove or not emoji else [t.ReactionEmoji(emoticon=emoji)]
    await ctx.client(fn.messages.SendReactionRequest(peer=peer, msg_id=int(msg_id), reaction=reaction,
                                                     big=big or None))
    return {"ok": True}


async def poll(ctx, chat, question: str, options: list[str], multiple: bool = False,
               anonymous: bool = True, quiz_correct: int = -1) -> dict:
    answers = [t.PollAnswer(text=t.TextWithEntities(text=o, entities=[]), option=bytes([i]))
               for i, o in enumerate(options)]
    p = t.Poll(id=0, question=t.TextWithEntities(text=question, entities=[]), answers=answers, hash=0,
               multiple_choice=multiple or None, public_voters=(not anonymous) or None,
               quiz=(quiz_correct >= 0) or None)
    media = t.InputMediaPoll(poll=p, correct_answers=[bytes([quiz_correct])] if quiz_correct >= 0 else None)
    m = await ctx.client.send_file(_peer(chat), media)
    return {"ok": True, "message_id": getattr(m, "id", None)}


async def click(ctx, chat, msg_id: int, text: str = "", data: str = "", index: int = -1) -> dict:
    m = await ctx.client.get_messages(_peer(chat), ids=int(msg_id))
    if m is None:
        return {"ok": False, "error": "повідомлення не знайдено"}
    if not m.buttons:
        return {"ok": False, "error": "у повідомлення немає кнопок"}
    if data:
        res = await m.click(data=data.encode())
    elif text:
        res = await m.click(text=text)
    else:
        res = await m.click(max(0, int(index)))
    return {"ok": True, "answer": getattr(res, "message", None) if res is not None else None,
            "url": getattr(res, "url", None) if res is not None else None}


async def download(ctx, chat, msg_id: int, save_to: str = "") -> dict:
    m = await ctx.client.get_messages(_peer(chat), ids=int(msg_id))
    if m is None or not m.media:
        return {"ok": False, "error": "немає медіа"}
    if save_to:
        path = await ctx.client.download_media(m, file=save_to)
        return {"ok": True, "path": path, "media": _media(m)}
    size = (m.file.size if m.file else 0) or 0
    if size > MAX_DOWNLOAD:
        return {"ok": False, "error": f"файл {size} байт — більший за ліміт {MAX_DOWNLOAD}; "
                                      "передай save_to (шлях у контейнері gateway)"}
    data = await ctx.client.download_media(m, file=bytes)
    return {"ok": True, "name": (m.file.name if m.file else None) or f"msg{m.id}",
            "mime": m.file.mime_type if m.file else None, "size": len(data or b""),
            "base64": base64.b64encode(data or b"").decode()}


async def send_file(ctx, chat, name: str = "", base64_data: str = "", path: str = "",
                    caption: str = "", as_document: bool = False, reply_to: int = 0,
                    voice: bool = False) -> dict:
    if path:
        file = path
    else:
        buf = io.BytesIO(base64.b64decode(base64_data))
        buf.name = name or "file.bin"
        file = buf
    attrs = [t.DocumentAttributeAudio(duration=0, voice=True)] if voice else None
    m = await ctx.client.send_file(_peer(chat), file, caption=caption or None,
                                   force_document=as_document, reply_to=int(reply_to) or None,
                                   attributes=attrs, voice_note=voice or None)
    return {"ok": True, "message_id": getattr(m, "id", None)}


# ------------------------------------------------------------------ участь і адміністрування

async def leave(ctx, chat) -> dict:
    await ctx.client.delete_dialog(_peer(chat))
    return {"ok": True}


async def create_chat(ctx, title: str, users: list | None = None, channel: bool = False,
                      about: str = "", forum: bool = False) -> dict:
    client = ctx.client
    if channel or not users:
        res = await client(fn.channels.CreateChannelRequest(
            title=title, about=about or "", broadcast=channel or None,
            megagroup=(not channel) or None, forum=forum or None))
        ch = next((c for c in res.chats), None)
        return {"ok": True, "id": getattr(ch, "id", None), "kind": "channel" if channel else "supergroup"}
    res = await client(fn.messages.CreateChatRequest(users=[await _ent(client, u) for u in users],
                                                     title=title))
    upd = getattr(res, "updates", res)
    ch = next((c for c in getattr(upd, "chats", [])), None)
    return {"ok": True, "id": getattr(ch, "id", None), "kind": "group"}


async def invite(ctx, chat, users: list) -> dict:
    client = ctx.client
    e = await _ent(client, chat)
    inputs = [await _ent(client, u) for u in users]
    if isinstance(e, t.Channel):
        await client(fn.channels.InviteToChannelRequest(channel=e, users=inputs))
    else:
        for u in inputs:
            await client(fn.messages.AddChatUserRequest(chat_id=e.id, user_id=u, fwd_limit=50))
    return {"ok": True, "invited": len(inputs)}


async def kick(ctx, chat, user) -> dict:
    await ctx.client.kick_participant(_peer(chat), _peer(user))
    return {"ok": True}


async def ban(ctx, chat, user, unban: bool = False, until=None) -> dict:
    if unban:
        await ctx.client.edit_permissions(_peer(chat), _peer(user))
    else:
        await ctx.client.edit_permissions(_peer(chat), _peer(user), until_date=_dt(until),
                                          view_messages=False)
    return {"ok": True}


async def restrict(ctx, chat, user, send_messages: bool = True, send_media: bool = True,
                   invite_users: bool = True, pin_messages: bool = True, until=None) -> dict:
    await ctx.client.edit_permissions(_peer(chat), _peer(user), until_date=_dt(until),
                                      send_messages=send_messages, send_media=send_media,
                                      invite_users=invite_users, pin_messages=pin_messages)
    return {"ok": True}


async def admin(ctx, chat, user, promote: bool = True, title: str = "", rights: dict | None = None) -> dict:
    kw = {"is_admin": promote}
    if promote and rights:
        kw = {k: bool(v) for k, v in rights.items()
              if k in ("change_info", "post_messages", "edit_messages", "delete_messages",
                       "ban_users", "invite_users", "pin_messages", "add_admins", "manage_call",
                       "anonymous")}
    await ctx.client.edit_admin(_peer(chat), _peer(user), title=title or None, **kw)
    return {"ok": True}


async def participants(ctx, chat, limit: int = 100, search: str = "", kind: str = "") -> list[dict]:
    flt = {"admins": t.ChannelParticipantsAdmins(), "bots": t.ChannelParticipantsBots(),
           "kicked": t.ChannelParticipantsKicked(q=search), "banned": t.ChannelParticipantsBanned(q=search),
           "recent": t.ChannelParticipantsRecent()}.get(kind)
    out = []
    async for u in ctx.client.iter_participants(_peer(chat), limit=limit, search=search, filter=flt):
        d = _user(u)
        p = getattr(u, "participant", None)
        d["role"] = ("creator" if isinstance(p, (t.ChannelParticipantCreator, t.ChatParticipantCreator))
                     else "admin" if isinstance(p, (t.ChannelParticipantAdmin, t.ChatParticipantAdmin))
                     else "banned" if isinstance(p, t.ChannelParticipantBanned) else "member")
        out.append(d)
    return out


async def invite_link(ctx, chat, title: str = "", usage_limit: int = 0, expire=None,
                      request_needed: bool = False) -> dict:
    res = await ctx.client(fn.messages.ExportChatInviteRequest(
        peer=_peer(chat), title=title or None, usage_limit=int(usage_limit) or None,
        expire_date=_dt(expire), request_needed=request_needed or None))
    return {"ok": True, "link": getattr(res, "link", None)}


async def edit_chat(ctx, chat, title: str = "", about: str = "", slow_mode: int = -1,
                    username: str = "") -> dict:
    client = ctx.client
    e = await _ent(client, chat)
    done = []
    if title:
        if isinstance(e, t.Channel):
            await client(fn.channels.EditTitleRequest(channel=e, title=title))
        else:
            await client(fn.messages.EditChatTitleRequest(chat_id=e.id, title=title))
        done.append("title")
    if about:
        await client(fn.messages.EditChatAboutRequest(peer=e, about="" if about == "-" else about))
        done.append("about")
    if slow_mode >= 0 and isinstance(e, t.Channel):
        await client(fn.channels.ToggleSlowModeRequest(channel=e, seconds=int(slow_mode)))
        done.append("slow_mode")
    if username and isinstance(e, t.Channel):
        await client(fn.channels.UpdateUsernameRequest(channel=e, username="" if username == "-" else username.lstrip("@")))
        done.append("username")
    return {"ok": True, "changed": done}


async def common_chats(ctx, user, limit: int = 100) -> list[dict]:
    res = await ctx.client(fn.messages.GetCommonChatsRequest(user_id=await _ent(ctx.client, user),
                                                             max_id=0, limit=limit))
    return [_chat(c) for c in res.chats]


async def topics(ctx, chat, limit: int = 100, query: str = "") -> list[dict]:
    res = await ctx.client(fn.messages.GetForumTopicsRequest(peer=_peer(chat), offset_date=None,
                                                             offset_id=0, offset_topic=0, limit=limit,
                                                             q=query or None))
    return [{"id": tp.id, "title": tp.title, "closed": bool(getattr(tp, "closed", False)),
             "pinned": bool(getattr(tp, "pinned", False)), "top_message": tp.top_message,
             "unread": getattr(tp, "unread_count", 0)}
            for tp in res.topics if isinstance(tp, t.ForumTopic)]


# ------------------------------------------------------------------ контакти

async def contacts(ctx, query: str = "", limit: int = 200) -> list[dict]:
    res = await ctx.client(fn.contacts.GetContactsRequest(hash=0))
    users = getattr(res, "users", [])
    if query:
        q = query.lower()
        users = [u for u in users if q in (_name(u) + " " + (u.username or "") + " " + (u.phone or "")).lower()]
    return [_user(u) for u in users[:limit]]


async def contacts_search(ctx, query: str, limit: int = 20) -> dict:
    res = await ctx.client(fn.contacts.SearchRequest(q=query, limit=limit))
    return {"users": [_user(u) for u in res.users], "chats": [_chat(c) for c in res.chats]}


async def contact_add(ctx, phone: str, first_name: str, last_name: str = "") -> dict:
    res = await ctx.client(fn.contacts.ImportContactsRequest(contacts=[
        t.InputPhoneContact(client_id=0, phone=phone, first_name=first_name, last_name=last_name or "")]))
    return {"ok": True, "imported": [_user(u) for u in res.users],
            "retry_contacts": list(res.retry_contacts)}


async def contact_delete(ctx, users: list) -> dict:
    await ctx.client(fn.contacts.DeleteContactsRequest(id=[await _ent(ctx.client, u) for u in users]))
    return {"ok": True}


async def block(ctx, user, unblock: bool = False) -> dict:
    req = fn.contacts.UnblockRequest if unblock else fn.contacts.BlockRequest
    await ctx.client(req(id=await _ent(ctx.client, user)))
    return {"ok": True}


async def user_photos(ctx, user, limit: int = 10) -> dict:
    photos = await ctx.client.get_profile_photos(_peer(user), limit=limit)
    return {"total": photos.total, "photos": [{"id": p.id, "date": _iso(p.date)} for p in photos]}


# ------------------------------------------------------------------ профіль

async def update_profile(ctx, first_name: str = "", last_name: str = "", about: str = "",
                         username: str = "") -> dict:
    client = ctx.client
    done = []
    if first_name or last_name or about:
        await client(fn.account.UpdateProfileRequest(
            first_name=first_name or None,
            last_name=("" if last_name == "-" else last_name) if last_name else None,
            about=("" if about == "-" else about) if about else None))
        done += [k for k, v in (("first_name", first_name), ("last_name", last_name), ("about", about)) if v]
    if username:
        await client(fn.account.UpdateUsernameRequest(username="" if username == "-" else username.lstrip("@")))
        done.append("username")
    me = await client.get_me()
    return {"ok": True, "changed": done, "me": _user(me)}


async def set_photo(ctx, base64_data: str = "", path: str = "", delete: bool = False) -> dict:
    client = ctx.client
    if delete:
        photos = await client.get_profile_photos("me", limit=1)
        if not photos:
            return {"ok": False, "error": "фото профілю немає"}
        p = photos[0]
        await client(fn.photos.DeletePhotosRequest(id=[t.InputPhoto(id=p.id, access_hash=p.access_hash,
                                                                    file_reference=p.file_reference)]))
        return {"ok": True, "deleted": p.id}
    if path:
        up = await client.upload_file(path)
    else:
        buf = io.BytesIO(base64.b64decode(base64_data))
        buf.name = "photo.jpg"
        up = await client.upload_file(buf)
    await client(fn.photos.UploadProfilePhotoRequest(file=up))
    return {"ok": True}


_PRIVACY = {"phone": t.InputPrivacyKeyPhoneNumber, "last_seen": t.InputPrivacyKeyStatusTimestamp,
            "photo": t.InputPrivacyKeyProfilePhoto, "forwards": t.InputPrivacyKeyForwards,
            "calls": t.InputPrivacyKeyPhoneCall, "groups": t.InputPrivacyKeyChatInvite,
            "about": t.InputPrivacyKeyAbout, "birthday": t.InputPrivacyKeyBirthday}


async def privacy(ctx, key: str = "") -> dict:
    out = {}
    for k, cls in _PRIVACY.items():
        if key and k != key:
            continue
        try:
            res = await ctx.client(fn.account.GetPrivacyRequest(key=cls()))
            out[k] = [type(r).__name__.replace("PrivacyValue", "") for r in res.rules]
        except Exception as e:  # noqa: BLE001
            out[k] = [f"? {type(e).__name__}"]
    return out


# ------------------------------------------------------------------ теки й чернетки

async def folders(ctx) -> list[dict]:
    res = await ctx.client(fn.messages.GetDialogFiltersRequest())
    items = getattr(res, "filters", res)
    out = []
    for f in items:
        if not isinstance(f, t.DialogFilter):
            continue
        title = getattr(f.title, "text", f.title)
        out.append({"id": f.id, "title": title, "emoticon": f.emoticon,
                    "include": len(f.include_peers), "exclude": len(f.exclude_peers),
                    "pinned": len(f.pinned_peers)})
    return out


async def drafts(ctx) -> list[dict]:
    res = await ctx.client(fn.messages.GetAllDraftsRequest())
    out = []
    for u in getattr(res, "updates", []):
        if isinstance(u, t.UpdateDraftMessage) and isinstance(u.draft, t.DraftMessage):
            out.append({"peer": type(u.peer).__name__ + ":" + str(
                getattr(u.peer, "user_id", None) or getattr(u.peer, "channel_id", None)
                or getattr(u.peer, "chat_id", None)),
                "text": u.draft.message, "date": _iso(u.draft.date)})
    return out


async def draft(ctx, chat, text: str = "", clear: bool = False) -> dict:
    await ctx.client(fn.messages.SaveDraftRequest(peer=_peer(chat), message="" if clear else text))
    return {"ok": True}


# ------------------------------------------------------------------ реєстрація

OPS = {
    # (fn, timeout, needs_auth)
    "tg_chat_info": (chat_info, 60, True), "tg_dialogs": (dialogs_ex, 90, True),
    "tg_search_global": (search_global, 90, True), "tg_history": (history, 120, True),
    "tg_messages": (messages, 60, True), "tg_context": (context, 60, True),
    "tg_message_link": (message_link, 30, True),
    "tg_send": (send, 60, True), "tg_edit": (edit, 60, True), "tg_delete": (delete, 60, True),
    "tg_forward": (forward_many, 90, True), "tg_pin": (pin, 30, True),
    "tg_mark_read": (mark_read, 30, True), "tg_react": (react, 30, True), "tg_poll": (poll, 60, True),
    "tg_click": (click, 60, True), "tg_download": (download, 300, True),
    "tg_send_file": (send_file, 300, True),
    "tg_leave": (leave, 60, True), "tg_create_chat": (create_chat, 90, True),
    "tg_invite": (invite, 120, True), "tg_kick": (kick, 60, True), "tg_ban": (ban, 60, True),
    "tg_restrict": (restrict, 60, True), "tg_admin": (admin, 60, True),
    "tg_participants": (participants, 180, True), "tg_invite_link": (invite_link, 60, True),
    "tg_edit_chat": (edit_chat, 60, True), "tg_common_chats": (common_chats, 60, True),
    "tg_topics": (topics, 60, True),
    "tg_contacts": (contacts, 60, True), "tg_contacts_search": (contacts_search, 60, True),
    "tg_contact_add": (contact_add, 60, True), "tg_contact_delete": (contact_delete, 60, True),
    "tg_block": (block, 30, True), "tg_user_photos": (user_photos, 60, True),
    "tg_update_profile": (update_profile, 60, True), "tg_set_photo": (set_photo, 120, True),
    "tg_privacy": (privacy, 60, True),
    "tg_folders": (folders, 30, True), "tg_drafts": (drafts, 30, True), "tg_draft": (draft, 30, True),
}
