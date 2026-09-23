"""Telegram «руками» наших акаунтів: чати, повідомлення, учасники, контакти,
профіль — інструменти `tg_*`.

Усе йде через `accounts.services.registry` → `ManagedAccount.tg(op)` → gateway
(`accounts/gateway/_tg_tools.py`), тобто тією ж проксі й сесією, що й воркери;
стан/паузи/бан класифікує gateway. Акаунт — лише видимий викликачу
(`visible_to`); не вказаний — перший доступний.

Скоупи: читання — mcp:read; членство, правки, контакти, профіль — mcp:write;
НАДСИЛАННЯ (повідомлення, файли, пересилання, опитування, створення чатів,
запрошення, імпорт контактів) — mcp:admin: це видимі дії від імені акаунта,
за які прилітає спам-бан.
"""
from accounts.models import TelegramAccount
from accounts.services import registry as acc_registry
from accounts.services.managed import AccountUnavailable, RateLimited, TelegramOpError
from analysis.services.mcp_api import common, fmt
from analysis.services.mcp_api.registry import SCOPE_ADMIN, ToolError, tool

ACC_DOC = ("Акаунт: id, номер або частина назви (лише видимий тобі). Порожньо = перший "
           "доступний (активний, авторизований, без паузи).")
CHAT_DOC = "Чат: @username, посилання t.me/…, або числовий id (як у tg_dialogs)."
USER_DOC = "Користувач: @username або числовий id."


def _account(ref: str = "") -> TelegramAccount:
    if ref:
        return common.resolve_account(ref)
    qs = common.scope_accounts(TelegramAccount.objects.filter(
        is_active=True, is_authenticated=True)).order_by("id")
    for a in qs:
        if a.is_available:
            return a
    raise ToolError("немає доступного акаунта (активного, авторизованого, без паузи) — "
                    "accounts_list problems_only=true")


def _tg(op: str, account: str = "", **kw):
    """Виклик операції gateway від імені акаунта → (акаунт, результат)."""
    a = _account(account)
    try:
        res = acc_registry.get(a.id).tg(op, **{k: v for k, v in kw.items() if v is not None})
    except RateLimited as e:
        raise ToolError(f"акаунт #{a.id}: пауза {e.retry_after}с ({e.reason or 'rate-limited'}): {e}")
    except AccountUnavailable as e:
        raise ToolError(f"акаунт #{a.id} недоступний ({e.reason}): {e}")
    except TelegramOpError as e:
        raise ToolError(f"Telegram ({e.kind}): {e}")
    if isinstance(res, dict) and res.get("ok") is False:
        raise ToolError(str(res.get("error") or "операція не вдалась"))
    return a, res


def _head(a, title: str) -> str:
    return f"{title} · акаунт #{a.id} {a.name}"


def _ids(spec) -> list[int]:
    out = []
    for x in str(spec or "").replace(";", ",").split(","):
        x = x.strip()
        if x.lstrip("-").isdigit():
            out.append(int(x))
    if not out:
        raise ToolError("дай id повідомлень через кому")
    return out


def _list(spec) -> list[str]:
    return [x.strip() for x in str(spec or "").split(",") if x.strip()]


def _msg_rows(rows, with_chat=False):
    out = []
    for m in rows:
        media = m.get("media") or {}
        marks = ("📎" + media.get("kind", "") if media else "") + (" 📌" if m.get("pinned") else "")
        r = [m["id"], (m.get("date") or "")[:16].replace("T", " "),
             fmt.trunc(("→ " if m.get("out") else "") + (m.get("sender") or str(m.get("sender_id") or "")), 18)]
        if with_chat:
            r.append(fmt.trunc(m.get("chat") or "", 18))
        r += [fmt.trunc((m.get("text") or "").replace("\n", " "), 80), marks,
              m.get("views") or "", ",".join(f"{k}{v}" for k, v in (m.get("reactions") or {}).items())]
        out.append(r)
    return out


MSG_HEADERS = ["id", "коли", "від", "текст", "медіа", "перегл.", "реакції"]


def _users_table(users):
    return fmt.table(["id", "username", "імʼя", "телефон", "статус", "роль"],
                     [[u["id"], f"@{u['username']}" if u.get("username") else "—",
                       fmt.trunc(u.get("name") or "", 24), u.get("phone") or "—",
                       fmt.trunc(u.get("status") or "", 22), u.get("role") or ("бот" if u.get("bot") else "")]
                      for u in users]) if users else "нікого"


# ------------------------------------------------------------------ чати й читання

@tool("tg_dialogs", group="telegram", params={
      "account": ACC_DOC, "kind": "channel | group | user | bot. Порожньо = усі.",
      "unread_only": "true — лише з непрочитаним.", "archived": "true — архів замість основного списку.",
      "query": "Частина назви.", "limit": "Скільки діалогів."})
def tg_dialogs(account: str = "", kind: str = "", unread_only: bool = False, archived: bool = False,
               query: str = "", limit: int = 60):
    """Діалоги акаунта: чати, канали, приватні — з непрочитаним і останнім повідомленням.
    id звідси годиться в `chat=` решти tg_-інструментів."""
    a, rows = _tg("tg_dialogs", account, kind=kind, limit=limit, unread_only=unread_only,
                  archived=archived, query=query)
    return fmt.section(_head(a, f"Діалоги ({len(rows)})"), fmt.table(
        ["id", "тип", "назва", "username", "непроч.", "останнє", "текст"],
        [[d["id"], d["kind"], fmt.trunc(d["name"], 28), f"@{d['username']}" if d.get("username") else "—",
          d["unread"] or "", (d.get("last") or "")[:10], fmt.trunc(d.get("last_text") or "", 50)]
         for d in rows]) if rows else "порожньо")


@tool("tg_chat_info", group="telegram", params={"account": ACC_DOC, "chat": CHAT_DOC})
def tg_chat_info(chat: str, account: str = ""):
    """Картка чату/каналу/користувача: опис, учасники, linked-група, закріплене, invite-link."""
    a, d = _tg("tg_chat_info", account, chat=chat)
    return fmt.section(_head(a, f"{d.get('kind', '')} {d.get('title') or d.get('name') or chat}"),
                       fmt.kv([(k, v) for k, v in d.items() if v not in (None, "", [], {})]))


@tool("tg_history", group="telegram", params={
      "account": ACC_DOC, "chat": CHAT_DOC, "limit": "Скільки повідомлень (з кінця).",
      "search": "Текстовий пошук у цьому чаті.", "from_user": "Лише від цього користувача (@ або id).",
      "offset_id": "Старіші за це id (пагінація: візьми найменший id з попередньої сторінки).",
      "min_id": "Новіші за це id.", "max_id": "Старіші за це id.",
      "offset_date": "Старіші за дату/час ISO (2026-09-20 або 2026-09-20T12:00).",
      "reverse": "true — від старих до нових.", "media_only": "true — лише фото/відео."})
def tg_history(chat: str, account: str = "", limit: int = 30, search: str = "", from_user: str = "",
               offset_id: int = 0, min_id: int = 0, max_id: int = 0, offset_date: str = "",
               reverse: bool = False, media_only: bool = False):
    """Історія чату/каналу: останні N повідомлень або пошук/пагінація. Повний
    текст одного — `tg_messages`; довкола — `tg_context`."""
    a, rows = _tg("tg_history", account, chat=chat, limit=limit, search=search, from_user=from_user or None,
                  offset_id=offset_id, min_id=min_id, max_id=max_id, offset_date=offset_date or None,
                  reverse=reverse, media_only=media_only)
    return fmt.section(_head(a, f"{chat}: {len(rows)} повідомлень"),
                       fmt.table(MSG_HEADERS, _msg_rows(rows)) if rows else "порожньо")


@tool("tg_messages", group="telegram", params={"account": ACC_DOC, "chat": CHAT_DOC,
      "ids": "id повідомлень через кому."})
def tg_messages(chat: str, ids: str, account: str = ""):
    """Повідомлення повністю: текст без обрізання, медіа, кнопки, реакції, пересилання."""
    a, rows = _tg("tg_messages", account, chat=chat, ids=_ids(ids))
    parts = []
    for m in rows:
        kv = [("від", f"{m.get('sender') or ''} (#{m.get('sender_id')})"), ("коли", m.get("date")),
              ("відповідь на", m.get("reply_to")), ("медіа", m.get("media")),
              ("перегл./пересил.", f"{m.get('views')}/{m.get('forwards')}"),
              ("реакції", m.get("reactions")), ("переслано з", m.get("fwd_from")),
              ("кнопки", "; ".join(f"[{b['row']},{b['col']}] {b['text']}" + (f" → {b['url']}" if b.get('url') else "")
                                   for b in m.get("buttons") or []) or None),
              ("текст", m.get("text"))]
        parts.append(fmt.section(f"#{m['id']}", fmt.kv([(k, v) for k, v in kv if v not in (None, "", {}, [])])))
    return fmt.joinsec(_head(a, f"{chat}"), *parts) if parts else "не знайдено"


@tool("tg_context", group="telegram", params={"account": ACC_DOC, "chat": CHAT_DOC,
      "msg_id": "id повідомлення-центру.", "around": "Скільки повідомлень до і після."})
def tg_context(chat: str, msg_id: int, account: str = "", around: int = 5):
    """Повідомлення й N сусідніх до/після — контекст розмови."""
    a, rows = _tg("tg_context", account, chat=chat, msg_id=int(msg_id), around=around)
    return fmt.section(_head(a, f"{chat} довкола #{msg_id}"), fmt.table(MSG_HEADERS, _msg_rows(rows)))


@tool("tg_search_global", group="telegram", params={"account": ACC_DOC,
      "query": "Що шукати по всіх діалогах акаунта.", "limit": "Скільки результатів."})
def tg_search_global(query: str, account: str = "", limit: int = 30):
    """Пошук по всіх чатах акаунта (як рядок пошуку в Telegram)."""
    a, rows = _tg("tg_search_global", account, query=query, limit=limit)
    return fmt.section(_head(a, f"«{query}»: {len(rows)}"),
                       fmt.table(["id", "коли", "від", "чат", "текст", "медіа", "перегл.", "реакції"],
                                 _msg_rows(rows, with_chat=True)) if rows else "нічого")


@tool("tg_message_link", group="telegram", params={"account": ACC_DOC, "chat": CHAT_DOC, "msg_id": "id повідомлення."})
def tg_message_link(chat: str, msg_id: int, account: str = ""):
    """Посилання t.me/… на повідомлення (публічний або приватний канал)."""
    a, d = _tg("tg_message_link", account, chat=chat, msg_id=int(msg_id))
    return d["link"]


# ------------------------------------------------------------------ дії з повідомленнями

@tool("tg_send", group="telegram", mutates=True, scope=SCOPE_ADMIN, params={
      "account": ACC_DOC, "chat": CHAT_DOC, "text": "Текст (markdown за замовчуванням).",
      "reply_to": "id повідомлення, на яке відповідаємо. 0 = ні.",
      "parse_mode": "md | html | '' (без розмітки).", "link_preview": "Прев'ю посилань.",
      "silent": "true — без звуку.", "schedule": "Відкласти до ISO дати/часу (2026-09-25T09:00)."})
def tg_send(chat: str, text: str, account: str = "", reply_to: int = 0, parse_mode: str = "md",
            link_preview: bool = True, silent: bool = False, schedule: str = ""):
    """Надіслати повідомлення від імені акаунта. Це ВИДИМА дія: чужим людям і
    в чужі чати — спам-ризик для акаунта."""
    if not (text or "").strip():
        raise ToolError("порожній текст")
    a, d = _tg("tg_send", account, chat=chat, text=text, reply_to=reply_to, parse_mode=parse_mode,
               link_preview=link_preview, silent=silent, schedule=schedule or None)
    return f"надіслано #{d.get('message_id')} у {chat} акаунтом #{a.id}" + (f" (заплановано на {schedule})" if schedule else "")


@tool("tg_edit", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC,
      "msg_id": "id свого повідомлення.", "text": "Новий текст.", "parse_mode": "md | html | ''."})
def tg_edit(chat: str, msg_id: int, text: str, account: str = "", parse_mode: str = "md"):
    """Відредагувати своє повідомлення."""
    a, d = _tg("tg_edit", account, chat=chat, msg_id=int(msg_id), text=text, parse_mode=parse_mode)
    return f"#{msg_id} у {chat} відредаговано ({d.get('edited')})"


@tool("tg_delete", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC,
      "ids": "id повідомлень через кому.", "revoke": "true — видалити для всіх (де є право)."})
def tg_delete(chat: str, ids: str, account: str = "", revoke: bool = True):
    """Видалити повідомлення (свої — завжди; чужі — якщо адмін)."""
    a, d = _tg("tg_delete", account, chat=chat, ids=_ids(ids), revoke=revoke)
    return f"видалено {d.get('deleted')} у {chat}"


@tool("tg_forward", group="telegram", mutates=True, scope=SCOPE_ADMIN, params={"account": ACC_DOC,
      "from_chat": "Звідки: " + CHAT_DOC, "ids": "id повідомлень через кому.", "to_chat": "Куди: " + CHAT_DOC,
      "silent": "Без звуку."})
def tg_forward(from_chat: str, ids: str, to_chat: str, account: str = "", silent: bool = False):
    """Переслати повідомлення (файли не проходять через нас — копія на боці Telegram)."""
    a, d = _tg("tg_forward", account, from_chat=from_chat, ids=_ids(ids), to_chat=to_chat, silent=silent)
    return f"переслано {len(d.get('message_ids') or [])} → {to_chat}: {d.get('message_ids')}"


@tool("tg_pin", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC,
      "msg_id": "id повідомлення (0 разом з unpin=true = відкріпити все).",
      "notify": "Сповістити учасників.", "unpin": "true — відкріпити."})
def tg_pin(chat: str, msg_id: int = 0, account: str = "", notify: bool = False, unpin: bool = False):
    """Закріпити / відкріпити повідомлення."""
    _tg("tg_pin", account, chat=chat, msg_id=int(msg_id), notify=notify, unpin=unpin)
    return f"{'відкріплено' if unpin else 'закріплено'} #{msg_id or 'усе'} у {chat}"


@tool("tg_mark_read", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC,
      "max_id": "Прочитано до цього id (0 = усе)."})
def tg_mark_read(chat: str, account: str = "", max_id: int = 0):
    """Позначити чат прочитаним."""
    _tg("tg_mark_read", account, chat=chat, max_id=max_id)
    return f"{chat}: прочитано"


@tool("tg_react", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC,
      "msg_id": "id повідомлення.", "emoji": "Емодзі реакції (👍 ❤ 🔥 …).",
      "remove": "true — зняти свою реакцію.", "big": "Велика анімація."})
def tg_react(chat: str, msg_id: int, emoji: str = "", account: str = "", remove: bool = False, big: bool = False):
    """Поставити або зняти реакцію."""
    _tg("tg_react", account, chat=chat, msg_id=int(msg_id), emoji=emoji, remove=remove, big=big)
    return f"реакцію {'знято' if remove else emoji} на #{msg_id} у {chat}"


@tool("tg_poll", group="telegram", mutates=True, scope=SCOPE_ADMIN, params={"account": ACC_DOC,
      "chat": CHAT_DOC, "question": "Питання.", "options": "Варіанти через | (2–10).",
      "multiple": "Кілька відповідей.", "anonymous": "Анонімне (дефолт).",
      "quiz_correct": "Індекс правильної відповіді (0…) — робить вікторину; -1 = звичайне."})
def tg_poll(chat: str, question: str, options: str, account: str = "", multiple: bool = False,
            anonymous: bool = True, quiz_correct: int = -1):
    """Створити опитування."""
    opts = [o.strip() for o in options.split("|") if o.strip()]
    if not 2 <= len(opts) <= 10:
        raise ToolError("2–10 варіантів через |")
    a, d = _tg("tg_poll", account, chat=chat, question=question, options=opts, multiple=multiple,
               anonymous=anonymous, quiz_correct=int(quiz_correct))
    return f"опитування #{d.get('message_id')} у {chat}"


@tool("tg_click", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC,
      "msg_id": "id повідомлення з кнопками (список кнопок — tg_messages).",
      "text": "Текст кнопки.", "data": "callback-data кнопки.", "index": "Порядковий номер кнопки (0…), якщо text/data не задано."})
def tg_click(chat: str, msg_id: int, account: str = "", text: str = "", data: str = "", index: int = 0):
    """Натиснути inline-кнопку (боти, меню)."""
    a, d = _tg("tg_click", account, chat=chat, msg_id=int(msg_id), text=text, data=data, index=int(index))
    return f"натиснуто; відповідь: {d.get('answer') or '—'}" + (f"; url: {d['url']}" if d.get("url") else "")


@tool("tg_download", group="telegram", params={"account": ACC_DOC, "chat": CHAT_DOC,
      "msg_id": "id повідомлення з медіа.",
      "save_to": "Шлях у контейнері gateway, куди зберегти (напр. /app/media/x.jpg). Порожньо = повернути base64 (до 8 МБ)."})
def tg_download(chat: str, msg_id: int, account: str = "", save_to: str = ""):
    """Скачати медіа повідомлення: у файл gateway або base64 у відповідь."""
    a, d = _tg("tg_download", account, chat=chat, msg_id=int(msg_id), save_to=save_to)
    if d.get("path"):
        return f"збережено: {d['path']} ({d.get('media')})"
    return fmt.joinsec(fmt.kv([("файл", d.get("name")), ("mime", d.get("mime")), ("байт", d.get("size"))]),
                       "base64:\n" + (d.get("base64") or ""))


@tool("tg_send_file", group="telegram", mutates=True, scope=SCOPE_ADMIN, params={"account": ACC_DOC,
      "chat": CHAT_DOC, "name": "Імʼя файлу (для base64).", "base64_data": "Вміст файлу в base64.",
      "path": "Або шлях до файлу в контейнері gateway.", "caption": "Підпис.",
      "as_document": "true — як документ, а не фото/відео.", "reply_to": "Відповідь на id.",
      "voice": "true — як голосове (ogg/opus)."})
def tg_send_file(chat: str, account: str = "", name: str = "", base64_data: str = "", path: str = "",
                 caption: str = "", as_document: bool = False, reply_to: int = 0, voice: bool = False):
    """Надіслати файл/фото/відео/голосове."""
    if bool(base64_data) == bool(path):
        raise ToolError("дай рівно одне: base64_data або path")
    a, d = _tg("tg_send_file", account, chat=chat, name=name, base64_data=base64_data, path=path,
               caption=caption, as_document=as_document, reply_to=reply_to, voice=voice)
    return f"надіслано файл #{d.get('message_id')} у {chat}"


# ------------------------------------------------------------------ участь і адміністрування

@tool("tg_join", group="telegram", mutates=True, params={"account": ACC_DOC,
      "chats": "Канали/чати через кому: @name, t.me/name, t.me/+invite."})
def tg_join(chats: str, account: str = ""):
    """Вступити в канали/чати (з паузами між ними, як прогрів)."""
    a = _account(account)
    try:
        d = acc_registry.get(a.id).join(_list(chats))
    except (RateLimited, AccountUnavailable, TelegramOpError) as e:
        raise ToolError(f"акаунт #{a.id}: {e}")
    return fmt.kv([("вступив", ", ".join(d.get("joined") or []) or "—"),
                   ("не вдалось", "; ".join(d.get("failed") or []) or "—"),
                   ("flood-wait", d.get("flood_wait") or "—")])


@tool("tg_leave", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC})
def tg_leave(chat: str, account: str = ""):
    """Вийти з чату/каналу (діалог видаляється)."""
    a, _ = _tg("tg_leave", account, chat=chat)
    return f"акаунт #{a.id} вийшов із {chat}"


@tool("tg_create_chat", group="telegram", mutates=True, scope=SCOPE_ADMIN, params={"account": ACC_DOC,
      "title": "Назва.", "users": "Учасники через кому (@ або id) — для звичайної групи.",
      "channel": "true — канал (мовлення); false — група/супергрупа.", "about": "Опис.",
      "forum": "true — супергрупа з темами."})
def tg_create_chat(title: str, account: str = "", users: str = "", channel: bool = False, about: str = "",
                   forum: bool = False):
    """Створити групу, супергрупу або канал."""
    a, d = _tg("tg_create_chat", account, title=title, users=_list(users) or None, channel=channel,
               about=about, forum=forum)
    return f"створено {d.get('kind')} «{title}» id={d.get('id')} (акаунт #{a.id} — власник)"


@tool("tg_invite", group="telegram", mutates=True, scope=SCOPE_ADMIN, params={"account": ACC_DOC,
      "chat": CHAT_DOC, "users": "Кого додати, через кому (@ або id)."})
def tg_invite(chat: str, users: str, account: str = ""):
    """Додати людей у чат/канал (потрібне право; людина має дозволяти запрошення)."""
    a, d = _tg("tg_invite", account, chat=chat, users=_list(users))
    return f"запрошено {d.get('invited')} у {chat}"


@tool("tg_kick", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC, "user": USER_DOC})
def tg_kick(chat: str, user: str, account: str = ""):
    """Вигнати учасника (без бану — може повернутись)."""
    _tg("tg_kick", account, chat=chat, user=user)
    return f"{user} вигнано з {chat}"


@tool("tg_ban", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC, "user": USER_DOC,
      "unban": "true — розбанити.", "until": "До ISO дати (порожньо = назавжди)."})
def tg_ban(chat: str, user: str, account: str = "", unban: bool = False, until: str = ""):
    """Забанити / розбанити учасника."""
    _tg("tg_ban", account, chat=chat, user=user, unban=unban, until=until or None)
    return f"{user}: {'розбанено' if unban else 'забанено'} у {chat}"


@tool("tg_restrict", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC, "user": USER_DOC,
      "send_messages": "Може писати.", "send_media": "Може слати медіа.", "invite_users": "Може запрошувати.",
      "pin_messages": "Може закріплювати.", "until": "До ISO дати."})
def tg_restrict(chat: str, user: str, account: str = "", send_messages: bool = True, send_media: bool = True,
                invite_users: bool = True, pin_messages: bool = True, until: str = ""):
    """Обмежити права учасника (мут = send_messages=false)."""
    _tg("tg_restrict", account, chat=chat, user=user, send_messages=send_messages, send_media=send_media,
        invite_users=invite_users, pin_messages=pin_messages, until=until or None)
    return f"{user}: права оновлено у {chat}"


@tool("tg_admin", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC, "user": USER_DOC,
      "promote": "true — зробити адміном; false — зняти.", "title": "Титул адміна.",
      "rights": "Права через кому (порожньо = усі стандартні): change_info, post_messages, edit_messages, delete_messages, ban_users, invite_users, pin_messages, add_admins, manage_call, anonymous."})
def tg_admin(chat: str, user: str, account: str = "", promote: bool = True, title: str = "", rights: str = ""):
    """Призначити / зняти адміністратора."""
    r = {k: True for k in _list(rights)} or None
    _tg("tg_admin", account, chat=chat, user=user, promote=promote, title=title, rights=r)
    return f"{user}: {'адмін' if promote else 'знято адміна'} у {chat}"


@tool("tg_participants", group="telegram", params={"account": ACC_DOC, "chat": CHAT_DOC,
      "limit": "Скільки.", "search": "Пошук за іменем.", "kind": "admins | bots | kicked | banned | recent. Порожньо = усі."})
def tg_participants(chat: str, account: str = "", limit: int = 100, search: str = "", kind: str = ""):
    """Учасники чату/каналу (де це дозволено): адміни, боти, забанені."""
    a, rows = _tg("tg_participants", account, chat=chat, limit=limit, search=search, kind=kind)
    return fmt.section(_head(a, f"{chat}: {len(rows)} учасників"), _users_table(rows))


@tool("tg_invite_link", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC,
      "title": "Назва посилання.", "usage_limit": "Ліміт використань (0 = без).",
      "expire": "Дійсне до ISO дати.", "request_needed": "true — вступ за схваленням."})
def tg_invite_link(chat: str, account: str = "", title: str = "", usage_limit: int = 0, expire: str = "",
                   request_needed: bool = False):
    """Створити invite-посилання (потрібне право запрошувати)."""
    a, d = _tg("tg_invite_link", account, chat=chat, title=title, usage_limit=usage_limit,
               expire=expire or None, request_needed=request_needed)
    return d.get("link") or "—"


@tool("tg_edit_chat", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC,
      "title": "Нова назва.", "about": "Опис ('-' = очистити).", "slow_mode": "Повільний режим, секунд (0 = вимкнути; -1 = не чіпати).",
      "username": "Публічний username каналу ('-' = зняти)."})
def tg_edit_chat(chat: str, account: str = "", title: str = "", about: str = "", slow_mode: int = -1, username: str = ""):
    """Змінити назву/опис/повільний режим/username чату (потрібні права)."""
    a, d = _tg("tg_edit_chat", account, chat=chat, title=title, about=about, slow_mode=int(slow_mode), username=username)
    return f"{chat}: змінено {', '.join(d.get('changed') or []) or 'нічого'}"


@tool("tg_common_chats", group="telegram", params={"account": ACC_DOC, "user": USER_DOC, "limit": "Скільки."})
def tg_common_chats(user: str, account: str = "", limit: int = 100):
    """Спільні чати акаунта з користувачем."""
    a, rows = _tg("tg_common_chats", account, user=user, limit=limit)
    return fmt.table(["id", "тип", "назва", "username", "учасників"],
                     [[c["id"], c["kind"], fmt.trunc(c["title"], 30), c.get("username") or "—", c.get("members") or ""]
                      for c in rows]) if rows else "спільних чатів немає"


@tool("tg_topics", group="telegram", params={"account": ACC_DOC, "chat": CHAT_DOC, "limit": "Скільки.", "query": "Пошук за назвою."})
def tg_topics(chat: str, account: str = "", limit: int = 100, query: str = ""):
    """Теми форум-супергрупи."""
    a, rows = _tg("tg_topics", account, chat=chat, limit=limit, query=query)
    return fmt.table(["id", "назва", "закрита", "закріплена", "непроч."],
                     [[tp["id"], fmt.trunc(tp["title"], 40), fmt.flag(tp["closed"], "так", ""),
                       fmt.flag(tp["pinned"], "так", ""), tp["unread"] or ""] for tp in rows]) if rows else "тем немає"


# ------------------------------------------------------------------ контакти

@tool("tg_contacts", group="telegram", params={"account": ACC_DOC, "query": "Фільтр за іменем/username/телефоном.", "limit": "Скільки."})
def tg_contacts(account: str = "", query: str = "", limit: int = 200):
    """Контакти акаунта."""
    a, rows = _tg("tg_contacts", account, query=query, limit=limit)
    return fmt.section(_head(a, f"Контакти ({len(rows)})"), _users_table(rows))


@tool("tg_contacts_search", group="telegram", params={"account": ACC_DOC, "query": "Імʼя/username (глобальний пошук Telegram).", "limit": "Скільки."})
def tg_contacts_search(query: str, account: str = "", limit: int = 20):
    """Глобальний пошук людей і чатів у Telegram за іменем/username."""
    a, d = _tg("tg_contacts_search", account, query=query, limit=limit)
    return fmt.joinsec(fmt.section("Люди", _users_table(d.get("users") or [])),
                       fmt.section("Чати", fmt.table(["id", "тип", "назва", "username", "учасників"],
                                                     [[c["id"], c["kind"], fmt.trunc(c["title"], 30), c.get("username") or "—",
                                                       c.get("members") or ""] for c in d.get("chats") or []])
                                   if d.get("chats") else "—"))


@tool("tg_contact_add", group="telegram", mutates=True, scope=SCOPE_ADMIN, params={"account": ACC_DOC,
      "phone": "Телефон (+7…).", "first_name": "Імʼя.", "last_name": "Прізвище."})
def tg_contact_add(phone: str, first_name: str, account: str = "", last_name: str = ""):
    """Додати контакт за телефоном."""
    a, d = _tg("tg_contact_add", account, phone=phone, first_name=first_name, last_name=last_name)
    imported = d.get("imported") or []
    return ("додано: " + _users_table(imported)) if imported else "номер не в Telegram (або приховано)"


@tool("tg_contact_delete", group="telegram", mutates=True, params={"account": ACC_DOC, "users": "Кого прибрати, через кому (@ або id)."})
def tg_contact_delete(users: str, account: str = ""):
    """Видалити контакти."""
    _tg("tg_contact_delete", account, users=_list(users))
    return "контакти видалено"


@tool("tg_block", group="telegram", mutates=True, params={"account": ACC_DOC, "user": USER_DOC, "unblock": "true — розблокувати."})
def tg_block(user: str, account: str = "", unblock: bool = False):
    """Заблокувати / розблокувати користувача."""
    _tg("tg_block", account, user=user, unblock=unblock)
    return f"{user}: {'розблоковано' if unblock else 'заблоковано'}"


@tool("tg_user_photos", group="telegram", params={"account": ACC_DOC, "user": USER_DOC, "limit": "Скільки."})
def tg_user_photos(user: str, account: str = "", limit: int = 10):
    """Фото профілю користувача (кількість і дати; скачати — tg_download не підходить, лише перелік)."""
    a, d = _tg("tg_user_photos", account, user=user, limit=limit)
    return f"усього {d.get('total')}: " + ", ".join(f"{p['id']} ({(p.get('date') or '')[:10]})" for p in d.get("photos") or [])


# ------------------------------------------------------------------ профіль

@tool("tg_update_profile", group="telegram", mutates=True, params={"account": ACC_DOC,
      "first_name": "Імʼя.", "last_name": "Прізвище ('-' = очистити).", "about": "Про себе ('-' = очистити).",
      "username": "Публічний username ('-' = зняти)."})
def tg_update_profile(account: str = "", first_name: str = "", last_name: str = "", about: str = "", username: str = ""):
    """Змінити імʼя/прізвище/опис/username акаунта."""
    a, d = _tg("tg_update_profile", account, first_name=first_name, last_name=last_name, about=about, username=username)
    me = d.get("me") or {}
    return f"акаунт #{a.id}: змінено {', '.join(d.get('changed') or []) or 'нічого'}; тепер {me.get('name')} @{me.get('username') or '—'}"


@tool("tg_set_photo", group="telegram", mutates=True, params={"account": ACC_DOC,
      "base64_data": "Фото (jpg) у base64.", "path": "Або шлях у контейнері gateway.", "delete": "true — видалити поточне фото."})
def tg_set_photo(account: str = "", base64_data: str = "", path: str = "", delete: bool = False):
    """Встановити або видалити фото профілю."""
    if not delete and bool(base64_data) == bool(path):
        raise ToolError("дай рівно одне: base64_data або path (або delete=true)")
    a, d = _tg("tg_set_photo", account, base64_data=base64_data, path=path, delete=delete)
    return f"акаунт #{a.id}: фото {'видалено' if delete else 'оновлено'}"


@tool("tg_privacy", group="telegram", params={"account": ACC_DOC,
      "key": "phone | last_seen | photo | forwards | calls | groups | about | birthday. Порожньо = усі."})
def tg_privacy(account: str = "", key: str = ""):
    """Налаштування приватності акаунта (хто бачить телефон, статус, фото…)."""
    a, d = _tg("tg_privacy", account, key=key)
    return fmt.section(_head(a, "Приватність"), fmt.kv([(k, ", ".join(v)) for k, v in d.items()]))


# ------------------------------------------------------------------ теки й чернетки

@tool("tg_folders", group="telegram", params={"account": ACC_DOC})
def tg_folders(account: str = ""):
    """Теки чатів акаунта."""
    a, rows = _tg("tg_folders", account)
    return fmt.table(["id", "назва", "емодзі", "включено", "виключено", "закріплено"],
                     [[f["id"], f["title"], f.get("emoticon") or "", f["include"], f["exclude"], f["pinned"]]
                      for f in rows]) if rows else "тек немає"


@tool("tg_drafts", group="telegram", params={"account": ACC_DOC})
def tg_drafts(account: str = ""):
    """Чернетки у всіх чатах."""
    a, rows = _tg("tg_drafts", account)
    return fmt.table(["чат", "коли", "текст"], [[d["peer"], (d.get("date") or "")[:16], fmt.trunc(d["text"], 80)]
                                                 for d in rows]) if rows else "чернеток немає"


@tool("tg_draft", group="telegram", mutates=True, params={"account": ACC_DOC, "chat": CHAT_DOC,
      "text": "Текст чернетки.", "clear": "true — очистити чернетку."})
def tg_draft(chat: str, account: str = "", text: str = "", clear: bool = False):
    """Зберегти або очистити чернетку в чаті."""
    _tg("tg_draft", account, chat=chat, text=text, clear=clear)
    return f"{chat}: чернетку {'очищено' if clear else 'збережено'}"
