from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import async_to_sync
from telethon.errors import FloodWaitError, YouBlockedUserError
from telethon.tl.functions.contacts import UnblockRequest

from accounts.gateway import _telethon


def client_with_reply(send_error=None):
    client = AsyncMock()
    bot = SimpleNamespace(id=178220800)
    client.get_entity.return_value = bot
    client.get_messages.return_value = [SimpleNamespace(out=False, text="Good news, no limits!")]
    if send_error:
        client.send_message.side_effect = [send_error, None]
    return client, bot


@pytest.mark.parametrize("blocked", [False, True])
def test_spam_status_unblocks_only_when_needed(monkeypatch, blocked):
    monkeypatch.setattr(_telethon.asyncio, "sleep", AsyncMock())
    client, bot = client_with_reply(YouBlockedUserError(request=None) if blocked else None)
    result = async_to_sync(_telethon.spam_status)(SimpleNamespace(client=client))
    assert result == {"ok": True, "status": "free", "detail": "Good news, no limits!"}
    assert client.send_message.await_count == (2 if blocked else 1)
    assert all(call.args == (bot, "/start") for call in client.send_message.await_args_list)
    if blocked:
        client.assert_awaited_once()
        request = client.await_args.args[0]
        assert isinstance(request, UnblockRequest) and request.id is bot
    else:
        client.assert_not_awaited()


def test_spam_status_does_not_unblock_on_flood_wait():
    error = FloodWaitError(request=None, capture=30)
    client, _ = client_with_reply(error)
    with pytest.raises(FloodWaitError):
        async_to_sync(_telethon.spam_status)(SimpleNamespace(client=client))
    client.assert_not_awaited()
    assert client.send_message.await_count == 1


def test_unblock_failure_propagates_without_retry():
    error = FloodWaitError(request=None, capture=30)
    client, _ = client_with_reply(YouBlockedUserError(request=None))
    client.side_effect = error
    with pytest.raises(FloodWaitError):
        async_to_sync(_telethon.spam_status)(SimpleNamespace(client=client))
    assert client.send_message.await_count == 1
