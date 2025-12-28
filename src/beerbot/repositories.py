"""Data access layer for users and beers."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg

from .database import get_pool
from .models import Beer, DebtLeaderboardEntry, DrinkType, Group, GroupStats, SplitGGroupStats, SplitGUserStats, User, UserDebt, UserStats

# Use Eastern time for all date calculations
EASTERN = ZoneInfo("America/New_York")


class UserRepository:
    """Repository for user operations."""

    async def get_or_create(
        self,
        groupme_user_id: str,
        name: str,
        avatar_url: str | None = None,
    ) -> User:
        """Get existing user or create new one. Updates name/avatar if changed.

        Uses ON CONFLICT for atomic upsert - safe for concurrent requests.
        """
        pool = await get_pool()

        async with pool.acquire() as conn:
            # Atomic upsert: insert or update on conflict
            row = await conn.fetchrow(
                """
                INSERT INTO users (groupme_user_id, name, avatar_url)
                VALUES ($1, $2, $3)
                ON CONFLICT (groupme_user_id) DO UPDATE
                SET name = EXCLUDED.name,
                    avatar_url = EXCLUDED.avatar_url,
                    updated_at = NOW()
                RETURNING *
                """,
                groupme_user_id,
                name,
                avatar_url,
            )
            return User(**dict(row))

    async def get_by_id(self, user_id: int) -> User | None:
        """Get user by internal ID."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            return User(**dict(row)) if row else None

    async def get_by_groupme_id(self, groupme_user_id: str) -> User | None:
        """Get user by GroupMe user ID."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE groupme_user_id = $1",
                groupme_user_id,
            )
            return User(**dict(row)) if row else None


class BeerRepository:
    """Repository for beer operations."""

    async def create(
        self,
        user_id: int,
        group_id: str,
        quantity: int = 1,
        message_id: str | None = None,
        split_the_g: int = 0,
        drink_type: DrinkType = DrinkType.BEER,
    ) -> Beer | None:
        """Log a drink entry.

        Returns the created Beer/Drink, or None if this was a duplicate (same message_id + user_id).
        This provides idempotency - processing the same message twice won't double-count.
        """
        pool = await get_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO beers (user_id, group_id, quantity, message_id, split_the_g, drink_type)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (message_id, user_id) WHERE message_id IS NOT NULL
                DO NOTHING
                RETURNING *
                """,
                user_id,
                group_id,
                quantity,
                message_id,
                split_the_g,
                drink_type.value,
            )
            return Beer(**dict(row)) if row else None

    async def get_user_total(self, user_id: int, group_id: str | None = None) -> int:
        """Get total beers for a user, optionally filtered by group."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            if group_id:
                result = await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(quantity), 0)
                    FROM beers WHERE user_id = $1 AND group_id = $2
                    """,
                    user_id,
                    group_id,
                )
            else:
                result = await conn.fetchval(
                    "SELECT COALESCE(SUM(quantity), 0) FROM beers WHERE user_id = $1",
                    user_id,
                )
            return int(result)

    async def get_user_total_by_type(
        self, user_id: int, group_id: str, drink_type: DrinkType
    ) -> int:
        """Get total drinks of a specific type for a user in a group."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            result = await conn.fetchval(
                """
                SELECT COALESCE(SUM(quantity), 0)
                FROM beers WHERE user_id = $1 AND group_id = $2 AND drink_type = $3
                """,
                user_id,
                group_id,
                drink_type.value,
            )
            return int(result)

    async def get_group_stats(
        self,
        group_id: str,
        since: datetime | None = None,
        drink_type: DrinkType | None = None,
    ) -> GroupStats:
        """Get statistics for a group, optionally filtered by drink type."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            # Build WHERE clauses
            params: list = [group_id]
            where_parts = ["b.group_id = $1"]
            param_idx = 2

            if since:
                where_parts.append(f"b.logged_at >= ${param_idx}")
                params.append(since)
                param_idx += 1

            if drink_type:
                where_parts.append(f"b.drink_type = ${param_idx}")
                params.append(drink_type.value)
                param_idx += 1

            where_clause = " AND ".join(where_parts)

            # Total query
            total_query = f"""
                SELECT COALESCE(SUM(b.quantity), 0)
                FROM beers b WHERE {where_clause}
            """
            total = await conn.fetchval(total_query, *params)

            # User stats query
            user_stats_query = f"""
                SELECT u.name, SUM(b.quantity) as total, MAX(b.logged_at) as last_beer
                FROM beers b
                JOIN users u ON b.user_id = u.id
                WHERE {where_clause}
                GROUP BY u.id, u.name
                ORDER BY total DESC
            """
            rows = await conn.fetch(user_stats_query, *params)

            user_stats = [
                UserStats(
                    name=row["name"],
                    total_beers=int(row["total"]),
                    last_beer_at=row["last_beer"],
                )
                for row in rows
            ]

            # Get drink type breakdown (always unfiltered by type to show full breakdown)
            breakdown_params: list = [group_id]
            breakdown_where = "group_id = $1"
            if since:
                breakdown_where += " AND logged_at >= $2"
                breakdown_params.append(since)

            type_query = f"""
                SELECT drink_type, COALESCE(SUM(quantity), 0) as count
                FROM beers WHERE {breakdown_where}
                GROUP BY drink_type
            """
            type_rows = await conn.fetch(type_query, *breakdown_params)

            drink_type_counts = {row["drink_type"]: int(row["count"]) for row in type_rows}

            # Determine period description
            if since is None:
                period = "all time"
            else:
                now = datetime.now(EASTERN)
                diff = now - since
                if diff <= timedelta(days=1):
                    period = "today"
                elif diff <= timedelta(weeks=1):
                    period = "this week"
                else:
                    period = f"since {since.strftime('%Y-%m-%d')}"

            return GroupStats(
                total_beers=int(total),
                unique_drinkers=len(user_stats),
                user_stats=user_stats,
                period_description=period,
                drink_type_counts=drink_type_counts,
            )

    async def get_user_stats_in_group(
        self,
        user_id: int,
        group_id: str,
    ) -> UserStats | None:
        """Get stats for a specific user in a group."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT u.name, SUM(b.quantity) as total, MAX(b.logged_at) as last_beer
                FROM beers b
                JOIN users u ON b.user_id = u.id
                WHERE b.user_id = $1 AND b.group_id = $2
                GROUP BY u.id, u.name
                """,
                user_id,
                group_id,
            )

            if not row or row["total"] is None:
                return None

            return UserStats(
                name=row["name"],
                total_beers=int(row["total"]),
                last_beer_at=row["last_beer"],
            )

    async def delete_last_beer(self, user_id: int, group_id: str) -> int:
        """Delete the most recent beer entry for a user in a group.

        Returns the quantity of beers that were deleted (0 if none found).
        """
        pool = await get_pool()

        async with pool.acquire() as conn:
            # Get the last entry's quantity and delete it
            row = await conn.fetchrow(
                """
                DELETE FROM beers
                WHERE id = (
                    SELECT id FROM beers
                    WHERE user_id = $1 AND group_id = $2
                    ORDER BY logged_at DESC
                    LIMIT 1
                )
                RETURNING quantity
                """,
                user_id,
                group_id,
            )
            return row["quantity"] if row else 0

    async def remove_beers(self, user_id: int, group_id: str, quantity: int) -> int:
        """Remove a specific number of beers from a user in a group.

        Decrements quantity on most recent entries first. Only deletes entries
        when their quantity reaches 0.
        Returns the actual number of beers removed (may be less if user has fewer).
        """
        pool = await get_pool()
        removed = 0

        async with pool.acquire() as conn:
            while removed < quantity:
                # Find most recent entry
                row = await conn.fetchrow(
                    """
                    SELECT id, quantity FROM beers
                    WHERE user_id = $1 AND group_id = $2
                    ORDER BY logged_at DESC
                    LIMIT 1
                    """,
                    user_id,
                    group_id,
                )
                if not row:
                    break  # No more entries

                entry_id = row["id"]
                entry_qty = row["quantity"]
                needed = quantity - removed

                if entry_qty > needed:
                    # Decrement quantity on this entry
                    await conn.execute(
                        "UPDATE beers SET quantity = quantity - $1 WHERE id = $2",
                        needed,
                        entry_id,
                    )
                    removed += needed
                else:
                    # Delete entire entry and continue
                    await conn.execute("DELETE FROM beers WHERE id = $1", entry_id)
                    removed += entry_qty

        return removed

    async def remove_beers_by_type(
        self, user_id: int, group_id: str, quantity: int, drink_type: DrinkType
    ) -> int:
        """Remove a specific number of drinks of a specific type from a user.

        Decrements quantity on most recent entries of that type first.
        Returns the actual number removed (may be less if user has fewer).
        """
        pool = await get_pool()
        removed = 0

        async with pool.acquire() as conn:
            while removed < quantity:
                # Find most recent entry of this drink type
                row = await conn.fetchrow(
                    """
                    SELECT id, quantity FROM beers
                    WHERE user_id = $1 AND group_id = $2 AND drink_type = $3
                    ORDER BY logged_at DESC
                    LIMIT 1
                    """,
                    user_id,
                    group_id,
                    drink_type.value,
                )
                if not row:
                    break  # No more entries of this type

                entry_id = row["id"]
                entry_qty = row["quantity"]
                needed = quantity - removed

                if entry_qty > needed:
                    # Decrement quantity on this entry
                    await conn.execute(
                        "UPDATE beers SET quantity = quantity - $1 WHERE id = $2",
                        needed,
                        entry_id,
                    )
                    removed += needed
                else:
                    # Delete entire entry and continue
                    await conn.execute("DELETE FROM beers WHERE id = $1", entry_id)
                    removed += entry_qty

        return removed

    async def create_batch(
        self,
        entries: list[tuple[int, str, int, str | None, int, DrinkType]],
    ) -> list[Beer | None]:
        """Log multiple drink entries in a single transaction.

        Args:
            entries: List of (user_id, group_id, quantity, message_id, split_the_g, drink_type) tuples

        Returns:
            List of Beer/Drink objects (or None for duplicates), in same order as input.
            All entries are committed together or none are (atomic).
        """
        if not entries:
            return []

        pool = await get_pool()
        results: list[Beer | None] = []

        async with pool.acquire() as conn:
            async with conn.transaction():
                for user_id, group_id, quantity, message_id, split_the_g, drink_type in entries:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO beers (user_id, group_id, quantity, message_id, split_the_g, drink_type)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (message_id, user_id) WHERE message_id IS NOT NULL
                        DO NOTHING
                        RETURNING *
                        """,
                        user_id,
                        group_id,
                        quantity,
                        message_id,
                        split_the_g,
                        drink_type.value,
                    )
                    results.append(Beer(**dict(row)) if row else None)

        return results

    async def get_rate_stats(
        self,
        group_id: str,
        days: int = 30,
        drink_type: DrinkType | None = None,
    ) -> tuple[int, int, float]:
        """Get rate statistics for a group over a period.

        Args:
            group_id: The group to get stats for
            days: Number of days to look back
            drink_type: Optional filter by drink type

        Returns:
            tuple: (total_beers_all_time, beers_in_period, days_with_activity)
        """
        pool = await get_pool()

        async with pool.acquire() as conn:
            # Build WHERE clause for drink type filter
            type_filter = ""
            type_param: list = []
            if drink_type is not None:
                type_filter = " AND drink_type = $2"
                type_param = [drink_type.value]

            # Get total all time (with optional type filter)
            total_all_time = await conn.fetchval(
                f"SELECT COALESCE(SUM(quantity), 0) FROM beers WHERE group_id = $1{type_filter}",
                group_id,
                *type_param,
            )

            # Get drinks and date range for period (Eastern time)
            since = datetime.now(EASTERN) - timedelta(days=days)
            # Adjust parameter numbers for the period query
            if drink_type is not None:
                period_query = """
                    SELECT
                        COALESCE(SUM(quantity), 0) as period_total,
                        MIN(logged_at) as first_beer,
                        MAX(logged_at) as last_beer
                    FROM beers
                    WHERE group_id = $1 AND logged_at >= $2 AND drink_type = $3
                """
                row = await conn.fetchrow(period_query, group_id, since, drink_type.value)
            else:
                period_query = """
                    SELECT
                        COALESCE(SUM(quantity), 0) as period_total,
                        MIN(logged_at) as first_beer,
                        MAX(logged_at) as last_beer
                    FROM beers
                    WHERE group_id = $1 AND logged_at >= $2
                """
                row = await conn.fetchrow(period_query, group_id, since)

            period_total = int(row["period_total"])
            first_beer = row["first_beer"]
            last_beer = row["last_beer"]

            # Calculate actual days with data
            if first_beer and last_beer:
                actual_days = max(1.0, (last_beer - first_beer).total_seconds() / 86400)
            else:
                actual_days = 0.0

            return int(total_all_time), period_total, actual_days

    async def get_split_g_stats(self, group_id: str) -> SplitGGroupStats:
        """Get split-the-G statistics for a group."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            # Total splits
            total = await conn.fetchval(
                "SELECT COALESCE(SUM(split_the_g), 0) FROM beers WHERE group_id = $1",
                group_id,
            )

            # Per-user stats
            rows = await conn.fetch(
                """
                SELECT u.name, SUM(b.split_the_g) as total, MAX(b.logged_at) as last_split
                FROM beers b
                JOIN users u ON b.user_id = u.id
                WHERE b.group_id = $1 AND b.split_the_g > 0
                GROUP BY u.id, u.name
                ORDER BY total DESC
                """,
                group_id,
            )

            user_stats = [
                SplitGUserStats(
                    name=row["name"],
                    split_the_g_count=int(row["total"]),
                    last_split_at=row["last_split"],
                )
                for row in rows
            ]

            return SplitGGroupStats(
                total_splits=int(total),
                unique_splitters=len(user_stats),
                user_stats=user_stats,
            )

    async def get_user_split_g_total(self, user_id: int, group_id: str | None = None) -> int:
        """Get total split-the-G count for a user."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            if group_id:
                result = await conn.fetchval(
                    "SELECT COALESCE(SUM(split_the_g), 0) FROM beers WHERE user_id = $1 AND group_id = $2",
                    user_id,
                    group_id,
                )
            else:
                result = await conn.fetchval(
                    "SELECT COALESCE(SUM(split_the_g), 0) FROM beers WHERE user_id = $1",
                    user_id,
                )
            return int(result)

    async def remove_splits(self, user_id: int, group_id: str, quantity: int) -> int:
        """Remove a specific number of split-the-G counts from a user.

        Decrements split_the_g on most recent entries first.
        Returns the actual number of splits removed.
        """
        pool = await get_pool()
        removed = 0

        async with pool.acquire() as conn:
            # Get entries with splits, ordered by most recent
            while removed < quantity:
                # Find most recent entry with split_the_g > 0
                row = await conn.fetchrow(
                    """
                    SELECT id, split_the_g FROM beers
                    WHERE user_id = $1 AND group_id = $2 AND split_the_g > 0
                    ORDER BY logged_at DESC
                    LIMIT 1
                    """,
                    user_id,
                    group_id,
                )
                if not row:
                    break  # No more entries with splits

                entry_id = row["id"]
                entry_splits = row["split_the_g"]
                needed = quantity - removed

                if entry_splits <= needed:
                    # Remove all splits from this entry
                    await conn.execute(
                        "UPDATE beers SET split_the_g = 0 WHERE id = $1",
                        entry_id,
                    )
                    removed += entry_splits
                else:
                    # Partially remove splits from this entry
                    await conn.execute(
                        "UPDATE beers SET split_the_g = split_the_g - $1 WHERE id = $2",
                        needed,
                        entry_id,
                    )
                    removed += needed

        return removed

    async def get_group_total_by_type(
        self,
        group_id: str,
        drink_type: DrinkType | None = None,
    ) -> int:
        """Get total drinks for a group, optionally filtered by type.

        Args:
            group_id: The group to query
            drink_type: Filter by drink type, or None for all drinks

        Returns:
            Total drink count
        """
        pool = await get_pool()

        async with pool.acquire() as conn:
            if drink_type is None:
                result = await conn.fetchval(
                    "SELECT COALESCE(SUM(quantity), 0) FROM beers WHERE group_id = $1",
                    group_id,
                )
            else:
                result = await conn.fetchval(
                    "SELECT COALESCE(SUM(quantity), 0) FROM beers WHERE group_id = $1 AND drink_type = $2",
                    group_id,
                    drink_type.value,
                )
            return int(result)

    async def get_user_stats_by_type(
        self,
        user_id: int,
        group_id: str,
    ) -> dict[DrinkType, int]:
        """Get breakdown of drinks by type for a user in a group.

        Returns:
            Dict mapping DrinkType to count (only includes types with > 0)
        """
        pool = await get_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT drink_type, SUM(quantity) as total
                FROM beers
                WHERE user_id = $1 AND group_id = $2
                GROUP BY drink_type
                """,
                user_id,
                group_id,
            )

            result: dict[DrinkType, int] = {}
            for row in rows:
                drink_type_str = row["drink_type"] or "beer"
                drink_type = DrinkType.from_string(drink_type_str)
                result[drink_type] = int(row["total"])

            return result

    async def get_leaderboard_with_breakdown(
        self,
        group_id: str,
        drink_type: DrinkType | None = None,
        limit: int = 5,
    ) -> list[tuple[str, int, dict[str, int]]]:
        """Get leaderboard with per-user drink type breakdown.

        Args:
            group_id: The group to query
            drink_type: Optional filter - if set, users are ranked by this type only
            limit: Max number of users to return

        Returns:
            List of (name, total_for_ranking, {type: count}) tuples
        """
        pool = await get_pool()

        async with pool.acquire() as conn:
            if drink_type:
                # When filtered, rank by specific type
                query = """
                    SELECT u.name,
                           SUM(CASE WHEN b.drink_type = $2 THEN b.quantity ELSE 0 END) as total,
                           SUM(CASE WHEN b.drink_type = 'beer' THEN b.quantity ELSE 0 END) as beer_count,
                           SUM(CASE WHEN b.drink_type = 'wine' THEN b.quantity ELSE 0 END) as wine_count,
                           SUM(CASE WHEN b.drink_type = 'cocktail' THEN b.quantity ELSE 0 END) as cocktail_count,
                           SUM(CASE WHEN b.drink_type = 'claw' THEN b.quantity ELSE 0 END) as claw_count
                    FROM beers b
                    JOIN users u ON b.user_id = u.id
                    WHERE b.group_id = $1
                    GROUP BY u.id, u.name
                    HAVING SUM(CASE WHEN b.drink_type = $2 THEN b.quantity ELSE 0 END) > 0
                    ORDER BY total DESC
                    LIMIT $3
                """
                rows = await conn.fetch(query, group_id, drink_type.value, limit)
            else:
                # Unfiltered - rank by total of all types
                query = """
                    SELECT u.name,
                           SUM(b.quantity) as total,
                           SUM(CASE WHEN b.drink_type = 'beer' THEN b.quantity ELSE 0 END) as beer_count,
                           SUM(CASE WHEN b.drink_type = 'wine' THEN b.quantity ELSE 0 END) as wine_count,
                           SUM(CASE WHEN b.drink_type = 'cocktail' THEN b.quantity ELSE 0 END) as cocktail_count,
                           SUM(CASE WHEN b.drink_type = 'claw' THEN b.quantity ELSE 0 END) as claw_count
                    FROM beers b
                    JOIN users u ON b.user_id = u.id
                    WHERE b.group_id = $1
                    GROUP BY u.id, u.name
                    ORDER BY total DESC
                    LIMIT $2
                """
                rows = await conn.fetch(query, group_id, limit)

            results = []
            for row in rows:
                breakdown = {
                    "beer": int(row["beer_count"]),
                    "wine": int(row["wine_count"]),
                    "cocktail": int(row["cocktail_count"]),
                    "claw": int(row["claw_count"]),
                }
                results.append((row["name"], int(row["total"]), breakdown))

            return results


class DebtRepository:
    """Repository for simple debt tracking (beers owed to the group)."""

    async def add_debt(self, user_id: int, group_id: str, amount: int) -> int:
        """Add to a user's debt. Returns new total."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO user_debts (user_id, group_id, amount)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, group_id) DO UPDATE
                SET amount = user_debts.amount + $3, updated_at = NOW()
                RETURNING amount
                """,
                user_id,
                group_id,
                amount,
            )
            return row["amount"]

    async def reduce_debt(self, user_id: int, group_id: str, amount: int) -> int:
        """Reduce a user's debt (when they drink). Returns amount reduced."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            # Get current debt
            current = await conn.fetchval(
                "SELECT amount FROM user_debts WHERE user_id = $1 AND group_id = $2",
                user_id,
                group_id,
            )

            if not current or current <= 0:
                return 0

            # Reduce by min(amount, current) - can't go below 0
            reduction = min(amount, current)
            await conn.execute(
                """
                UPDATE user_debts
                SET amount = amount - $1, updated_at = NOW()
                WHERE user_id = $2 AND group_id = $3
                """,
                reduction,
                user_id,
                group_id,
            )
            return reduction

    async def get_debt(self, user_id: int, group_id: str) -> int:
        """Get a user's current debt."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT COALESCE(amount, 0) FROM user_debts WHERE user_id = $1 AND group_id = $2",
                user_id,
                group_id,
            )
            return result or 0

    async def get_debt_leaderboard(self, group_id: str, limit: int = 10) -> list[DebtLeaderboardEntry]:
        """Get leaderboard of who owes the most beers."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT u.name, d.amount
                FROM user_debts d
                JOIN users u ON d.user_id = u.id
                WHERE d.group_id = $1 AND d.amount > 0
                ORDER BY d.amount DESC
                LIMIT $2
                """,
                group_id,
                limit,
            )

            return [
                DebtLeaderboardEntry(name=row["name"], amount=row["amount"])
                for row in rows
            ]


class GroupRepository:
    """Repository for group-to-bot mapping operations."""

    async def get_by_group_id(self, group_id: str) -> Group | None:
        """Get a group by its GroupMe group ID."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM groups WHERE group_id = $1",
                group_id,
            )
            return Group(**dict(row)) if row else None

    async def get_bot_id(self, group_id: str) -> str | None:
        """Get the bot_id for a group. Returns None if group not registered."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT bot_id FROM groups WHERE group_id = $1",
                group_id,
            )
            return result

    async def register(
        self,
        group_id: str,
        bot_id: str,
        name: str | None = None,
    ) -> Group:
        """Register a new group or update existing one."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO groups (group_id, bot_id, name)
                VALUES ($1, $2, $3)
                ON CONFLICT (group_id) DO UPDATE
                SET bot_id = EXCLUDED.bot_id,
                    name = EXCLUDED.name
                RETURNING *
                """,
                group_id,
                bot_id,
                name,
            )
            return Group(**dict(row))

    async def list_all(self) -> list[Group]:
        """List all registered groups."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM groups ORDER BY created_at DESC"
            )
            return [Group(**dict(row)) for row in rows]

    async def delete(self, group_id: str) -> bool:
        """Delete a group registration. Returns True if deleted."""
        pool = await get_pool()

        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM groups WHERE group_id = $1",
                group_id,
            )
            return result == "DELETE 1"


# Singleton instances
user_repo = UserRepository()
beer_repo = BeerRepository()
debt_repo = DebtRepository()
group_repo = GroupRepository()
