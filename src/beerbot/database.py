"""Database connection and versioned schema migrations with asyncpg."""

import asyncpg

from .config import settings

# Connection pool
_pool: asyncpg.Pool | None = None


SCHEMA_MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "baseline",
        (
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                groupme_user_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                avatar_url TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS beers (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                group_id TEXT NOT NULL,
                quantity INTEGER DEFAULT 1,
                logged_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                message_id TEXT,
                split_the_g INTEGER DEFAULT 0,
                drink_type TEXT NOT NULL DEFAULT 'beer'
            )
            """,
            # Upgrade older installations before creating indexes that use
            # these columns. PostgreSQL DDL is transactional here.
            "ALTER TABLE beers ADD COLUMN IF NOT EXISTS split_the_g INTEGER DEFAULT 0",
            "ALTER TABLE beers ADD COLUMN IF NOT EXISTS drink_type TEXT DEFAULT 'beer'",
            "UPDATE beers SET drink_type = 'beer' WHERE drink_type IS NULL",
            "ALTER TABLE beers ALTER COLUMN drink_type SET NOT NULL",
            "DROP INDEX IF EXISTS idx_beers_message_user",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_beers_message_user_type
            ON beers(message_id, user_id, drink_type)
            WHERE message_id IS NOT NULL
            """,
            "CREATE INDEX IF NOT EXISTS idx_beers_user_id ON beers(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_beers_group_id ON beers(group_id)",
            "CREATE INDEX IF NOT EXISTS idx_beers_logged_at ON beers(logged_at)",
            "CREATE INDEX IF NOT EXISTS idx_users_groupme_user_id ON users(groupme_user_id)",
            "CREATE INDEX IF NOT EXISTS idx_beers_drink_type ON beers(drink_type)",
            """
            CREATE INDEX IF NOT EXISTS idx_beers_split_the_g
            ON beers(split_the_g) WHERE split_the_g > 0
            """,
            """
            CREATE TABLE IF NOT EXISTS user_debts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                group_id TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(user_id, group_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_user_debts_group ON user_debts(group_id)",
            """
            CREATE TABLE IF NOT EXISTS groups (
                group_id TEXT PRIMARY KEY,
                bot_id TEXT NOT NULL,
                name TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS weekly_recaps (
                id SERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                week_start DATE NOT NULL,
                sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(group_id, week_start)
            )
            """,
        ),
    ),
    (
        2,
        "workspace_gateway_routing",
        (
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'America/New_York',
                settings JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS gateway_connections (
                id TEXT PRIMARY KEY,
                gateway_type TEXT NOT NULL,
                name TEXT,
                credential_ref TEXT,
                config JSONB NOT NULL DEFAULT '{}'::jsonb,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS gateway_routes (
                id TEXT PRIMARY KEY,
                gateway_type TEXT NOT NULL,
                route_key TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                gateway_connection_id TEXT NOT NULL REFERENCES gateway_connections(id),
                external_conversation_id TEXT,
                name TEXT,
                config JSONB NOT NULL DEFAULT '{}'::jsonb,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                UNIQUE(gateway_type, route_key)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_gateway_routes_workspace
            ON gateway_routes(workspace_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_gateway_routes_connection
            ON gateway_routes(gateway_connection_id)
            """,
            """
            ALTER TABLE groups
            ADD COLUMN IF NOT EXISTS workspace_id TEXT REFERENCES workspaces(id)
            """,
            """
            INSERT INTO workspaces (id, name)
            SELECT
                'groupme:' || group_id,
                COALESCE(NULLIF(name, ''), 'GroupMe ' || group_id)
            FROM groups
            ON CONFLICT (id) DO UPDATE
            SET name = EXCLUDED.name,
                updated_at = NOW()
            """,
            """
            INSERT INTO gateway_connections (
                id, gateway_type, name, credential_ref, config, status
            )
            SELECT
                'groupme-bot:' || group_id,
                'groupme',
                COALESCE(NULLIF(name, ''), 'GroupMe bot ' || group_id),
                'legacy:groups/' || group_id || '/bot_id',
                jsonb_build_object('legacy_group_id', group_id),
                'active'
            FROM groups
            ON CONFLICT (id) DO UPDATE
            SET name = EXCLUDED.name,
                credential_ref = EXCLUDED.credential_ref,
                config = EXCLUDED.config,
                status = 'active',
                updated_at = NOW()
            """,
            """
            INSERT INTO gateway_routes (
                id, gateway_type, route_key, workspace_id,
                gateway_connection_id, external_conversation_id, name, status
            )
            SELECT
                'groupme-route:' || group_id,
                'groupme',
                group_id,
                'groupme:' || group_id,
                'groupme-bot:' || group_id,
                group_id,
                name,
                'active'
            FROM groups
            ON CONFLICT (gateway_type, route_key) DO UPDATE
            SET workspace_id = EXCLUDED.workspace_id,
                gateway_connection_id = EXCLUDED.gateway_connection_id,
                external_conversation_id = EXCLUDED.external_conversation_id,
                name = EXCLUDED.name,
                status = 'active',
                updated_at = NOW()
            """,
            """
            UPDATE groups
            SET workspace_id = 'groupme:' || group_id
            WHERE workspace_id IS DISTINCT FROM 'groupme:' || group_id
            """,
            "CREATE INDEX IF NOT EXISTS idx_groups_workspace_id ON groups(workspace_id)",
        ),
    ),
)


async def get_pool() -> asyncpg.Pool:
    """Get or create the database connection pool."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=1,
            max_size=10,
        )
    return _pool


async def close_pool() -> None:
    """Close the database connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_db() -> None:
    """Apply pending schema migrations atomically and in version order."""
    pool = await get_pool()

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
        """)

        for version, name, statements in SCHEMA_MIGRATIONS:
            async with conn.transaction():
                # Multiple application instances may start together. Serialize
                # migration checks so only one applies each version.
                await conn.execute("LOCK TABLE schema_migrations IN EXCLUSIVE MODE")
                applied = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version = $1)",
                    version,
                )
                if applied:
                    continue

                for statement in statements:
                    await conn.execute(statement)

                await conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                    version,
                    name,
                )
