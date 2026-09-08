"""Tests for GroupMe outbound routing and delivery handling."""

from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from src.beerbot.groupme_client import GroupMeClient


@patch("src.beerbot.groupme_client.httpx.AsyncClient")
async def test_accepts_any_successful_2xx_response(mock_http_cls):
    response = MagicMock(status_code=201, text="")
    http = AsyncMock()
    http.post = AsyncMock(return_value=response)
    mock_http_cls.return_value.__aenter__ = AsyncMock(return_value=http)
    mock_http_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    group_repo = MagicMock()
    group_repo.get_bot_id = AsyncMock(return_value="registered-bot")

    with patch("src.beerbot.repositories.group_repo", group_repo):
        sent = await GroupMeClient(default_bot_id="default-bot").send_message(
            "hello", group_id="group-1"
        )

    assert sent is True


async def test_rejects_unregistered_group_when_required():
    group_repo = MagicMock()
    group_repo.get_bot_id = AsyncMock(return_value=None)

    with (
        patch("src.beerbot.repositories.group_repo", group_repo),
        patch("src.beerbot.groupme_client.settings") as mock_settings,
    ):
        mock_settings.require_registered_groups = True
        sent = await GroupMeClient(default_bot_id="default-bot").send_message(
            "hello", group_id="unknown"
        )

    assert sent is False


@pytest.mark.parametrize(
    "outcome,state",
    [
        (httpx.ConnectTimeout("connect"), "retry"),
        (httpx.ReadTimeout("read"), "uncertain"),
        (httpx.WriteError("write"), "uncertain"),
        (429, "retry"),
        (503, "uncertain"),
        (408, "uncertain"),
        (403, "failed"),
        (202, "sent"),
    ],
)
async def test_delivery_classification(outcome, state):
    client = GroupMeClient(default_bot_id="test")
    http = AsyncMock()
    if isinstance(outcome, Exception):
        http.post.side_effect = outcome
    else:
        http.post.return_value = httpx.Response(outcome, headers={"retry-after": "17"})
    with patch("src.beerbot.groupme_client.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__ = AsyncMock(return_value=http)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await client.deliver_message("reply")
    assert result.state == state
    if outcome == 429:
        assert result.retry_after == 17
