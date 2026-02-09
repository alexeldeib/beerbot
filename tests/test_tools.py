"""Tests for tool factory and individual tool functions."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.beerbot.tools import ToolContext, create_tools


def _make_ctx(**overrides) -> ToolContext:
    """Create a ToolContext with sensible defaults."""
    defaults = {
        "group_id": "group-1",
        "message_id": "msg-001",
        "sender_id": "user-001",
        "sender_name": "Alice",
        "sender_avatar_url": "https://example.com/alice.png",
        "mentioned_users": [],
    }
    defaults.update(overrides)
    return ToolContext(**defaults)


def _find_tool(tools: list, name: str):
    """Find a tool by function name."""
    for t in tools:
        if t.__name__ == name:
            return t
    raise KeyError(f"Tool {name!r} not found in {[t.__name__ for t in tools]}")


class TestToolFactory:
    def test_create_tools_returns_17(self):
        ctx = _make_ctx()
        tools = create_tools(ctx)
        assert len(tools) == 17

    def test_all_tools_are_async(self):
        import asyncio

        ctx = _make_ctx()
        tools = create_tools(ctx)
        for tool in tools:
            assert asyncio.iscoroutinefunction(tool), f"{tool.__name__} is not async"

    def test_all_tools_have_docstrings(self):
        ctx = _make_ctx()
        tools = create_tools(ctx)
        for tool in tools:
            assert tool.__doc__, f"{tool.__name__} missing docstring"

    def test_tool_names(self):
        ctx = _make_ctx()
        tools = create_tools(ctx)
        names = {t.__name__ for t in tools}
        expected = {
            "log_drinks",
            "remove_drinks",
            "undo_last_drink",
            "log_split_the_g",
            "remove_splits",
            "add_debt",
            "forgive_debt",
            "get_leaderboard",
            "get_today_stats",
            "get_week_stats",
            "get_recent_drinks",
            "get_user_stats",
            "get_group_stats",
            "get_debt_leaderboard",
            "get_split_g_leaderboard",
            "get_million_countdown",
            "reply",
        }
        assert names == expected


class TestLogDrinks:
    @pytest.mark.asyncio
    @patch("src.beerbot.tools.debt_repo")
    @patch("src.beerbot.tools.beer_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_logs_for_sender_by_default(self, mock_user, mock_beer, mock_debt):
        user_mock = MagicMock()
        user_mock.id = 1
        user_mock.name = "Alice"
        mock_user.get_or_create = AsyncMock(return_value=user_mock)
        mock_beer.create = AsyncMock(return_value=MagicMock(id=10))
        mock_beer.get_user_total_by_type = AsyncMock(return_value=5)
        mock_debt.reduce_debt = AsyncMock(return_value=0)

        ctx = _make_ctx()
        tools = create_tools(ctx)
        log_drinks = _find_tool(tools, "log_drinks")

        result = await log_drinks(count=1, drink_type="beer")

        assert ctx.tools_called is True
        assert result["results"][0]["status"] == "logged"
        assert result["results"][0]["user"] == "Alice"
        assert result["results"][0]["new_total"] == 5
        mock_beer.create.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.beerbot.tools.debt_repo")
    @patch("src.beerbot.tools.beer_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_clamps_count(self, mock_user, mock_beer, mock_debt):
        user_mock = MagicMock()
        user_mock.id = 1
        user_mock.name = "Alice"
        mock_user.get_or_create = AsyncMock(return_value=user_mock)
        mock_beer.create = AsyncMock(return_value=MagicMock(id=10))
        mock_beer.get_user_total_by_type = AsyncMock(return_value=25)
        mock_debt.reduce_debt = AsyncMock(return_value=0)

        ctx = _make_ctx()
        tools = create_tools(ctx)
        log_drinks = _find_tool(tools, "log_drinks")

        result = await log_drinks(count=999, drink_type="beer")
        # Should clamp to 20
        assert result["results"][0]["count"] == 20

    @pytest.mark.asyncio
    @patch("src.beerbot.tools.debt_repo")
    @patch("src.beerbot.tools.beer_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_logs_for_mentioned_users(self, mock_user, mock_beer, mock_debt):
        bob = MagicMock()
        bob.id = 2
        bob.name = "Bob"
        carol = MagicMock()
        carol.id = 3
        carol.name = "Carol"
        mock_user.get_or_create = AsyncMock(side_effect=[bob, carol])
        mock_beer.create = AsyncMock(return_value=MagicMock(id=10))
        mock_beer.get_user_total_by_type = AsyncMock(return_value=1)
        mock_debt.reduce_debt = AsyncMock(return_value=0)

        ctx = _make_ctx(mentioned_users=[("user-002", "Bob"), ("user-003", "Carol")])
        tools = create_tools(ctx)
        log_drinks = _find_tool(tools, "log_drinks")

        result = await log_drinks(
            count=2, drink_type="wine", target_user_ids=["user-002", "user-003"]
        )
        assert len(result["results"]) == 2
        assert result["results"][0]["drink_type"] == "wine"

    @pytest.mark.asyncio
    @patch("src.beerbot.tools.debt_repo")
    @patch("src.beerbot.tools.beer_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_rejects_unauthorized_user_id(self, mock_user, mock_beer, mock_debt):
        ctx = _make_ctx()
        tools = create_tools(ctx)
        log_drinks = _find_tool(tools, "log_drinks")

        with pytest.raises(ValueError, match="Cannot target user"):
            await log_drinks(count=1, drink_type="beer", target_user_ids=["hacker-id"])

    @pytest.mark.asyncio
    @patch("src.beerbot.tools.beer_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_allows_replied_to_user(self, mock_user, mock_beer):
        """Bob replies to Alice's message — tools should allow targeting Alice."""
        user_mock = MagicMock()
        user_mock.id = 42
        user_mock.name = "Alice"
        mock_user.get_by_groupme_id = AsyncMock(return_value=user_mock)
        mock_beer.remove_beers_by_type = AsyncMock(return_value=1)
        mock_beer.get_user_total_by_type = AsyncMock(return_value=4)

        ctx = _make_ctx(
            sender_id="user-bob",
            sender_name="Bob",
            replied_to_user=("user-alice", "Alice"),
        )
        tools = create_tools(ctx)
        remove = _find_tool(tools, "remove_drinks")

        # Bob can target Alice via reply context (no @mention needed)
        result = await remove(count=1, drink_type="cocktail", target_user_id="user-alice")
        assert result["user"] == "Alice"
        assert result["removed"] == 1

    @pytest.mark.asyncio
    @patch("src.beerbot.tools.debt_repo")
    @patch("src.beerbot.tools.beer_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_handles_duplicate(self, mock_user, mock_beer, mock_debt):
        mock_user.get_or_create = AsyncMock(return_value=MagicMock(id=1, name="Alice"))
        mock_beer.create = AsyncMock(return_value=None)  # duplicate

        ctx = _make_ctx()
        tools = create_tools(ctx)
        log_drinks = _find_tool(tools, "log_drinks")

        result = await log_drinks(count=1, drink_type="beer")
        assert result["results"][0]["status"] == "duplicate"

    @pytest.mark.asyncio
    @patch("src.beerbot.tools.debt_repo")
    @patch("src.beerbot.tools.beer_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_auto_reduces_debt(self, mock_user, mock_beer, mock_debt):
        user_mock = MagicMock()
        user_mock.id = 1
        user_mock.name = "Alice"
        mock_user.get_or_create = AsyncMock(return_value=user_mock)
        mock_beer.create = AsyncMock(return_value=MagicMock(id=10))
        mock_beer.get_user_total_by_type = AsyncMock(return_value=5)
        mock_debt.reduce_debt = AsyncMock(return_value=2)
        mock_debt.get_debt = AsyncMock(return_value=3)

        ctx = _make_ctx()
        tools = create_tools(ctx)
        log_drinks = _find_tool(tools, "log_drinks")

        result = await log_drinks(count=2, drink_type="beer")
        assert result["results"][0]["debt_reduced"] == 2
        assert result["results"][0]["remaining_debt"] == 3


class TestReply:
    @pytest.mark.asyncio
    async def test_sets_reply_text(self):
        ctx = _make_ctx()
        tools = create_tools(ctx)
        reply = _find_tool(tools, "reply")

        result = await reply(message="Nice one!")
        assert result == {"status": "queued"}
        assert ctx.reply_text == "Nice one!"

    @pytest.mark.asyncio
    async def test_reply_text_default_none(self):
        ctx = _make_ctx()
        assert ctx.reply_text is None


class TestGetRecentDrinks:
    @pytest.mark.asyncio
    @patch("src.beerbot.tools.beer_repo")
    async def test_returns_group_stats_default(self, mock_beer):
        stats = MagicMock()
        stats.total_beers = 25
        stats.unique_drinkers = 4
        stats.drink_type_counts = {"beer": 20, "wine": 5}
        stats.user_stats = [MagicMock(name="Alice", total_beers=10)]
        mock_beer.get_group_stats = AsyncMock(return_value=stats)

        ctx = _make_ctx()
        tools = create_tools(ctx)
        recent = _find_tool(tools, "get_recent_drinks")

        result = await recent()
        assert result["total"] == 25
        assert result["until"] == "now"
        assert ctx.tools_called is True

    @pytest.mark.asyncio
    @patch("src.beerbot.tools.beer_repo")
    async def test_with_since_and_until(self, mock_beer):
        stats = MagicMock()
        stats.total_beers = 18
        stats.unique_drinkers = 3
        stats.drink_type_counts = {"beer": 18}
        stats.user_stats = []
        mock_beer.get_group_stats = AsyncMock(return_value=stats)

        ctx = _make_ctx()
        tools = create_tools(ctx)
        recent = _find_tool(tools, "get_recent_drinks")

        result = await recent(since="2026-02-07T00:00:00", until="2026-02-08T00:00:00")
        assert result["total"] == 18
        # Verify both timestamps were passed to repo
        call_kwargs = mock_beer.get_group_stats.call_args
        assert call_kwargs.kwargs["since"] is not None
        assert call_kwargs.kwargs["until"] is not None

    @pytest.mark.asyncio
    @patch("src.beerbot.tools.beer_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_returns_user_specific_stats(self, mock_user, mock_beer):
        user_mock = MagicMock()
        user_mock.id = 1
        user_mock.name = "Alice"
        mock_user.get_or_create = AsyncMock(return_value=user_mock)

        user_stat = MagicMock()
        user_stat.name = "Alice"
        user_stat.total_beers = 7
        stats = MagicMock()
        stats.user_stats = [user_stat]
        mock_beer.get_group_stats = AsyncMock(return_value=stats)

        ctx = _make_ctx()
        tools = create_tools(ctx)
        recent = _find_tool(tools, "get_recent_drinks")

        result = await recent(target_user_id="user-001")
        assert result["user"] == "Alice"
        assert result["total"] == 7

    @pytest.mark.asyncio
    @patch("src.beerbot.tools.beer_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_user_not_in_period_returns_zero(self, mock_user, mock_beer):
        user_mock = MagicMock()
        user_mock.id = 1
        user_mock.name = "Alice"
        mock_user.get_or_create = AsyncMock(return_value=user_mock)

        stats = MagicMock()
        stats.user_stats = []
        mock_beer.get_group_stats = AsyncMock(return_value=stats)

        ctx = _make_ctx()
        tools = create_tools(ctx)
        recent = _find_tool(tools, "get_recent_drinks")

        result = await recent(since="2026-02-07T00:00:00", target_user_id="user-001")
        assert result["total"] == 0


class TestRemoveDrinks:
    @pytest.mark.asyncio
    @patch("src.beerbot.tools.beer_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_removes_from_sender(self, mock_user, mock_beer):
        mock_user.get_by_groupme_id = AsyncMock(return_value=MagicMock(id=1, name="Alice"))
        mock_beer.remove_beers_by_type = AsyncMock(return_value=3)
        mock_beer.get_user_total_by_type = AsyncMock(return_value=7)

        ctx = _make_ctx()
        tools = create_tools(ctx)
        remove = _find_tool(tools, "remove_drinks")

        result = await remove(count=3, drink_type="cocktail")
        assert result["removed"] == 3
        assert result["new_total"] == 7


class TestAddDebt:
    @pytest.mark.asyncio
    @patch("src.beerbot.tools.debt_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_only_allows_mentioned_users(self, mock_user, mock_debt):
        ctx = _make_ctx(mentioned_users=[("user-002", "Bob")])
        tools = create_tools(ctx)
        add = _find_tool(tools, "add_debt")

        # Trying to add debt to sender (not mentioned) should fail
        result = await add(target_user_id="user-001", amount=5)
        assert "error" in result

    @pytest.mark.asyncio
    @patch("src.beerbot.tools.debt_repo")
    @patch("src.beerbot.tools.user_repo")
    async def test_adds_debt_to_mentioned(self, mock_user, mock_debt):
        mock_user.get_or_create = AsyncMock(return_value=MagicMock(id=2, name="Bob"))
        mock_debt.add_debt = AsyncMock(return_value=5)

        ctx = _make_ctx(mentioned_users=[("user-002", "Bob")])
        tools = create_tools(ctx)
        add = _find_tool(tools, "add_debt")

        result = await add(target_user_id="user-002", amount=5)
        assert result["total_debt"] == 5


class TestGetLeaderboard:
    @pytest.mark.asyncio
    @patch("src.beerbot.tools.beer_repo")
    async def test_returns_entries(self, mock_beer):
        mock_beer.get_leaderboard_with_breakdown = AsyncMock(
            return_value=[
                ("Alice", 100, {"beer": 80, "wine": 20, "cocktail": 0, "claw": 0}),
                ("Bob", 75, {"beer": 75, "wine": 0, "cocktail": 0, "claw": 0}),
            ]
        )

        ctx = _make_ctx()
        tools = create_tools(ctx)
        lb = _find_tool(tools, "get_leaderboard")

        result = await lb()
        assert len(result["entries"]) == 2
        assert result["entries"][0]["rank"] == 1
        assert result["entries"][0]["name"] == "Alice"

    @pytest.mark.asyncio
    @patch("src.beerbot.tools.beer_repo")
    async def test_clamps_limit(self, mock_beer):
        mock_beer.get_leaderboard_with_breakdown = AsyncMock(return_value=[])

        ctx = _make_ctx()
        tools = create_tools(ctx)
        lb = _find_tool(tools, "get_leaderboard")

        await lb(limit=999)
        # Should have clamped to 50
        mock_beer.get_leaderboard_with_breakdown.assert_called_once_with("group-1", None, 50)


class TestGetMillionCountdown:
    @pytest.mark.asyncio
    @patch("src.beerbot.tools.beer_repo")
    async def test_returns_projections(self, mock_beer):
        mock_beer.get_group_total_by_type = AsyncMock(return_value=5000)
        mock_beer.get_rate_stats = AsyncMock(
            side_effect=[
                (5000, 100, 7.0),  # 7-day stats
                (5000, 400, 28.0),  # 30-day stats
            ]
        )

        ctx = _make_ctx()
        tools = create_tools(ctx)
        countdown = _find_tool(tools, "get_million_countdown")

        result = await countdown()
        assert result["total"] == 5000
        assert result["remaining"] == 995000
        assert "pace_7d" in result
        assert "pace_30d" in result
        assert "target_date" in result
