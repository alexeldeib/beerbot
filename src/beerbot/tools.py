"""Tool factory for Gemini function calling.

Each tool is an async closure that captures a ToolContext, binding group_id,
message_id, and sender info so the AI never sees security-sensitive identifiers.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .models import DrinkType
from .repositories import beer_repo, debt_repo, user_repo

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")


@dataclass
class ToolContext:
    """Context injected into tool closures via the factory."""

    group_id: str
    message_id: str
    sender_id: str
    sender_name: str
    sender_avatar_url: str | None
    mentioned_users: list[tuple[str, str]]  # (user_id, name)
    replied_to_user: tuple[str, str] | None = None  # (user_id, name) from reply context
    tools_called: bool = field(default=False, init=False)


def _validate_user_id(ctx: ToolContext, user_id: str | None) -> str:
    """Validate and resolve a user_id, defaulting to sender."""
    if user_id is None:
        return ctx.sender_id
    allowed = {ctx.sender_id} | {uid for uid, _ in ctx.mentioned_users}
    if ctx.replied_to_user:
        allowed.add(ctx.replied_to_user[0])
    if user_id not in allowed:
        raise ValueError(f"Cannot target user {user_id}: not sender or mentioned")
    return user_id


def _resolve_name(ctx: ToolContext, user_id: str) -> str:
    """Get display name for a user_id from context."""
    if user_id == ctx.sender_id:
        return ctx.sender_name
    for uid, name in ctx.mentioned_users:
        if uid == user_id:
            return name
    if ctx.replied_to_user and ctx.replied_to_user[0] == user_id:
        return ctx.replied_to_user[1]
    return f"User {user_id[-4:]}"


def _resolve_avatar(ctx: ToolContext, user_id: str) -> str | None:
    """Get avatar URL for a user_id from context."""
    if user_id == ctx.sender_id:
        return ctx.sender_avatar_url
    return None


async def _get_or_create_user(ctx: ToolContext, user_id: str):
    """Get or create a user from context info."""
    return await user_repo.get_or_create(
        groupme_user_id=user_id,
        name=_resolve_name(ctx, user_id),
        avatar_url=_resolve_avatar(ctx, user_id),
    )


async def _get_next_ahead(
    user_name: str, group_id: str, drink_type: DrinkType | None = None
) -> dict | None:
    """Find the person one rank above user_name on the leaderboard.

    Returns {"name": ..., "total": ...} or None if user is #1 or not found.
    """
    entries = await beer_repo.get_leaderboard_with_breakdown(group_id, drink_type, 50)
    for i, (name, total, _breakdown) in enumerate(entries):
        if name == user_name:
            if i > 0:
                ahead_name, ahead_total, _ = entries[i - 1]
                return {"name": ahead_name, "total": ahead_total}
            return None  # already #1
    return None  # user not found on leaderboard


def create_tools(ctx: ToolContext) -> list[Callable]:
    """Create tool closures bound to the given request context.

    Returns a list of async callables suitable for Gemini AFC.
    """

    # === WRITE TOOLS ===

    async def log_drinks(
        count: int, drink_type: str, target_user_ids: list[str] | None = None
    ) -> dict:
        """Log drinks for one or more users. Use when someone is drinking now.

        Args:
            count: Number of drinks to log, between 1 and 20.
            drink_type: Type of drink: beer, wine, cocktail, or claw.
            target_user_ids: User IDs to log for. Defaults to the message sender.
                When specified, logs ONLY for those users (not the sender unless included).
        """
        ctx.tools_called = True
        count = max(1, min(20, count))
        dt = DrinkType.from_string(drink_type)

        # Determine who to log for
        if target_user_ids:
            user_ids = [_validate_user_id(ctx, uid) for uid in target_user_ids]
        else:
            user_ids = [ctx.sender_id]

        # Deduplicate
        seen: set[str] = set()
        unique_ids: list[str] = []
        for uid in user_ids:
            if uid not in seen:
                seen.add(uid)
                unique_ids.append(uid)

        results = []
        for uid in unique_ids:
            user = await _get_or_create_user(ctx, uid)
            beer = await beer_repo.create(
                user_id=user.id,
                group_id=ctx.group_id,
                quantity=count,
                message_id=ctx.message_id,
                split_the_g=0,
                drink_type=dt,
            )
            if beer is None:
                results.append({"user": user.name, "status": "duplicate"})
                continue

            total = await beer_repo.get_user_total_by_type(user.id, ctx.group_id, dt)
            overall_total = await beer_repo.get_user_total(user.id, ctx.group_id)
            next_ahead_by_type = await _get_next_ahead(user.name, ctx.group_id, dt)
            next_ahead_overall = await _get_next_ahead(user.name, ctx.group_id, None)

            # Auto-reduce debt
            debt_reduced = await debt_repo.reduce_debt(user.id, ctx.group_id, count)
            remaining_debt = 0
            if debt_reduced > 0:
                remaining_debt = await debt_repo.get_debt(user.id, ctx.group_id)

            results.append(
                {
                    "user": user.name,
                    "status": "logged",
                    "count": count,
                    "drink_type": dt.value,
                    "new_total": total,
                    "overall_total": overall_total,
                    "next_ahead_by_type": next_ahead_by_type,
                    "next_ahead_overall": next_ahead_overall,
                    "debt_reduced": debt_reduced,
                    "remaining_debt": remaining_debt,
                }
            )

        return {"results": results}

    async def remove_drinks(count: int, drink_type: str, target_user_id: str | None = None) -> dict:
        """Remove drinks from a user's count.

        Args:
            count: Number of drinks to remove, between 1 and 50.
            drink_type: Type of drink: beer, wine, cocktail, or claw.
            target_user_id: User to remove from. Defaults to the message sender.
        """
        ctx.tools_called = True
        count = max(1, min(50, count))
        dt = DrinkType.from_string(drink_type)
        uid = _validate_user_id(ctx, target_user_id)

        user = await user_repo.get_by_groupme_id(uid)
        if not user:
            return {"error": "User not found", "removed": 0}

        removed = await beer_repo.remove_beers_by_type(user.id, ctx.group_id, count, dt)
        new_total = await beer_repo.get_user_total_by_type(user.id, ctx.group_id, dt)

        return {
            "user": user.name,
            "removed": removed,
            "drink_type": dt.value,
            "new_total": new_total,
        }

    async def undo_last_drink(target_user_id: str | None = None) -> dict:
        """Undo the most recent drink entry for a user.

        Args:
            target_user_id: User to undo for. Defaults to the message sender.
        """
        ctx.tools_called = True
        uid = _validate_user_id(ctx, target_user_id)

        user = await user_repo.get_by_groupme_id(uid)
        if not user:
            user = await _get_or_create_user(ctx, uid)

        removed_qty = await beer_repo.delete_last_beer(user.id, ctx.group_id)
        total = await beer_repo.get_user_total(user.id, ctx.group_id)

        return {
            "user": user.name,
            "removed": removed_qty,
            "new_total": total,
        }

    async def log_split_the_g(target_user_id: str | None = None) -> dict:
        """Log a Split the G achievement (Guinness poured to the G level). Also logs 1 beer.

        Args:
            target_user_id: User who split the G. Defaults to the message sender.
        """
        ctx.tools_called = True
        uid = _validate_user_id(ctx, target_user_id)
        user = await _get_or_create_user(ctx, uid)

        beer = await beer_repo.create(
            user_id=user.id,
            group_id=ctx.group_id,
            quantity=1,
            message_id=ctx.message_id,
            split_the_g=1,
            drink_type=DrinkType.BEER,
        )
        if beer is None:
            return {"user": user.name, "status": "duplicate"}

        beer_total = await beer_repo.get_user_total(user.id, ctx.group_id)
        split_total = await beer_repo.get_user_split_g_total(user.id, ctx.group_id)

        return {
            "user": user.name,
            "status": "logged",
            "beer_total": beer_total,
            "split_total": split_total,
        }

    async def remove_splits(count: int, target_user_id: str | None = None) -> dict:
        """Remove split-the-G counts from a user.

        Args:
            count: Number of splits to remove, between 1 and 20.
            target_user_id: User to remove from. Defaults to the message sender.
        """
        ctx.tools_called = True
        count = max(1, min(20, count))
        uid = _validate_user_id(ctx, target_user_id)

        user = await user_repo.get_by_groupme_id(uid)
        if not user:
            return {"error": "User not found", "removed": 0}

        removed = await beer_repo.remove_splits(user.id, ctx.group_id, count)
        total = await beer_repo.get_user_split_g_total(user.id, ctx.group_id)

        return {"user": user.name, "removed": removed, "new_total": total}

    async def add_debt(target_user_id: str, amount: int) -> dict:
        """Add beer debt to a mentioned user (they owe the group).

        Args:
            target_user_id: The mentioned user who owes beers. Must be a mentioned user.
            amount: Number of beers owed, between 1 and 100.
        """
        ctx.tools_called = True
        amount = max(1, min(100, amount))

        # Debt can only be added to mentioned users, not self
        mentioned_ids = {uid for uid, _ in ctx.mentioned_users}
        if target_user_id not in mentioned_ids:
            return {"error": "Can only add debt to a mentioned user"}

        uid = _validate_user_id(ctx, target_user_id)
        user = await _get_or_create_user(ctx, uid)
        new_total = await debt_repo.add_debt(user.id, ctx.group_id, amount)

        return {"user": user.name, "amount_added": amount, "total_debt": new_total}

    async def forgive_debt(target_user_id: str | None = None, amount: int = 1) -> dict:
        """Forgive (reduce) a user's beer debt without them drinking.

        Args:
            target_user_id: User to forgive. Defaults to the message sender.
            amount: Number of beers to forgive, between 1 and 100.
        """
        ctx.tools_called = True
        amount = max(1, min(100, amount))
        uid = _validate_user_id(ctx, target_user_id)

        user = await user_repo.get_by_groupme_id(uid)
        if not user:
            return {"error": "User not found"}

        current = await debt_repo.get_debt(user.id, ctx.group_id)
        if current <= 0:
            return {"user": user.name, "error": "No debt to forgive"}

        reduced = await debt_repo.reduce_debt(user.id, ctx.group_id, amount)
        remaining = await debt_repo.get_debt(user.id, ctx.group_id)

        return {
            "user": user.name,
            "forgiven": reduced,
            "remaining_debt": remaining,
        }

    # === READ TOOLS ===

    async def get_leaderboard(drink_type: str | None = None, limit: int = 10) -> dict:
        """Get the drink leaderboard showing top drinkers.

        Args:
            drink_type: Filter by type (beer, wine, cocktail, claw), or null for all.
            limit: Number of users to show, between 1 and 50. Default 10.
        """
        ctx.tools_called = True
        limit = max(1, min(50, limit))
        dt = DrinkType.from_string(drink_type) if drink_type else None

        entries = await beer_repo.get_leaderboard_with_breakdown(ctx.group_id, dt, limit)
        return {
            "filter": dt.value if dt else "all",
            "entries": [
                {"rank": i + 1, "name": name, "total": total, "breakdown": breakdown}
                for i, (name, total, breakdown) in enumerate(entries)
            ],
        }

    async def get_today_stats(drink_type: str | None = None) -> dict:
        """Get drink stats for today (Eastern time).

        Args:
            drink_type: Filter by type (beer, wine, cocktail, claw), or null for all.
        """
        ctx.tools_called = True
        dt = DrinkType.from_string(drink_type) if drink_type else None
        now = datetime.now(EASTERN)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        stats = await beer_repo.get_group_stats(ctx.group_id, since=start, drink_type=dt)

        return {
            "period": "today",
            "filter": dt.value if dt else "all",
            "total": stats.total_beers,
            "drinkers": stats.unique_drinkers,
            "type_breakdown": stats.drink_type_counts,
            "top_users": [{"name": u.name, "count": u.total_beers} for u in stats.user_stats[:10]],
        }

    async def get_week_stats(drink_type: str | None = None) -> dict:
        """Get drink stats for this week (Monday-Sunday, Eastern time).

        Args:
            drink_type: Filter by type (beer, wine, cocktail, claw), or null for all.
        """
        ctx.tools_called = True
        dt = DrinkType.from_string(drink_type) if drink_type else None
        now = datetime.now(EASTERN)
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        stats = await beer_repo.get_group_stats(ctx.group_id, since=start, drink_type=dt)

        return {
            "period": "this_week",
            "filter": dt.value if dt else "all",
            "total": stats.total_beers,
            "drinkers": stats.unique_drinkers,
            "type_breakdown": stats.drink_type_counts,
            "top_users": [{"name": u.name, "count": u.total_beers} for u in stats.user_stats[:10]],
        }

    async def get_user_stats(target_user_id: str | None = None) -> dict:
        """Get detailed stats for a specific user.

        Args:
            target_user_id: User to get stats for. Defaults to the message sender.
        """
        ctx.tools_called = True
        uid = _validate_user_id(ctx, target_user_id)
        user = await _get_or_create_user(ctx, uid)

        stats = await beer_repo.get_user_stats_in_group(user.id, ctx.group_id)
        by_type = await beer_repo.get_user_stats_by_type(user.id, ctx.group_id)

        now = datetime.now(EASTERN)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_week = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        today_stats = await beer_repo.get_group_stats(ctx.group_id, since=start_of_day)
        week_stats = await beer_repo.get_group_stats(ctx.group_id, since=start_of_week)

        today_count = next(
            (u.total_beers for u in today_stats.user_stats if u.name == user.name), 0
        )
        week_count = next((u.total_beers for u in week_stats.user_stats if u.name == user.name), 0)

        breakdown = {dt.value: count for dt, count in by_type.items()}

        return {
            "user": user.name,
            "total": stats.total_beers if stats else 0,
            "today": today_count,
            "this_week": week_count,
            "breakdown": breakdown,
            "last_drink_at": stats.last_beer_at.isoformat()
            if stats and stats.last_beer_at
            else None,
        }

    async def get_group_stats() -> dict:
        """Get all-time stats for the entire group."""
        ctx.tools_called = True
        stats = await beer_repo.get_group_stats(ctx.group_id)

        return {
            "total": stats.total_beers,
            "drinkers": stats.unique_drinkers,
            "type_breakdown": stats.drink_type_counts,
            "top_users": [{"name": u.name, "count": u.total_beers} for u in stats.user_stats[:10]],
        }

    async def get_debt_leaderboard() -> dict:
        """Get the debt leaderboard showing who owes the most beers."""
        ctx.tools_called = True
        entries = await debt_repo.get_debt_leaderboard(ctx.group_id)

        return {
            "entries": [{"name": e.name, "amount": e.amount} for e in entries],
        }

    async def get_split_g_leaderboard() -> dict:
        """Get the Split the G leaderboard showing who has the most Guinness splits."""
        ctx.tools_called = True
        stats = await beer_repo.get_split_g_stats(ctx.group_id)

        return {
            "total_splits": stats.total_splits,
            "entries": [{"name": u.name, "splits": u.split_the_g_count} for u in stats.user_stats],
        }

    async def get_million_countdown(drink_type: str | None = None) -> dict:
        """Get the countdown to 1 million drinks with pace projections.

        Args:
            drink_type: Filter by type (beer, wine, cocktail, claw), or null for all.
        """
        ctx.tools_called = True
        dt = DrinkType.from_string(drink_type) if drink_type else None
        goal = 1_000_000

        total = await beer_repo.get_group_total_by_type(ctx.group_id, dt)
        _, beers_7d, days_7d = await beer_repo.get_rate_stats(ctx.group_id, days=7, drink_type=dt)
        _, beers_30d, days_30d = await beer_repo.get_rate_stats(
            ctx.group_id, days=30, drink_type=dt
        )

        remaining = goal - total
        result: dict = {
            "filter": dt.value if dt else "all",
            "total": total,
            "goal": goal,
            "remaining": max(0, remaining),
        }

        if days_7d > 0 and beers_7d > 0:
            rate_7d = beers_7d / days_7d
            result["pace_7d"] = round(rate_7d, 1)
            result["days_at_7d_pace"] = round(remaining / rate_7d)

        if days_30d > 0 and beers_30d > 0:
            rate_30d = beers_30d / days_30d
            result["pace_30d"] = round(rate_30d, 1)
            result["days_at_30d_pace"] = round(remaining / rate_30d)
            target_date = datetime.now(EASTERN) + timedelta(days=remaining / rate_30d)
            result["target_date"] = target_date.strftime("%B %d, %Y")

        return result

    return [
        # Write tools
        log_drinks,
        remove_drinks,
        undo_last_drink,
        log_split_the_g,
        remove_splits,
        add_debt,
        forgive_debt,
        # Read tools
        get_leaderboard,
        get_today_stats,
        get_week_stats,
        get_user_stats,
        get_group_stats,
        get_debt_leaderboard,
        get_split_g_leaderboard,
        get_million_countdown,
    ]
