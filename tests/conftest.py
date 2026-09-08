"""Pytest configuration and fixtures."""

from unittest.mock import AsyncMock, MagicMock
import asyncio

import pytest
import pytest_asyncio
import asyncpg
import os
from uuid import uuid4
from src.beerbot import database


@pytest_asyncio.fixture
async def pg(monkeypatch):
    dsn = os.environ.get("BEERBOT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set BEERBOT_TEST_DATABASE_URL to a disposable PostgreSQL database")
    schema = "test_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def configure(connection):
        # Reset hooks/proxies can restore search_path when a connection is
        # returned. Establish and assert isolation on EVERY acquisition.
        await connection.execute(f'SET search_path TO "{schema}"')
        assert await connection.fetchval("SELECT current_schema()") == schema

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, setup=configure)
    monkeypatch.setattr(database, "_pool", pool)
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.fixture(autouse=True)
def isolate_external_services(monkeypatch):
    """Keep endpoint tests away from the developer's real database and gateways."""
    from src.beerbot import main

    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "close_pool", AsyncMock())

    async def idle_worker(*args):
        await asyncio.Future()

    monkeypatch.setattr(main, "execution_worker", idle_worker)
    monkeypatch.setattr(main, "delivery_worker", idle_worker)
    monkeypatch.setattr(
        main, "accept_message", AsyncMock(return_value={"status": "ok", "action": "queued"})
    )
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
