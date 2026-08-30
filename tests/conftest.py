"""Pytest configuration and fixtures."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def isolate_external_services(monkeypatch):
    """Keep endpoint tests away from the developer's real database and gateways."""
    from src.beerbot import main

    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "close_pool", AsyncMock())
    monkeypatch.setattr(
        main.group_repo,
        "get_by_group_id",
        AsyncMock(return_value=MagicMock(group_id="12345")),
    )


@pytest.fixture
def sample_groupme_message():
    """Sample GroupMe message data."""
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
