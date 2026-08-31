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
    (
        3,
        "shadow_global_identities",
        (
            """
            CREATE TABLE IF NOT EXISTS people (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                avatar_url TEXT,
                status TEXT NOT NULL DEFAULT 'provisional',
                canonical_person_id TEXT REFERENCES people(id),
                settings JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL UNIQUE REFERENCES people(id),
                status TEXT NOT NULL DEFAULT 'active',
                settings JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS external_identities (
                id TEXT PRIMARY KEY,
                gateway_type TEXT NOT NULL,
                issuer_key TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                person_id TEXT NOT NULL REFERENCES people(id),
                display_name TEXT,
                avatar_url TEXT,
                assurance TEXT NOT NULL DEFAULT 'gateway_asserted',
                status TEXT NOT NULL DEFAULT 'active',
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                UNIQUE(gateway_type, issuer_key, subject_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workspace_memberships (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                person_id TEXT NOT NULL REFERENCES people(id),
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                status TEXT NOT NULL DEFAULT 'active',
                settings JSONB NOT NULL DEFAULT '{}'::jsonb,
                joined_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                UNIQUE(workspace_id, person_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS person_merges (
                id BIGSERIAL PRIMARY KEY,
                source_person_id TEXT NOT NULL REFERENCES people(id),
                target_person_id TEXT NOT NULL REFERENCES people(id),
                status TEXT NOT NULL DEFAULT 'proposed',
                reason TEXT,
                created_by_account_id TEXT REFERENCES accounts(id),
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                applied_at TIMESTAMP WITH TIME ZONE,
                reverted_at TIMESTAMP WITH TIME ZONE,
                CHECK (source_person_id <> target_person_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_external_identities_person ON external_identities(person_id)",
            "CREATE INDEX IF NOT EXISTS idx_workspace_memberships_person ON workspace_memberships(person_id)",
            "CREATE INDEX IF NOT EXISTS idx_workspace_memberships_workspace ON workspace_memberships(workspace_id)",
            "CREATE INDEX IF NOT EXISTS idx_person_merges_source ON person_merges(source_person_id)",
            "CREATE INDEX IF NOT EXISTS idx_person_merges_target ON person_merges(target_person_id)",
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS person_id TEXT REFERENCES people(id)
            """,
            """
            INSERT INTO people (id, display_name, avatar_url, status)
            SELECT
                'person:groupme:' || groupme_user_id,
                name,
                avatar_url,
                'provisional'
            FROM users
            ON CONFLICT (id) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                avatar_url = EXCLUDED.avatar_url,
                updated_at = NOW()
            """,
            """
            INSERT INTO external_identities (
                id, gateway_type, issuer_key, subject_key, person_id,
                display_name, avatar_url, assurance, status
            )
            SELECT
                'identity:groupme:' || groupme_user_id,
                'groupme',
                'groupme',
                groupme_user_id,
                'person:groupme:' || groupme_user_id,
                name,
                avatar_url,
                'gateway_asserted',
                'active'
            FROM users
            ON CONFLICT (gateway_type, issuer_key, subject_key) DO UPDATE
            SET person_id = EXCLUDED.person_id,
                display_name = EXCLUDED.display_name,
                avatar_url = EXCLUDED.avatar_url,
                assurance = EXCLUDED.assurance,
                status = 'active',
                last_seen_at = NOW()
            """,
            """
            UPDATE users
            SET person_id = 'person:groupme:' || groupme_user_id
            WHERE person_id IS DISTINCT FROM 'person:groupme:' || groupme_user_id
            """,
            """
            WITH observed_memberships AS (
                SELECT DISTINCT b.user_id, g.workspace_id
                FROM beers b
                JOIN groups g ON g.group_id = b.group_id
                WHERE g.workspace_id IS NOT NULL
                UNION
                SELECT DISTINCT d.user_id, g.workspace_id
                FROM user_debts d
                JOIN groups g ON g.group_id = d.group_id
                WHERE g.workspace_id IS NOT NULL
            )
            INSERT INTO workspace_memberships (
                id, workspace_id, person_id, display_name, role, status
            )
            SELECT
                'membership:' || observed.workspace_id || ':person:groupme:' || u.groupme_user_id,
                observed.workspace_id,
                'person:groupme:' || u.groupme_user_id,
                u.name,
                'member',
                'active'
            FROM observed_memberships observed
            JOIN users u ON u.id = observed.user_id
            ON CONFLICT (workspace_id, person_id) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                status = 'active',
                updated_at = NOW()
            """,
            "CREATE INDEX IF NOT EXISTS idx_users_person_id ON users(person_id)",
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
