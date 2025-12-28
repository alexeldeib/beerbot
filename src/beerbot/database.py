"""Database connection and initialization with asyncpg."""

import asyncpg

from .config import settings

# Connection pool
_pool: asyncpg.Pool | None = None


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
    """Initialize database schema."""
    pool = await get_pool()

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                groupme_user_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                avatar_url TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS beers (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                group_id TEXT NOT NULL,
                quantity INTEGER DEFAULT 1,
                logged_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                message_id TEXT,
                split_the_g INTEGER DEFAULT 0
            )
        """)

        # Create indexes if they don't exist
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_beers_user_id ON beers(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_beers_group_id ON beers(group_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_beers_logged_at ON beers(logged_at)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_groupme_user_id ON users(groupme_user_id)
        """)

        # Unique constraint for idempotency: same message can't log beers twice for same user
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_beers_message_user
            ON beers(message_id, user_id)
            WHERE message_id IS NOT NULL
        """)

        # Add split_the_g column if it doesn't exist (migration for existing DBs)
        await conn.execute("""
            ALTER TABLE beers ADD COLUMN IF NOT EXISTS split_the_g INTEGER DEFAULT 0
        """)

        # Index for split_the_g leaderboard queries
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_beers_split_the_g
            ON beers(split_the_g) WHERE split_the_g > 0
        """)

        # Add drink_type column (migration for multi-drink support)
        await conn.execute("""
            ALTER TABLE beers ADD COLUMN IF NOT EXISTS drink_type TEXT DEFAULT 'beer'
        """)

        # Index for drink_type filtering
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_beers_drink_type ON beers(drink_type)
        """)

        # Simple debt tracking per user per group (no creditor - just total owed)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_debts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                group_id TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(user_id, group_id)
            )
        """)

        # Index for debt queries
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_debts_group ON user_debts(group_id)
        """)

        # Groups table for multi-group support (maps group_id to bot_id)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                group_id TEXT PRIMARY KEY,
                bot_id TEXT NOT NULL,
                name TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
