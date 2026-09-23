"""Операції tg_* gateway (accounts/gateway/_tg_tools.py) на фейковому Telethon:
серіалізація повідомлень/користувачів, побудова запитів, логічні відмови."""
import base64
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from asgiref.sync import async_to_sync
from telethon.tl import types as t

from accounts.gateway import _telethon, _tg_tools as ops


def run(coro):
    async def _wrap():
        return await coro
    return async_to_sync(_wrap)()


class FakeClient:
    def __init__(self):
        self.calls, self.requests = [], []
        self.entities = {}
        self.msgs = {}

    async def get_entity(self, peer):
        return self.entities.get(peer, NS(id=peer if isinstance(peer, int) else 7, username=None,
                                          title="Чат", first_name=None, last_name=None))

    async def get_input_entity(self, peer):
        return NS(channel_id=1, access_hash=2)

    async def iter_messages(self, entity, limit=None, **kw):
        self.calls.append(("iter_messages", entity, limit, kw))
        for m in list(self.msgs.values())[:limit]:
            yield m

    async def get_messages(self, entity, ids=None):
        if isinstance(ids, list):
            return [self.msgs.get(i) for i in ids]
        return self.msgs.get(ids)

    async def send_message(self, entity, text, **kw):
        self.calls.append(("send_message", entity, text, kw))
        return NS(id=101, date=datetime(2026, 9, 23, tzinfo=timezone.utc))

    async def send_file(self, entity, file, **kw):
        self.calls.append(("send_file", entity, file, kw))
        return NS(id=102)

    async def edit_message(self, entity, msg_id, text, **kw):
        return NS(id=msg_id, edit_date=datetime(2026, 9, 23, tzinfo=timezone.utc))

    async def delete_messages(self, entity, ids, revoke=True):
        return [NS(pts_count=len(ids))]

    async def __call__(self, request):
        self.requests.append(request)
        return NS(link="https://t.me/+abc", chats=[NS(id=555)], users=[], rules=[], filters=[])

    async def iter_participants(self, entity, limit=None, search="", filter=None):
        yield NS(id=1, username="adm", first_name="Адмін", last_name=None, phone=None, bot=False,
                 premium=False, verified=False, deleted=False, status=t.UserStatusRecently(),
                 participant=t.ChannelParticipantCreator(user_id=1, admin_rights=None, rank=None))
        yield NS(id=2, username=None, first_name="Юзер", last_name="Один", phone="+1", bot=False,
                 premium=False, verified=False, deleted=False, status=None, participant=None)

    async def kick_participant(self, entity, user):
        self.calls.append(("kick", entity, user))

    async def edit_permissions(self, entity, user=None, until_date=None, **rights):
        self.calls.append(("edit_permissions", entity, user, until_date, rights))

    async def edit_admin(self, entity, user, **kw):
        self.calls.append(("edit_admin", entity, user, kw))


def _msg(i, text="привіт", **kw):
    base = dict(id=i, date=datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc), out=False, sender_id=5,
                sender=NS(first_name="Іван", last_name=None, username="ivan", title=None),
                text=text, reply_to=None, media=None, photo=None, document=None, views=12, forwards=1,
                pinned=False, edit_date=None, reactions=None, forward=None, buttons=None)
    return NS(**{**base, **kw})


@pytest.fixture
def ctx():
    return NS(client=FakeClient(), account=None, save=None)


def test_all_tg_ops_registered_in_gateway():
    tg = {k for k in _telethon.OPS if k.startswith("tg_")}
    assert tg == set(ops.OPS) and len(tg) >= 40
    assert all(needs_auth for _, _, needs_auth in ops.OPS.values())


def test_history_serializes_messages(ctx):
    ctx.client.msgs = {1: _msg(1), 2: _msg(2, "з фото", photo=object(), media=object())}
    rows = run(ops.history(ctx, "@chat", limit=5, search="x", from_user="@ivan"))
    assert [r["id"] for r in rows] == [1, 2]
    assert rows[0]["sender"] == "Іван" and rows[0]["sender_username"] == "ivan" and rows[0]["views"] == 12
    assert rows[1]["media"] == {"kind": "photo"}
    _, entity, limit, kw = ctx.client.calls[0]
    assert entity == "chat" and limit == 5 and kw["search"] == "x" and kw["from_user"] == "ivan"


def test_send_edit_delete_and_schedule(ctx):
    d = run(ops.send(ctx, "-100123", "текст", reply_to=7, schedule="2026-09-25T09:00"))
    assert d["message_id"] == 101
    _, entity, text, kw = ctx.client.calls[0]
    assert entity == -100123 and kw["reply_to"] == 7 and kw["schedule"].tzinfo is not None
    assert run(ops.edit(ctx, "@c", 101, "нове"))["message_id"] == 101
    assert run(ops.delete(ctx, "@c", [1, 2, 3]))["deleted"] == 3


def test_poll_and_react_build_requests(ctx):
    d = run(ops.poll(ctx, "@c", "Питання?", ["так", "ні"], multiple=True))
    assert d["message_id"] == 102
    media = ctx.client.calls[0][2]
    assert isinstance(media, t.InputMediaPoll) and media.poll.question.text == "Питання?"
    assert [a.text.text for a in media.poll.answers] == ["так", "ні"] and media.poll.multiple_choice
    run(ops.react(ctx, "@c", 5, "👍"))
    req = ctx.client.requests[-1]
    assert req.msg_id == 5 and req.reaction[0].emoticon == "👍"
    run(ops.react(ctx, "@c", 5, remove=True))
    assert ctx.client.requests[-1].reaction == []


def test_participants_roles_and_admin_rights(ctx):
    rows = run(ops.participants(ctx, "@c", kind="admins"))
    assert rows[0]["role"] == "creator" and rows[0]["status"] == "нещодавно"
    assert rows[1]["role"] == "member" and rows[1]["name"] == "Юзер Один"
    run(ops.admin(ctx, "@c", "@u", promote=True, rights={"ban_users": True, "manage_topics": True}))
    assert ctx.client.calls[-1][3] == {"ban_users": True, "title": None}   # невідоме право відкинуто
    run(ops.admin(ctx, "@c", "@u", promote=False))
    assert ctx.client.calls[-1][3] == {"is_admin": False, "title": None}
    run(ops.ban(ctx, "@c", "@u"))
    assert ctx.client.calls[-1][4] == {"view_messages": False}
    run(ops.ban(ctx, "@c", "@u", unban=True))
    assert ctx.client.calls[-1][4] == {}


def test_click_and_download_logical_failures(ctx):
    ctx.client.msgs = {1: _msg(1), 2: _msg(2, media=None)}
    assert run(ops.click(ctx, "@c", 1))["ok"] is False          # немає кнопок
    assert run(ops.click(ctx, "@c", 99))["ok"] is False         # немає повідомлення
    assert run(ops.download(ctx, "@c", 2))["ok"] is False       # немає медіа


def test_send_file_from_base64_sets_name(ctx):
    data = base64.b64encode(b"hello").decode()
    run(ops.send_file(ctx, "@c", name="a.txt", base64_data=data, caption="cap", as_document=True))
    _, entity, file, kw = ctx.client.calls[0]
    assert file.name == "a.txt" and file.getvalue() == b"hello" and kw["force_document"] is True


def test_invite_link_and_create_channel(ctx):
    assert run(ops.invite_link(ctx, "@c", title="x"))["link"] == "https://t.me/+abc"
    d = run(ops.create_chat(ctx, "Новий", channel=True, about="опис"))
    assert d == {"ok": True, "id": 555, "kind": "channel"}
    req = ctx.client.requests[-1]
    assert req.title == "Новий" and req.broadcast is True
