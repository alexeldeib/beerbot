"""Pytest configuration and fixtures."""

import pytest


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
        "text": "🍺",
        "user_id": "user-001",
    }
