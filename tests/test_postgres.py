"""Real database regressions, isolated in a new schema per test.

Only BEERBOT_TEST_DATABASE_URL enables these tests; never use DATABASE_URL.
CI always supplies a disposable PostgreSQL service.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from src.beerbot import database
from src.beerbot.models import DrinkType
from src.beerbot.reconciliation import LOCK_KEY, ReconciliationBusy, reconcile_identities
from src.beerbot.repositories import beer_repo, user_repo
from src.beerbot.tools import ToolContext, create_tools


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
    for _ in range(2):  # One drink per message, user, and type: the retry is a duplicate.
        logged = [
            await beer_repo.create(alice, "g1", message_id="round", drink_type=kind)
            for kind in (DrinkType.BEER, DrinkType.WINE)
        ]
    assert logged == [None, None]
    assert await pg.fetchval("SELECT COUNT(*) FROM beers WHERE message_id='round'") == 2


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


async def test_drinks_and_zyn_count_from_when_they_were_sent(pg):
    # A backlog processed a day late still lands on the day it was sent; a clock ahead of ours
    # never logs into the future; a retry is still a duplicate.
    await database.init_db()

    def tools(message_id: str, sent_at: datetime):
        ctx = ToolContext("g1", message_id, "alice", "Alice", None, [], sent_at=sent_at)
        return {tool.__name__: tool for tool in create_tools(ctx)}

    yesterday = datetime.now(UTC) - timedelta(days=1)
    await tools("m1", yesterday)["log_drinks"](2, "beer")
    await tools("m2", yesterday)["log_split_the_g"]()
    await tools("m3", yesterday)["log_zyn"](3)
    assert (await tools("m1", yesterday)["log_drinks"](2, "beer"))["results"][0]["status"] == (
        "duplicate"
    )
    assert await pg.fetchval("SELECT array_agg(DISTINCT logged_at) FROM beers") == [yesterday]
    assert await pg.fetchval("SELECT logged_at FROM zyn_logs") == yesterday
    today = tools("m4", datetime.now(UTC))
    assert (await today["get_today_stats"]())["total"] == 0
    assert (await today["get_zyn_stats"]())["entries"][0]["today"] == 0
    await tools("m5", datetime.now(UTC) + timedelta(hours=3))["log_drinks"](1, "wine")
    assert await pg.fetchval("SELECT logged_at <= now() FROM beers WHERE drink_type='wine'")


async def test_members_sharing_a_display_name_keep_their_own_stats(pg):
    await database.init_db()
    for uid in ("alex-1", "alex-2"):
        await user_repo.get_or_create(uid, "Alex")
    ids = dict(await pg.fetch("SELECT groupme_user_id, id FROM users"))
    await pg.execute(
        """INSERT INTO beers(user_id,group_id,quantity,drink_type,logged_at)
           VALUES($1,'g1',3,'beer',now()),($1,'g1',1,'wine',now()-interval '30 days'),
                 ($2,'g1',5,'beer',now())""",
        ids["alex-1"],
        ids["alex-2"],
    )

    def tools(sender: str):
        ctx = ToolContext("g1", f"m-{sender}", sender, "Alex", None, [])
        return {tool.__name__: tool for tool in create_tools(ctx)}

    stats = await tools("alex-1")["get_user_stats"]()
    assert {k: stats[k] for k in ("total", "today", "this_week", "breakdown")} == {
        "total": 4,
        "today": 3,
        "this_week": 3,
        "breakdown": {"beer": 3, "wine": 1},
    }
    assert stats["last_drink_at"] is not None
    assert (await tools("alex-2")["get_user_stats"]())["today"] == 5
    recent = await tools("alex-2")["get_recent_drinks"](target_user_id="alex-2")
    assert recent["total"] == 5
    empty = await tools("new")["get_user_stats"]()
    assert (empty["total"], empty["breakdown"], empty["last_drink_at"]) == (0, {}, None)


async def test_chat_queries_survive_a_migration_adding_a_column(pg):
    # Prepared statements outlive DDL (Neon's pooler caches them by text). A star result type
    # changes with the table and fails the cached plan inside a transaction (2026-11-25 outage).
    await database.init_db()
    async with pg.acquire() as conn:
        with database.bind_execution(conn):
            user = await user_repo.get_or_create("alice", "Alice")
            assert await user_repo.get_by_groupme_id("alice") == user
            assert await beer_repo.create(user.id, "g1", message_id="m1")
            await pg.execute("ALTER TABLE users ADD COLUMN nickname TEXT")
            await pg.execute("ALTER TABLE beers ADD COLUMN note TEXT")
            async with conn.transaction():
                assert (await user_repo.get_or_create("alice", "Alice")).id == user.id
                assert await user_repo.get_by_groupme_id("alice")
                assert await beer_repo.create(user.id, "g1", message_id="m2")
