"""Real database regressions, isolated in a new schema per test.

Only BEERBOT_TEST_DATABASE_URL enables these tests; never use DATABASE_URL.
CI always supplies a disposable PostgreSQL service.
"""

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.beerbot import database
from src.beerbot.models import DrinkType
from src.beerbot.reconciliation import LOCK_KEY, ReconciliationBusy, reconcile_identities
from src.beerbot.repositories import beer_repo


@pytest_asyncio.fixture
async def pg(monkeypatch):
    dsn = os.environ.get("BEERBOT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set BEERBOT_TEST_DATABASE_URL to a disposable PostgreSQL database")
    schema = "test_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    monkeypatch.setattr(database, "_pool", pool)
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def seed(pool):
    async with pool.acquire() as c:
        await c.execute("INSERT INTO workspaces(id,name) VALUES('w1','One'),('w2','Two')")
        await c.execute("""INSERT INTO groups(group_id,bot_id,workspace_id)
                           VALUES('g1','fake','w1'),('g2','fake','w2')""")
        alice = await c.fetchval("""INSERT INTO users(groupme_user_id,name)
                                     VALUES('alice','Alice') RETURNING id""")
        bob = await c.fetchval("""INSERT INTO users(groupme_user_id,name)
                                   VALUES('bob','Bob') RETURNING id""")
        await c.execute("INSERT INTO beers(user_id,group_id) VALUES($1,'g1'),($1,'g2')", alice)
        await c.execute("INSERT INTO user_debts(user_id,group_id) VALUES($1,'g2')", bob)
        return alice, bob


async def test_fresh_migration_and_reconciliation(pg):
    await database.init_db()
    alice, _ = await seed(pg)
    preview = await reconcile_identities()
    assert preview["missing_people"] == 2
    assert preview["missing_memberships"] == 3
    assert await pg.fetchval("SELECT COUNT(*) FROM people") == 0
    applied = await reconcile_identities(apply=True)
    assert applied["missing_people"] == 2
    assert await pg.fetchval("SELECT COUNT(*) FROM workspace_memberships") == 3
    assert (
        await pg.fetchval("SELECT COUNT(*) FROM workspace_memberships WHERE status='observed'") == 3
    )
    assert await pg.fetchval("SELECT COUNT(*) FROM accounts") == 0
    before = await pg.fetch("SELECT * FROM people ORDER BY id")
    assert (await reconcile_identities(apply=True))["missing_people"] == 0
    assert before == await pg.fetch("SELECT * FROM people ORDER BY id")
    await database.init_db()
    entries = [
        (alice, "g1", 1, "round", 0, DrinkType.BEER),
        (alice, "g1", 1, "round", 0, DrinkType.WINE),
    ]
    assert all(await beer_repo.create_batch(entries))
    assert await beer_repo.create_batch(entries) == [None, None]


async def test_upgrade_from_two_backfills_without_changing_legacy_activity(pg):
    async with pg.acquire() as c:
        await c.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT)")
        for version, name, statements in database.SCHEMA_MIGRATIONS[:2]:
            for statement in statements:
                await c.execute(statement)
            await c.execute("INSERT INTO schema_migrations VALUES($1,$2)", version, name)
    await seed(pg)
    before = await pg.fetch("SELECT * FROM beers ORDER BY id")
    await database.init_db()
    await database.init_db()
    assert await pg.fetchval("SELECT COUNT(*) FROM people") == 2
    assert await pg.fetchval("SELECT COUNT(*) FROM workspace_memberships") == 3
    assert before == await pg.fetch("SELECT * FROM beers ORDER BY id")
    assert (await reconcile_identities())["missing_memberships"] == 0


async def test_preserves_claimed_links_profiles_and_inactive_membership(pg):
    await database.init_db()
    await seed(pg)
    await pg.execute("""INSERT INTO people(id,display_name,status) VALUES('claimed','Custom','claimed');
        INSERT INTO external_identities(id,gateway_type,issuer_key,subject_key,person_id,assurance)
        VALUES('custom-id','groupme','groupme','alice','claimed','account_verified');
        INSERT INTO workspace_memberships(id,workspace_id,person_id,display_name,role,status)
        VALUES('custom-member','w1','claimed','Nickname','owner','inactive');""")
    await reconcile_identities(apply=True)
    assert (
        await pg.fetchval("SELECT person_id FROM users WHERE groupme_user_id='alice'") == "claimed"
    )
    assert await pg.fetchval("SELECT display_name FROM people WHERE id='claimed'") == "Custom"
    member = await pg.fetchrow("SELECT * FROM workspace_memberships WHERE id='custom-member'")
    assert (member["role"], member["status"], member["display_name"]) == (
        "owner",
        "inactive",
        "Nickname",
    )
    assert not await pg.fetchval(
        "SELECT EXISTS(SELECT 1 FROM people WHERE id='person:groupme:alice')"
    )


async def test_reports_conflicts_and_never_reactivates_revoked_identity(pg):
    await database.init_db()
    await seed(pg)
    await reconcile_identities(apply=True)
    await pg.execute("""INSERT INTO people(id,display_name) VALUES('different','Different');
        UPDATE users SET person_id='different' WHERE groupme_user_id='alice';
        UPDATE external_identities SET status='revoked' WHERE subject_key='bob';""")
    result = await reconcile_identities(apply=True)
    assert result["conflicts"] == 1
    assert result["blocked"] == 1
    assert (
        await pg.fetchval("SELECT person_id FROM users WHERE groupme_user_id='alice'")
        == "different"
    )
    assert (
        await pg.fetchval("SELECT status FROM external_identities WHERE subject_key='bob'")
        == "revoked"
    )


async def test_pages_and_excludes_unscoped_users_from_memberships(pg):
    await database.init_db()
    await seed(pg)
    await pg.execute("INSERT INTO users(groupme_user_id,name) VALUES('unscoped','No activity')")
    cursor = 0
    scanned = 0
    while True:
        result = await reconcile_identities(apply=True, after_id=cursor, limit=1)
        scanned += result["scanned"]
        cursor = result["next_after_id"]
        if cursor is None:
            break
    assert scanned == 3
    assert await pg.fetchval("SELECT COUNT(*) FROM people") == 3
    assert await pg.fetchval("SELECT COUNT(*) FROM workspace_memberships") == 3


async def test_concurrent_reconciliation_fails_fast(pg):
    await database.init_db()
    async with pg.acquire() as c:
        async with c.transaction():
            await c.fetchval("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
            with pytest.raises(ReconciliationBusy):
                await asyncio.wait_for(reconcile_identities(apply=True), timeout=2)


async def test_failed_page_rolls_back_all_repairs(pg):
    await database.init_db()
    await seed(pg)
    await pg.execute("""ALTER TABLE workspace_memberships ADD CONSTRAINT reject_observation
                        CHECK (status <> 'observed')""")
    with pytest.raises(asyncpg.CheckViolationError):
        await reconcile_identities(apply=True)
    assert await pg.fetchval("SELECT COUNT(*) FROM people") == 0
    assert await pg.fetchval("SELECT COUNT(*) FROM users WHERE person_id IS NOT NULL") == 0
