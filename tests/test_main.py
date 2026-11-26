"""Tests for FastAPI endpoints."""

from datetime import datetime
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.beerbot.groupme_client import DeliveryResult


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
        assert data["action"] == "queued"
        mock_agent.process_message.assert_not_called()
        mock_groupme.send_message.assert_not_called()

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

        assert resp.status_code == 200
        assert resp.json()["action"] == "queued"
        mock_agent.process_message.assert_not_called()
        mock_groupme.send_message.assert_not_called()

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
        assert resp.json()["action"] == "queued"

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


SUNDAY_9PM = datetime(2025, 1, 5, 21, 5, tzinfo=ZoneInfo("America/New_York"))


@pytest.fixture
def recap(monkeypatch):
    """The recap scheduler at a given time against fake repos, model, and GroupMe."""
    from src.beerbot import main

    world = SimpleNamespace(
        repo=SimpleNamespace(
            has_sent=AsyncMock(return_value=False),
            try_claim=AsyncMock(return_value=True),
            release_claim=AsyncMock(),
        ),
        groups=AsyncMock(return_value=[SimpleNamespace(group_id="group-1")]),
        generate=AsyncMock(return_value="Great week!"),
        deliver=AsyncMock(return_value=DeliveryResult("sent")),
    )
    monkeypatch.setattr(main, "recap_repo", world.repo)
    monkeypatch.setattr(main.group_repo, "list_all", world.groups)
    monkeypatch.setattr(main.beer_agent, "generate_weekly_recap", world.generate)
    monkeypatch.setattr(main, "groupme_client", SimpleNamespace(deliver_message=world.deliver))
    monkeypatch.setattr(main.settings, "weekly_recap_enabled", True)
    monkeypatch.setattr(main.settings, "weekly_recap_hour", 21)

    async def run(ticks: int = 1, at: datetime = SUNDAY_9PM):
        clock = MagicMock()
        clock.now.return_value = at
        monkeypatch.setattr(main, "datetime", clock)
        count = 0

        async def sleep(seconds):
            nonlocal count
            count += 1
            if count > ticks:
                raise asyncio.CancelledError

        monkeypatch.setattr(main.asyncio, "sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await main._recap_scheduler()

    world.run = run
    return world


class TestRecapScheduler:
    async def test_sends_recap_on_sunday(self, recap):
        await recap.run()
        recap.generate.assert_awaited_once_with("group-1")
        recap.deliver.assert_awaited_once_with("Great week!", group_id="group-1")

    async def test_skips_when_already_sent(self, recap):
        recap.repo.has_sent.return_value = True
        await recap.run()
        recap.generate.assert_not_awaited()
        recap.deliver.assert_not_awaited()

    async def test_skips_non_sunday(self, recap):
        await recap.run(at=datetime(2025, 1, 7, 21, 5, tzinfo=ZoneInfo("America/New_York")))
        recap.groups.assert_not_awaited()

    async def test_an_uncertain_send_keeps_its_claim_and_is_never_repeated(self, recap):
        recap.deliver.return_value = DeliveryResult("uncertain", "ReadTimeout")
        await recap.run(ticks=3)
        recap.deliver.assert_awaited_once()
        recap.repo.release_claim.assert_not_awaited()

    async def test_a_send_that_never_posted_releases_its_claim_and_retries(self, recap):
        recap.deliver.return_value = DeliveryResult("retry", "ConnectTimeout")
        await recap.run(ticks=2)
        assert recap.deliver.await_count == 2
        assert recap.repo.release_claim.await_count == 2

    async def test_a_quiet_week_is_done_without_checking_again(self, recap):
        recap.generate.return_value = None  # No drinks this week.
        await recap.run(ticks=3)
        recap.groups.assert_awaited_once()
        recap.repo.has_sent.assert_awaited_once()
        recap.deliver.assert_not_awaited()

    async def test_a_failed_recap_retries_at_the_next_tick(self, recap):
        recap.generate.side_effect = [RuntimeError("model down"), "Great week!"]
        await recap.run(ticks=3)
        recap.deliver.assert_awaited_once_with("Great week!", group_id="group-1")


def test_access_log_redacts_webhook_token():
    import logging

    from src.beerbot.main import _redact_tokens

    assert _redact_tokens in logging.getLogger("uvicorn.access").filters
    args = ("1.2.3.4:5", "POST", "/callback?token=s3cret&x=1", "1.1", 200)
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', args, None
    )
    assert _redact_tokens(record)
    assert record.getMessage() == '1.2.3.4:5 - "POST /callback?token=***&x=1 HTTP/1.1" 200'


def test_beerdevs_messages_become_reply_context(monkeypatch, sample_groupme_message):
    from fastapi.testclient import TestClient
    from src.beerbot import main

    kept = []
    monkeypatch.setattr(
        main, "accept_context", AsyncMock(side_effect=lambda m: kept.append(m.name))
    )
    monkeypatch.setattr(main.settings, "groupme_webhook_secret", None)
    with TestClient(main.app) as client:
        for name in ("beerdev", "beerius"):
            bot = sample_groupme_message | {"sender_type": "bot", "name": name, "id": name}
            assert client.post("/callback", json=bot).json()["reason"] == "bot message"
    assert kept == ["beerdev"]  # Kept as context, never processed; other bots are ignored.


def test_callback_routes_messages_naming_beerdev_to_beerdev(monkeypatch, sample_groupme_message):
    from fastapi.testclient import TestClient
    from src.beerbot import main

    routed = []

    async def accept(message, agent):
        routed.append((message.text, agent))
        return {"status": "ok"}

    monkeypatch.setattr(main, "accept_message", accept)
    monkeypatch.setattr(main.settings, "groupme_webhook_secret", None)
    monkeypatch.setattr(main.settings, "require_registered_groups", False)
    with TestClient(main.app) as client:
        for text in ("beerdev what's our spend?", "+1 beer"):
            client.post("/callback", json=sample_groupme_message | {"text": text, "id": text})
    assert routed == [("beerdev what's our spend?", "beerdev"), ("+1 beer", "beerius")]
