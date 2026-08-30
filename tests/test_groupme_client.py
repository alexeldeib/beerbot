"""Tests for GroupMe outbound routing and delivery handling."""

from unittest.mock import AsyncMock, MagicMock, patch

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
