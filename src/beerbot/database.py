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
