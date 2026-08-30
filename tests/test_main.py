"""Tests for FastAPI endpoints."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest


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
    @patch("src.beerbot.main.groupme_client")
    @patch("src.beerbot.main.beer_agent")
    async def test_reports_delivery_failure(self, mock_agent, mock_groupme, sample_groupme_message):
        mock_agent.process_message = AsyncMock(return_value="Reply")
        mock_groupme.send_message = AsyncMock(return_value=False)

        from fastapi.testclient import TestClient
        from src.beerbot.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/callback", json=sample_groupme_message)

        assert resp.status_code == 502
        assert resp.json()["action"] == "delivery_failed"

    @pytest.mark.asyncio
    @patch("src.beerbot.main.group_repo")
    @patch("src.beerbot.main.beer_agent")
    async def test_rejects_unregistered_group(
        self, mock_agent, mock_group_repo, sample_groupme_message
    ):
        mock_group_repo.get_by_group_id = AsyncMock(return_value=None)

        from fastapi.testclient import TestClient
        from src.beerbot.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/callback", json=sample_groupme_message)

        assert resp.status_code == 403
        mock_agent.process_message.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.beerbot.main.settings")
    @patch("src.beerbot.main.beer_agent")
    async def test_validates_configured_webhook_secret(
        self, mock_agent, mock_settings, sample_groupme_message
    ):
        mock_settings.groupme_webhook_secret = "expected"

        from fastapi.testclient import TestClient
        from src.beerbot.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/callback?token=wrong", json=sample_groupme_message)

        assert resp.status_code == 401
        mock_agent.process_message.assert_not_called()

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

    @patch("src.beerbot.main.settings")
    def test_version(self, mock_settings):
        mock_settings.app_version = "1.2.3"
        mock_settings.git_sha = "abc123"
        mock_settings.llm_provider = "google"
        mock_settings.llm_model = "gemini-3.6-flash"
        mock_settings.llm_base_url = None
        mock_settings.llm_supports_images = True
        mock_settings.llm_supports_video = True
        mock_settings.llm_supports_tools = True

        from fastapi.testclient import TestClient
        from src.beerbot.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/version")

        assert resp.status_code == 200
        assert resp.json()["git_sha"] == "abc123"
        assert resp.json()["llm"]["model"] == "gemini-3.6-flash"


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


class TestRecapScheduler:
    """Tests for the _recap_scheduler background loop logic."""

    @pytest.mark.asyncio
    @patch("src.beerbot.main.groupme_client")
    @patch("src.beerbot.main.beer_agent")
    @patch("src.beerbot.main.settings")
    async def test_sends_recap_on_sunday(self, mock_settings, mock_agent, mock_groupme):
        """Scheduler sends recap when it's Sunday after recap hour."""
        from src.beerbot.main import _recap_scheduler

        mock_settings.weekly_recap_enabled = True
        mock_settings.weekly_recap_hour = 21

        # Fake Sunday 9 PM ET
        sunday_9pm = datetime(2025, 1, 5, 21, 5, tzinfo=ZoneInfo("America/New_York"))

        mock_group = MagicMock()
        mock_group.group_id = "group-1"

        mock_recap_repo = AsyncMock()
        mock_recap_repo.has_sent = AsyncMock(return_value=False)
        mock_recap_repo.try_claim = AsyncMock(return_value=True)

        mock_group_repo = AsyncMock()
        mock_group_repo.list_all = AsyncMock(return_value=[mock_group])

        mock_agent.generate_weekly_recap = AsyncMock(return_value="Great week!")
        mock_groupme.send_message = AsyncMock(return_value=True)

        call_count = 0

        async def tick_then_stop(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        import asyncio

        with (
            patch("src.beerbot.main.asyncio.sleep", side_effect=tick_then_stop),
            patch("src.beerbot.main.datetime") as mock_dt,
            patch("src.beerbot.main.recap_repo", mock_recap_repo, create=True),
            patch("src.beerbot.main.group_repo", mock_group_repo, create=True),
            patch.dict(
                "sys.modules",
                {
                    "src.beerbot.repositories": MagicMock(
                        group_repo=mock_group_repo, recap_repo=mock_recap_repo
                    )
                },
            ),
        ):
            mock_dt.now.return_value = sunday_9pm
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            # Patch the repos at the import location inside _recap_scheduler
            with (
                patch("src.beerbot.repositories.group_repo", mock_group_repo),
                patch("src.beerbot.repositories.recap_repo", mock_recap_repo),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await _recap_scheduler()

        mock_agent.generate_weekly_recap.assert_called_once_with("group-1")
        mock_groupme.send_message.assert_called_once_with("Great week!", group_id="group-1")

    @pytest.mark.asyncio
    @patch("src.beerbot.main.groupme_client")
    @patch("src.beerbot.main.beer_agent")
    @patch("src.beerbot.main.settings")
    async def test_skips_when_already_sent(self, mock_settings, mock_agent, mock_groupme):
        """Scheduler skips groups that already have a recap for this week."""
        from src.beerbot.main import _recap_scheduler

        mock_settings.weekly_recap_enabled = True
        mock_settings.weekly_recap_hour = 21

        sunday_9pm = datetime(2025, 1, 5, 21, 5, tzinfo=ZoneInfo("America/New_York"))

        mock_group = MagicMock()
        mock_group.group_id = "group-1"

        mock_recap_repo = AsyncMock()
        mock_recap_repo.has_sent = AsyncMock(return_value=True)  # Already sent

        mock_group_repo = AsyncMock()
        mock_group_repo.list_all = AsyncMock(return_value=[mock_group])

        call_count = 0

        async def tick_then_stop(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        import asyncio

        with (
            patch("src.beerbot.main.asyncio.sleep", side_effect=tick_then_stop),
            patch("src.beerbot.main.datetime") as mock_dt,
            patch("src.beerbot.main.recap_repo", mock_recap_repo),
            patch("src.beerbot.main.group_repo", mock_group_repo),
            patch.dict(
                "sys.modules",
                {
                    "src.beerbot.repositories": MagicMock(
                        group_repo=mock_group_repo, recap_repo=mock_recap_repo
                    )
                },
            ),
        ):
            mock_dt.now.return_value = sunday_9pm
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with (
                patch("src.beerbot.repositories.group_repo", mock_group_repo),
                patch("src.beerbot.repositories.recap_repo", mock_recap_repo),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await _recap_scheduler()

        mock_agent.generate_weekly_recap.assert_not_called()
        mock_groupme.send_message.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.beerbot.main.groupme_client")
    @patch("src.beerbot.main.beer_agent")
    @patch("src.beerbot.main.settings")
    async def test_skips_non_sunday(self, mock_settings, mock_agent, mock_groupme):
        """Scheduler does nothing on non-Sunday days."""
        from src.beerbot.main import _recap_scheduler

        mock_settings.weekly_recap_enabled = True
        mock_settings.weekly_recap_hour = 21

        # Tuesday
        tuesday = datetime(2025, 1, 7, 21, 5, tzinfo=ZoneInfo("America/New_York"))

        call_count = 0

        async def tick_then_stop(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        import asyncio

        with (
            patch("src.beerbot.main.asyncio.sleep", side_effect=tick_then_stop),
            patch("src.beerbot.main.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = tuesday
            with pytest.raises(asyncio.CancelledError):
                await _recap_scheduler()

        mock_agent.generate_weekly_recap.assert_not_called()
