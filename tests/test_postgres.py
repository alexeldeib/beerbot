"""Real database regressions, isolated in a new schema per test.

Only BEERBOT_TEST_DATABASE_URL enables these tests; never use DATABASE_URL.
CI always supplies a disposable PostgreSQL service.
"""

from src.beerbot import database
from src.beerbot.models import DrinkType
from src.beerbot.repositories import GroupRepository, beer_repo


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


async def test_fresh_migration_reruns_and_batch_is_idempotent(pg):
    await database.init_db()
    alice, _ = await seed(pg)
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
