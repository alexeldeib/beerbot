"""Tests for versioned schema migration ordering."""

from contextlib import asynccontextmanager
from unittest.mock import patch

from src.beerbot.database import init_db


class FakeConnection:
    def __init__(self, applied: bool = False):
        self.applied = applied
        self.statements: list[str] = []

    async def execute(self, statement: str, *args):
        self.statements.append(" ".join(statement.split()))
        return "OK"

    async def fetchval(self, statement: str, *args):
        return self.applied

    @asynccontextmanager
    async def transaction(self):
        yield


class FakePool:
    def __init__(self, connection: FakeConnection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


async def test_fresh_schema_adds_drink_type_before_unique_index():
    connection = FakeConnection()
    pool = FakePool(connection)

    with patch("src.beerbot.database.get_pool", return_value=pool):
        await init_db()

    add_column = next(
        index
        for index, statement in enumerate(connection.statements)
        if "ADD COLUMN IF NOT EXISTS drink_type" in statement
    )
    create_index = next(
        index
        for index, statement in enumerate(connection.statements)
        if "CREATE UNIQUE INDEX IF NOT EXISTS idx_beers_message_user_type" in statement
    )

    assert add_column < create_index
    assert any("INSERT INTO schema_migrations" in statement for statement in connection.statements)


async def test_applied_migration_is_skipped():
    connection = FakeConnection(applied=True)
    pool = FakePool(connection)

    with patch("src.beerbot.database.get_pool", return_value=pool):
        await init_db()

    assert not any(
        "CREATE TABLE IF NOT EXISTS beers" in statement for statement in connection.statements
    )


async def test_workspace_gateway_migration_is_additive_and_backfills_groups():
    connection = FakeConnection()
    pool = FakePool(connection)

    with patch("src.beerbot.database.get_pool", return_value=pool):
        await init_db()

    statements = connection.statements
    workspace_table = next(
        index
        for index, statement in enumerate(statements)
        if "CREATE TABLE IF NOT EXISTS workspaces" in statement
    )
    connection_table = next(
        index
        for index, statement in enumerate(statements)
        if "CREATE TABLE IF NOT EXISTS gateway_connections" in statement
    )
    route_table = next(
        index
        for index, statement in enumerate(statements)
        if "CREATE TABLE IF NOT EXISTS gateway_routes" in statement
    )
    workspace_backfill = next(
        index for index, statement in enumerate(statements) if "INSERT INTO workspaces" in statement
    )

    assert workspace_table < connection_table < route_table < workspace_backfill
    assert any("ALTER TABLE groups ADD COLUMN IF NOT EXISTS workspace_id" in s for s in statements)
    assert any("INSERT INTO gateway_routes" in s for s in statements)


async def test_shadow_identity_migration_backfills_people_and_memberships():
    connection = FakeConnection()
    pool = FakePool(connection)

    with patch("src.beerbot.database.get_pool", return_value=pool):
        await init_db()

    statements = connection.statements
    people_table = next(
        index
        for index, statement in enumerate(statements)
        if "CREATE TABLE IF NOT EXISTS people" in statement
    )
    identities_table = next(
        index
        for index, statement in enumerate(statements)
        if "CREATE TABLE IF NOT EXISTS external_identities" in statement
    )
    memberships_table = next(
        index
        for index, statement in enumerate(statements)
        if "CREATE TABLE IF NOT EXISTS workspace_memberships" in statement
    )
    people_backfill = next(
        index for index, statement in enumerate(statements) if "INSERT INTO people" in statement
    )

    assert people_table < identities_table < memberships_table < people_backfill
    assert any("ALTER TABLE users ADD COLUMN IF NOT EXISTS person_id" in s for s in statements)
    assert any("INSERT INTO external_identities" in s for s in statements)
    assert any("INSERT INTO workspace_memberships" in s for s in statements)
    assert any("CREATE TABLE IF NOT EXISTS person_merges" in s for s in statements)
