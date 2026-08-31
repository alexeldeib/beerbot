"""Tests for resolving gateway routes into workspace context."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch

from src.beerbot.repositories import GatewayRouteRepository


class FakeConnection:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, statement: str, *args):
        return self.row


class FakePool:
    def __init__(self, row):
        self.connection = FakeConnection(row)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


async def test_resolve_returns_workspace_connection_and_route():
    now = datetime.now(UTC)
    row = {
        "workspace_id": "groupme:group-1",
        "workspace_name": "Friends",
        "workspace_timezone": "America/New_York",
        "workspace_settings": json.dumps({"personality": "classic"}),
        "workspace_created_at": now,
        "workspace_updated_at": now,
        "connection_id": "groupme-bot:group-1",
        "connection_gateway_type": "groupme",
        "connection_name": "Friends bot",
        "connection_credential_ref": "legacy:groups/group-1/bot_id",
        "connection_config": json.dumps({"legacy_group_id": "group-1"}),
        "connection_status": "active",
        "connection_created_at": now,
        "connection_updated_at": now,
        "route_id": "groupme-route:group-1",
        "route_gateway_type": "groupme",
        "route_key": "group-1",
        "external_conversation_id": "group-1",
        "route_name": "Friends",
        "route_config": json.dumps({}),
        "route_status": "active",
        "route_created_at": now,
        "route_updated_at": now,
    }

    with patch("src.beerbot.repositories.get_pool", return_value=FakePool(row)):
        result = await GatewayRouteRepository().resolve("groupme", "group-1")

    assert result is not None
    assert result.workspace.id == "groupme:group-1"
    assert result.workspace.settings == {"personality": "classic"}
    assert result.connection.id == "groupme-bot:group-1"
    assert result.connection.config == {"legacy_group_id": "group-1"}
    assert result.route.route_key == "group-1"


async def test_resolve_returns_none_for_unknown_route():
    with patch("src.beerbot.repositories.get_pool", return_value=FakePool(None)):
        result = await GatewayRouteRepository().resolve("groupme", "unknown")

    assert result is None
