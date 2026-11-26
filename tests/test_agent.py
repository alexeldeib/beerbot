"""Tests for BeerAgent."""

from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import httpx
import pytest
from google.genai import types
from src.beerbot import agent as agent_module
from src.beerbot.agent import BeerAgent, TokenBucket, extract_mentioned_users, fetch_media
from src.beerbot.model_runtime import Media, ModelProtocolError
from src.beerbot.models import GroupMeAttachment, GroupMeMessage


def empty_response(text=""):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=text)]),
                finish_reason="STOP",
            )
        ]
    )


def _make_message(**overrides) -> GroupMeMessage:
    defaults = {
        "attachments": [],
        "avatar_url": "https://example.com/avatar.png",
        "created_at": 1703700000,
        "group_id": "group-1",
        "id": "msg-001",
        "name": "Alice",
        "sender_id": "user-001",
        "sender_type": "user",
        "text": "+1 beer",
        "user_id": "user-001",
    }
    defaults.update(overrides)
    return GroupMeMessage(**defaults)


class TestTokenBucket:
    def test_initial_capacity(self):
        bucket = TokenBucket()
        assert bucket.consume() is True

    def test_drains_capacity(self):
        bucket = TokenBucket(capacity=2, tokens=2.0)
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is False

    def test_peek_does_not_consume(self):
        bucket = TokenBucket(capacity=1, tokens=1.0)
        assert bucket.peek() is True
        assert bucket.peek() is True  # Still available
        assert bucket.consume() is True
        assert bucket.peek() is False


class TestExtractMentionedUsers:
    def test_no_text(self):
        assert extract_mentioned_users(None, []) == []

    def test_no_mentions(self):
        attachments = [GroupMeAttachment(type="image", url="https://example.com")]
        assert extract_mentioned_users("+1 beer", attachments) == []

    def test_extract_name_from_loci(self):
        text = "+1 beer @Bob Smith"
        # @Bob Smith starts at index 8, length 10
        attachments = [
            GroupMeAttachment(
                type="mentions",
                user_ids=["user-002"],
                loci=[[8, 10]],
            ),
        ]
        result = extract_mentioned_users(text, attachments)
        assert len(result) == 1
        assert result[0] == ("user-002", "Bob Smith")

    def test_deduplicates(self):
        text = "+1 beer @Alice @Alice"
        attachments = [
            GroupMeAttachment(
                type="mentions",
                user_ids=["user-001", "user-001"],
                loci=[[9, 6], [16, 6]],
            ),
        ]
        result = extract_mentioned_users(text, attachments)
        assert len(result) == 1

    def test_fallback_name(self):
        text = "+1 beer @someone"
        attachments = [
            GroupMeAttachment(type="mentions", user_ids=["12345678"]),
        ]
        result = extract_mentioned_users(text, attachments)
        assert result[0] == ("12345678", "User 5678")


@pytest.fixture
def model(monkeypatch):
    """Beerius on a fake Google client, configured through the real settings. Returns the
    client; its generate_content answers with an empty turn unless a test says otherwise."""
    monkeypatch.setattr(agent_module.settings, "llm_provider", "google")
    monkeypatch.setattr(agent_module.settings, "llm_api_key", None)
    monkeypatch.setattr(agent_module.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(agent_module.settings, "enable_image_analysis", False)
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=empty_response())
    monkeypatch.setattr(agent_module.genai, "Client", lambda api_key: client)
    return client


class TestBeerAgentInit:
    def test_no_api_key(self, monkeypatch):
        monkeypatch.setattr(agent_module.settings, "llm_api_key", None)
        monkeypatch.setattr(agent_module.settings, "gemini_api_key", None)
        assert BeerAgent().client is None

    def test_with_api_key(self, model):
        assert BeerAgent().client is model


class TestBeerAgentProcessMessage:
    async def test_raises_without_client_so_the_message_retries(self, monkeypatch):
        monkeypatch.setattr(agent_module.settings, "llm_api_key", None)
        monkeypatch.setattr(agent_module.settings, "gemini_api_key", None)
        with pytest.raises(RuntimeError, match="Model client unavailable"):
            await BeerAgent().process_message(_make_message(), [])

    async def test_returns_reply_via_tool(self, model, monkeypatch):
        # Simulate the reply tool setting ctx.reply_text during the tool loop
        def setup_tools(ctx):
            ctx.reply_text = "+1🍺 for Alice. 5🍺 total."
            ctx.tools_called = True
            return []

        monkeypatch.setattr(agent_module, "create_tools", setup_tools)
        result = await BeerAgent().process_message(_make_message(), [])
        assert result == "+1🍺 for Alice. 5🍺 total."

    async def test_suppresses_personality_when_rate_limited(self, model, monkeypatch):
        # Model calls reply but no data tools
        def setup_tools(ctx):
            ctx.reply_text = "Nice weather we're having!"
            return []

        monkeypatch.setattr(agent_module, "create_tools", setup_tools)
        agent = BeerAgent()
        agent._get_bucket("group-1").tokens = 0.0  # Drain the rate limiter
        # Suppressed: no tools were called and the bucket is empty
        assert await agent.process_message(_make_message(), []) is None

    async def test_model_errors_raise_so_the_message_retries(self, model):
        model.aio.models.generate_content = AsyncMock(side_effect=Exception("API error"))
        with pytest.raises(Exception, match="API error"):
            await BeerAgent().process_message(_make_message(), [])


def _bot_post(**overrides) -> GroupMeMessage:
    return _make_message(**{"sender_type": "bot", "name": "beerius", "user_id": "bot"} | overrides)


class TestReplyContext:
    def test_reply_context_names_the_sender_and_their_id(self):
        alice = _make_message(id="msg-050", text="+1 cocktail", user_id="user-042")
        msg = _make_message(text="that was a wine not a cocktail")
        context = BeerAgent()._build_context_lines(msg, [alice], alice)
        assert "Replying to [Alice] (id: user-042): +1 cocktail" in context

    def test_reply_context_bot_message_has_no_target_id(self):
        post = _bot_post(id="msg-060", text="Logged 1 beer!")
        context = BeerAgent()._build_context_lines(_make_message(text="undo that"), [], post)
        assert "Replying to [beerius]: Logged 1 beer!" in context

    def test_reply_context_names_who_beerius_answered(self):
        jerry = _make_message(id="msg-040", text="+1 cocktail", name="Jerry", user_id="jerry")
        post = _bot_post(id="msg-041", text="+1🍸 for Jerry")
        context = BeerAgent()._build_context_lines(_make_message(), [], post, jerry)
        assert "Replying to [beerius]'s answer to [Jerry] (id: jerry): +1🍸 for Jerry" in context

    def test_reply_context_names_media_without_text(self):
        photo = _make_message(text=None, attachments=[GroupMeAttachment(type="image")])
        video = _make_message(text=None, attachments=[GroupMeAttachment(type="video")])
        agent = BeerAgent()
        assert "(id: user-001): (image)" in agent._build_context_lines(_make_message(), [], photo)
        assert "(id: user-001): (video)" in agent._build_context_lines(_make_message(), [], video)

    def test_reply_context_no_reply_attachment(self):
        context = BeerAgent()._build_context_lines(_make_message(text="hi"), [], None)
        assert "Replying to" not in context

    def test_recent_messages_show_the_history_with_text(self):
        history = [
            _make_message(id="1", text="+1 beer", name="Bob"),
            _make_message(id="2", text=None, attachments=[GroupMeAttachment(type="image")]),
            _bot_post(id="3", text="+1🍺 for Bob"),
        ]
        context = BeerAgent()._build_context_lines(_make_message(text="nice"), history, None)
        assert context.endswith("Recent messages:\n  [Bob]: +1 beer\n  [beerius]: +1🍺 for Bob")


class TestRepliedToUser:
    """Who a reply targets: the replied-to sender, or whoever Beerius's post answered; never a
    bot. The inbox lookup itself is tested against Postgres in test_durable_delivery."""

    async def replied_to(self, monkeypatch, replied, answered=None):
        monkeypatch.setattr(agent_module.settings, "gemini_api_key", "test-key")
        monkeypatch.setattr(agent_module.settings, "llm_api_key", None)
        monkeypatch.setattr(
            agent_module, "reply_target", AsyncMock(return_value=(replied, answered))
        )
        captured = {}

        def spy(ctx):
            captured["ctx"] = ctx
            return []

        agent = BeerAgent()
        agent.client = MagicMock()
        agent.client.aio.models.generate_content = AsyncMock(return_value=empty_response())
        monkeypatch.setattr(agent_module, "create_tools", spy)
        msg = _make_message(id="msg-099", text="that was a wine", name="Bob", user_id="user-bob")
        await agent.process_message(msg, [])
        return captured["ctx"].replied_to_user

    async def test_reply_to_a_member_targets_them(self, monkeypatch):
        alice = _make_message(id="msg-050", text="+1 cocktail", user_id="user-042")
        assert await self.replied_to(monkeypatch, alice) == ("user-042", "Alice")

    async def test_reply_to_a_photo_targets_its_sender(self, monkeypatch):
        photo = _make_message(id="msg-070", text=None, name="Bryan", user_id="user-bryan")
        assert await self.replied_to(monkeypatch, photo) == ("user-bryan", "Bryan")

    async def test_reply_to_beerius_targets_who_it_answered(self, monkeypatch):
        jerry = _make_message(id="msg-040", text="+1 cocktail", name="Jerry", user_id="jerry")
        post = _bot_post(id="msg-041", text="+1🍸 for Jerry")
        assert await self.replied_to(monkeypatch, post, jerry) == ("jerry", "Jerry")

    async def test_reply_to_a_bot_or_an_unknown_message_targets_nobody(self, monkeypatch):
        post = _bot_post(id="msg-060", text="+1🍺 for Bob")
        assert await self.replied_to(monkeypatch, post) is None
        assert await self.replied_to(monkeypatch, None) is None


def media_server(monkeypatch, handler):
    """Serve attachment fetches from `handler` instead of the network."""
    real = httpx.AsyncClient
    monkeypatch.setattr(
        agent_module.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


async def chunks(count: int, size: int = 4, sent: list | None = None):
    for _ in range(count):
        if sent is not None:
            sent.append(size)
        yield b"x" * size


class TestFetchMedia:
    async def test_fetches_with_its_content_type(self, monkeypatch):
        media_server(
            monkeypatch,
            lambda r: httpx.Response(200, content=b"img", headers={"content-type": "image/png"}),
        )
        assert await fetch_media("https://i.test/a", "image/jpeg") == Media(b"img", "image/png")

    async def test_declared_size_over_the_cap_is_never_read(self, monkeypatch):
        sent = []
        monkeypatch.setattr(agent_module, "MAX_MEDIA_BYTES", 10)
        media_server(
            monkeypatch,
            lambda r: httpx.Response(
                200, headers={"content-length": "11"}, content=chunks(3, sent=sent)
            ),
        )
        assert await fetch_media("https://v.test/huge.mp4", "video/mp4") is None
        assert sent == []

    async def test_streamed_size_over_the_cap_stops_reading(self, monkeypatch):
        sent = []
        monkeypatch.setattr(agent_module, "MAX_MEDIA_BYTES", 10)
        media_server(monkeypatch, lambda r: httpx.Response(200, content=chunks(100, sent=sent)))
        assert await fetch_media("https://v.test/huge.mp4", "video/mp4") is None
        assert sum(sent) <= 12  # Stopped at the cap instead of buffering 400 bytes.

    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_transient_failures_raise_so_the_message_retries(self, monkeypatch, status):
        media_server(monkeypatch, lambda r: httpx.Response(status))
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_media("https://i.test/a", "image/jpeg")

    async def test_timeouts_raise(self, monkeypatch):
        def slow(request):
            raise httpx.ReadTimeout("slow", request=request)

        media_server(monkeypatch, slow)
        with pytest.raises(httpx.TimeoutException):
            await fetch_media("https://i.test/a", "image/jpeg")

    async def test_permanent_failures_tell_the_model(self, monkeypatch):
        monkeypatch.setattr(agent_module.settings, "gemini_api_key", "test-key")
        monkeypatch.setattr(agent_module.settings, "llm_api_key", None)
        media_server(monkeypatch, lambda r: httpx.Response(404))
        attachments = [
            GroupMeAttachment(type="image", url="https://i.test/gone"),
            GroupMeAttachment(type="video", url="https://v.test/gone"),
        ]
        contents = await BeerAgent()._build_contents(_make_message(attachments=attachments))
        assert contents == [
            "+1 beer",
            "[photo could not be loaded]",
            "[video could not be loaded]",
        ]


class TestGenerateWeeklyRecap:
    @patch("src.beerbot.agent.beer_repo")
    async def test_enriched_recap_includes_fun_facts(self, mock_beer_repo, model):
        model.aio.models.generate_content = AsyncMock(
            return_value=empty_response("Weekly recap text!")
        )

        # Mock basic stats
        week_stats = MagicMock()
        week_stats.total_beers = 42
        week_stats.unique_drinkers = 5
        week_stats.drink_type_counts = {"beer": 30, "wine": 12}
        week_stats.user_stats = [
            SimpleNamespace(name=person, total_beers=count)
            for person, count in [
                ("Weekly winner", 15),
                ("Second", 10),
                ("Third", 7),
                ("Fourth", 5),
                ("Fifth", 3),
                ("Outside top five", 2),
            ]
        ]

        mock_beer_repo.get_group_stats = AsyncMock(return_value=week_stats)
        mock_beer_repo.get_leaderboard_with_breakdown = AsyncMock(
            return_value=[("Lifetime champion", 9000, {"beer": 9000})]
        )
        mock_beer_repo.get_split_g_stats = AsyncMock(return_value=MagicMock(total_splits=0))

        # Mock fun stats
        mock_beer_repo.get_period_total = AsyncMock(return_value=35)
        mock_beer_repo.get_biggest_day = AsyncMock(return_value=("Saturday", 18))
        mock_beer_repo.get_type_champions = AsyncMock(
            return_value=[("beer", "Bryan", 15), ("wine", "Celena", 8)]
        )
        mock_beer_repo.get_milestones = AsyncMock(return_value=[("Bryan", 200)])

        result = await BeerAgent().generate_weekly_recap("group-1")

        assert result == "Weekly recap text!"

        # Verify the prompt sent to Gemini includes fun facts
        call_args = model.aio.models.generate_content.call_args
        prompt = call_args.kwargs["contents"][0].parts[0].text
        assert "FUN FACTS" in prompt
        assert "Saturday" in prompt
        assert "Bryan" in prompt
        assert "Milestones" in prompt
        assert "35 last week" in prompt
        assert "1. Weekly winner: 15 drinks" in prompt
        assert "5. Fifth: 3 drinks" in prompt
        assert "Outside top five" not in prompt
        assert "Lifetime champion" not in prompt
        assert "ALL-TIME OVERALL RANKINGS" not in prompt
        mock_beer_repo.get_leaderboard_with_breakdown.assert_not_awaited()

    @patch("src.beerbot.agent.beer_repo")
    async def test_recap_no_fun_facts_when_empty(self, mock_beer_repo, model):
        model.aio.models.generate_content = AsyncMock(return_value=empty_response("Quiet week."))

        week_stats = MagicMock()
        week_stats.total_beers = 3
        week_stats.unique_drinkers = 1
        week_stats.drink_type_counts = {"beer": 3}
        week_stats.user_stats = []

        mock_beer_repo.get_group_stats = AsyncMock(return_value=week_stats)
        mock_beer_repo.get_leaderboard_with_breakdown = AsyncMock(return_value=[])
        mock_beer_repo.get_split_g_stats = AsyncMock(return_value=MagicMock(total_splits=0))
        mock_beer_repo.get_period_total = AsyncMock(return_value=0)
        mock_beer_repo.get_biggest_day = AsyncMock(return_value=None)
        mock_beer_repo.get_type_champions = AsyncMock(return_value=[])
        mock_beer_repo.get_milestones = AsyncMock(return_value=[])

        result = await BeerAgent().generate_weekly_recap("group-1")

        assert result == "Quiet week."
        call_args = model.aio.models.generate_content.call_args
        prompt = call_args.kwargs["contents"][0].parts[0].text
        assert "FUN FACTS" not in prompt

    @patch("src.beerbot.agent.beer_repo")
    async def test_recap_returns_none_without_client(self, mock_beer_repo, monkeypatch):
        monkeypatch.setattr(agent_module.settings, "llm_api_key", None)
        monkeypatch.setattr(agent_module.settings, "gemini_api_key", None)
        assert await BeerAgent().generate_weekly_recap("group-1") is None

    @patch("src.beerbot.agent.beer_repo")
    async def test_recap_returns_none_when_no_activity(self, mock_beer_repo, model):
        mock_beer_repo.get_group_stats = AsyncMock(return_value=MagicMock(total_beers=0))
        assert await BeerAgent().generate_weekly_recap("group-1") is None

    @pytest.mark.parametrize(
        "outcome,error",
        [(RuntimeError("API error"), "API error"), (empty_response("  "), "empty recap")],
    )
    @patch("src.beerbot.agent.beer_repo")
    async def test_model_failures_raise_so_the_scheduler_retries(
        self, mock_beer_repo, model, outcome, error
    ):
        # None means "nothing to recap" (a finished week); a failure must not look like that.
        model.aio.models.generate_content = AsyncMock(side_effect=[outcome])
        stats = MagicMock(total_beers=3, unique_drinkers=1, drink_type_counts={}, user_stats=[])
        mock_beer_repo.get_group_stats = AsyncMock(return_value=stats)
        mock_beer_repo.get_split_g_stats = AsyncMock(return_value=MagicMock(total_splits=0))
        mock_beer_repo.get_period_total = AsyncMock(return_value=0)
        mock_beer_repo.get_biggest_day = AsyncMock(return_value=None)
        mock_beer_repo.get_type_champions = AsyncMock(return_value=[])
        mock_beer_repo.get_milestones = AsyncMock(return_value=[])
        with pytest.raises((RuntimeError, ModelProtocolError), match=error):
            await BeerAgent().generate_weekly_recap("group-1")


def test_only_the_owner_gets_the_override_line(monkeypatch):
    from src.beerbot.agent import BeerAgent, settings
    from src.beerbot.models import GroupMeMessage

    monkeypatch.setattr(settings, "owner_user_id", "boss")
    agent = BeerAgent()
    for sender, expected in [("boss", True), ("someone", False)]:
        message = GroupMeMessage(
            id="m",
            group_id="g",
            user_id=sender,
            sender_id=sender,
            sender_type="user",
            name=sender,
            text="+25 for @Jay",
            created_at=0,
        )
        assert ("bot's owner" in agent._build_context_lines(message, [], None)) is expected
