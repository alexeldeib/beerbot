"""Tests for FastAPI endpoints."""

import pytest
from unittest.mock import AsyncMock, patch


@pytest.fixture
def sample_groupme_message():
    return {
        "attachments": [],
        "avatar_url": "https://example.com/avatar.png",
        "created_at": 1703700000,
        "group_id": "12345",
        "id": "msg-001",
        "name": "Test User",
        "sender_id": "user-001",
        "sender_type": "user",
        "text": "+1 beer",
        "user_id": "user-001",
    }


@pytest.fixture
def bot_message():
    return {
        "attachments": [],
        "avatar_url": None,
        "created_at": 1703700000,
        "group_id": "12345",
        "id": "msg-002",
        "name": "Beerius",
        "sender_id": "bot-001",
        "sender_type": "bot",
        "text": "Cheers!",
        "user_id": "bot-001",
    }


class TestCallback:
    @pytest.mark.asyncio
    @patch("src.beerbot.main.groupme_client")
    @patch("src.beerbot.main.beer_agent")
    async def test_processes_user_message(self, mock_agent, mock_groupme, sample_groupme_message):
        mock_agent.process_message = AsyncMock(return_value="Cheers! +1 beer logged.")
        mock_groupme.send_message = AsyncMock(return_value=True)

        from fastapi.testclient import TestClient
        from src.beerbot.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/callback", json=sample_groupme_message)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["action"] == "replied"

    @pytest.mark.asyncio
    @patch("src.beerbot.main.beer_agent")
    async def test_ignores_bot_messages(self, mock_agent, bot_message):
        from fastapi.testclient import TestClient
        from src.beerbot.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/callback", json=bot_message)

        assert resp.status_code == 200
        assert resp.json()["reason"] == "bot message"
        mock_agent.process_message.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.beerbot.main.beer_agent")
    async def test_returns_none_action_when_agent_silent(self, mock_agent, sample_groupme_message):
        mock_agent.process_message = AsyncMock(return_value=None)

        from fastapi.testclient import TestClient
        from src.beerbot.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/callback", json=sample_groupme_message)

        assert resp.status_code == 200
        assert resp.json()["action"] == "none"

    @pytest.mark.asyncio
    async def test_rejects_invalid_payload(self):
        from fastapi.testclient import TestClient
        from src.beerbot.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/callback", json={"bad": "data"})

        assert resp.status_code == 400


class TestHealthCheck:
    def test_health(self):
        from fastapi.testclient import TestClient
        from src.beerbot.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/health")

        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"


class TestWeeklyRecap:
    @pytest.mark.asyncio
    @patch("src.beerbot.main.settings")
    async def test_disabled_returns_disabled(self, mock_settings):
        mock_settings.weekly_recap_enabled = False
        mock_settings.admin_token = "test-token"

        from fastapi.testclient import TestClient
        from src.beerbot.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/admin/weekly-recap",
                headers={"Authorization": "Bearer test-token"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"
