from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import async_to_sync
from telethon.errors import (FloodWaitError, InviteHashExpiredError,
                             InviteHashInvalidError, InviteRequestSentError,
                             UserAlreadyParticipantError)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from accounts.gateway import _telethon


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(_telethon.asyncio, "sleep", AsyncMock())
    return AsyncMock()


@pytest.mark.parametrize("handle", [
    "https://t.me/+cbKFoHMNYuc1YTky", "http://t.me/+cbKFoHMNYuc1YTky",
    "t.me/+cbKFoHMNYuc1YTky", "+cbKFoHMNYuc1YTky",
    "https://t.me/joinchat/cbKFoHMNYuc1YTky", "joinchat/cbKFoHMNYuc1YTky",
    "https://telegram.me/joinchat/cbKFoHMNYuc1YTky",
])
def test_join_private_invite_does_not_resolve_username(client, handle):
    result = async_to_sync(_telethon.join)(SimpleNamespace(client=client), [handle])
    client.get_entity.assert_not_awaited()
    client.assert_awaited_once()
    request = client.await_args.args[0]
    assert isinstance(request, ImportChatInviteRequest)
    assert request.hash == "cbKFoHMNYuc1YTky"
    assert len(result["joined"]) == 1
    assert result["pending"] == result["failed"] == []


@pytest.mark.parametrize("handle", ["@public", "public", "https://t.me/public", "t.me/public"])
def test_join_public_channel(client, handle):
    result = async_to_sync(_telethon.join)(SimpleNamespace(client=client), [handle])
    client.get_entity.assert_awaited_once_with("public")
    request = client.await_args.args[0]
    assert isinstance(request, JoinChannelRequest)
    assert request.channel is client.get_entity.return_value
    assert result["joined"] == ["public"]


@pytest.mark.parametrize("error,field", [
    (UserAlreadyParticipantError, "joined"),
    (InviteRequestSentError, "pending"),
    (InviteHashExpiredError, "failed"),
    (InviteHashInvalidError, "failed"),
])
def test_invite_outcomes_and_continue(client, error, field):
    client.side_effect = [error(request=None), None]
    result = async_to_sync(_telethon.join)(SimpleNamespace(client=client), ["+abc", "@public"])
    assert len(result[field]) == (2 if field == "joined" else 1)
    assert result["joined"][-1] == "public"
    if field != "joined":
        assert "+abc" not in result["joined"]
    if field == "failed":
        assert error.__name__ in result["failed"][0]
    assert client.await_count == 2


def test_invite_flood_wait_stops_batch(client):
    client.side_effect = FloodWaitError(request=None, capture=30)
    result = async_to_sync(_telethon.join)(SimpleNamespace(client=client), ["+abc", "@public"])
    assert result["flood_wait"] == 30
    assert result["joined"] == result["pending"] == []
    assert result["failed"] == ["+abc: flood-wait 30с"]
    client.assert_awaited_once()
    client.get_entity.assert_not_awaited()
