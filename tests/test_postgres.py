"""Real database regressions, isolated in a new schema per test.

Only BEERBOT_TEST_DATABASE_URL enables these tests; never use DATABASE_URL.
CI always supplies a disposable PostgreSQL service.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.beerbot import database
from src.beerbot.models import DrinkType
from src.beerbot.repositories import GroupRepository, beer_repo, user_repo
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


async def test_fresh_migration_reruns_and_a_retry_is_a_duplicate(pg):
    await database.init_db()
    alice, _ = await seed(pg)
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
    assert await pg.fetchval(
        "SELECT bool_and(person_id='person:groupme:'||groupme_user_id) FROM users"
    )
    assert before == await pg.fetch("SELECT * FROM beers ORDER BY id")
    # Migration 3 filled these; migration 14 drops them with their rows.
    shadow = [
        "gateway_routes",
        "gateway_connections",
        "person_merges",
        "workspace_memberships",
        "external_identities",
        "app_group_events",
    ]
    assert not await pg.fetchval(
        "SELECT count(*) FROM pg_tables WHERE schemaname=current_schema() AND tablename=ANY($1)",
        shadow,
    )


async def test_group_registration_keeps_workspace_for_the_app(pg):
    await database.init_db()
    groups = GroupRepository()  # conftest stubs the shared instance's lookup
    group = await groups.register(group_id="g9", bot_id="bot-1", name="Friends")
    assert (group.workspace_id, group.bot_id, group.name) == ("groupme:g9", "bot-1", "Friends")
    await groups.register(group_id="g9", bot_id="bot-2")
    assert await groups.get_bot_id("g9") == "bot-2"
    assert (await groups.get_by_group_id("g9")).workspace_id == "groupme:g9"
    assert [g.group_id for g in await groups.list_all()] == ["g9"]
    assert await pg.fetchval("SELECT name FROM workspaces WHERE id='groupme:g9'") == "GroupMe g9"
    assert await groups.delete("g9") and not await groups.delete("g9")
    assert await groups.get_by_group_id("g9") is None
    assert await pg.fetchval("SELECT count(*) FROM workspaces") == 1


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


async def test_pace_runs_from_the_first_recent_drink_to_now(pg):
    await database.init_db()
    alice = (await user_repo.get_or_create("alice", "Alice")).id
    # Six beers six days ago, then nothing: one a day, not six a day.
    await pg.execute(
        """INSERT INTO beers(user_id,group_id,quantity,logged_at)
           VALUES($1,'g1',6,now()-interval '6 days'),($1,'g2',3,now()-interval '1 hour')""",
        alice,
    )
    beers, days = await beer_repo.get_rate_stats("g1", days=7)
    assert beers == 6 and days == pytest.approx(6, abs=0.01)
    assert await beer_repo.get_rate_stats("g2", days=7) == (3, 1.0)  # A burst: at least a day.
    assert await beer_repo.get_rate_stats("g2", days=7, drink_type=DrinkType.WINE) == (0, 0.0)
    ctx = ToolContext("g1", "m1", "alice", "Alice", None, [])
    countdown = next(t for t in create_tools(ctx) if t.__name__ == "get_million_countdown")
    assert (await countdown())["pace_7d"] == 1.0


async def test_leaderboard_and_removal_with_and_without_a_type(pg):
    await database.init_db()
    alice = (await user_repo.get_or_create("alice", "Alice")).id
    bob = (await user_repo.get_or_create("bob", "Bob")).id
    await pg.execute(
        """INSERT INTO beers(user_id,group_id,quantity,drink_type,logged_at)
           VALUES($1,'g1',5,'beer',now()-interval '1 hour'),($1,'g1',2,'wine',now()),
                 ($2,'g1',4,'beer',now())""",
        alice,
        bob,
    )
    board = await beer_repo.get_leaderboard_with_breakdown("g1", limit=5)
    assert [(name, total) for name, total, _ in board] == [("Alice", 7), ("Bob", 4)]
    assert board[0][2] == {"beer": 5, "wine": 2, "cocktail": 0, "claw": 0}
    wine = await beer_repo.get_leaderboard_with_breakdown("g1", DrinkType.WINE, 5)
    assert [(name, total) for name, total, _ in wine] == [("Alice", 2)]  # Bob has none.
    assert await beer_repo.remove_beers(alice, "g1", 3, DrinkType.BEER) == 3
    assert await beer_repo.remove_beers(alice, "g1", 3) == 3  # Newest first: 2 wine, 1 beer.
    assert await beer_repo.get_user_total(alice, "g1") == 1


async def test_the_unused_dev_requests_table_is_dropped(pg):
    # It held request text forever, past the three-day retention.
    await database.init_db()
    assert await pg.fetchval("SELECT to_regclass('dev_requests')") is None
    assert await pg.fetchval("SELECT MAX(version) FROM schema_migrations") >= 13
    await database.init_db()


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
