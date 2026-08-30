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
