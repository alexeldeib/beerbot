"""Tests for BeerAgent."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.beerbot.agent import BeerAgent, TokenBucket, extract_mentioned_users
from src.beerbot.models import GroupMeAttachment, GroupMeMessage


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


class TestBeerAgentInit:
    @patch("src.beerbot.agent.settings")
    def test_no_api_key(self, mock_settings):
        mock_settings.gemini_api_key = None
        agent = BeerAgent()
        assert agent.client is None

    @patch("src.beerbot.agent.genai")
    @patch("src.beerbot.agent.settings")
    def test_with_api_key(self, mock_settings, mock_genai):
        mock_settings.gemini_api_key = "test-key"
        mock_genai.Client.return_value = MagicMock()
        agent = BeerAgent()
        assert agent.client is not None


class TestBeerAgentProcessMessage:
    @patch("src.beerbot.agent.settings")
    async def test_returns_none_without_client(self, mock_settings):
        mock_settings.gemini_api_key = None
        agent = BeerAgent()
        result = await agent.process_message(_make_message())
        assert result is None

    @patch("src.beerbot.agent.settings")
    @patch("src.beerbot.agent.genai")
    async def test_returns_reply(self, mock_genai, mock_settings):
        mock_settings.gemini_api_key = "test-key"
        mock_settings.image_analysis_enabled = False
        mock_settings.agent_max_tool_calls = 5

        mock_response = MagicMock()
        mock_response.text = "Cheers, Alice! +1 beer logged. Total: 5."

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        mock_genai.Client.return_value = mock_client

        agent = BeerAgent()
        result = await agent.process_message(_make_message())
        assert result == "Cheers, Alice! +1 beer logged. Total: 5."

    @patch("src.beerbot.agent.settings")
    @patch("src.beerbot.agent.genai")
    async def test_suppresses_personality_when_rate_limited(self, mock_genai, mock_settings):
        mock_settings.gemini_api_key = "test-key"
        mock_settings.image_analysis_enabled = False
        mock_settings.agent_max_tool_calls = 5

        mock_response = MagicMock()
        mock_response.text = "Nice weather we're having!"

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        mock_genai.Client.return_value = mock_client

        agent = BeerAgent()
        # Drain the rate limiter
        bucket = agent._get_bucket("group-1")
        bucket.tokens = 0.0

        result = await agent.process_message(_make_message())
        # Should be suppressed since no tools were called and bucket is empty
        assert result is None

    @patch("src.beerbot.agent.settings")
    @patch("src.beerbot.agent.genai")
    async def test_handles_gemini_error(self, mock_genai, mock_settings):
        mock_settings.gemini_api_key = "test-key"
        mock_settings.image_analysis_enabled = False
        mock_settings.agent_max_tool_calls = 5

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=Exception("API error"))
        mock_genai.Client.return_value = mock_client

        agent = BeerAgent()
        result = await agent.process_message(_make_message())
        assert result is None

    @patch("src.beerbot.agent.settings")
    @patch("src.beerbot.agent.genai")
    async def test_records_conversation_history(self, mock_genai, mock_settings):
        mock_settings.gemini_api_key = "test-key"
        mock_settings.image_analysis_enabled = False
        mock_settings.agent_max_tool_calls = 5

        mock_response = MagicMock()
        mock_response.text = "Nice one!"
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        mock_genai.Client.return_value = mock_client

        agent = BeerAgent()
        await agent.process_message(_make_message(text="cheers"))

        history = agent._get_history("group-1")
        # Should have the user message and the bot reply
        assert len(history) == 2
        assert history[0].user_name == "Alice"
        assert history[0].is_bot is False
        assert history[1].user_name == "Beerius"
        assert history[1].is_bot is True


class TestChatMessageId:
    def test_record_message_stores_message_id(self):
        agent = BeerAgent()
        agent.client = None  # Not needed for history
        agent.record_message("group-1", "cheers", "Alice", message_id="msg-100")
        history = agent._get_history("group-1")
        assert history[0].message_id == "msg-100"

    def test_record_message_default_message_id_none(self):
        agent = BeerAgent()
        agent.record_message("group-1", "nice", "Beerius", is_bot=True)
        history = agent._get_history("group-1")
        assert history[0].message_id is None


class TestReplyContext:
    def test_reply_context_found_in_history(self):
        agent = BeerAgent()
        agent.record_message(
            "group-1", "+1 cocktail", "Alice", message_id="msg-050", user_id="user-042"
        )

        msg = _make_message(
            text="that was a wine not a cocktail",
            attachments=[
                GroupMeAttachment(type="reply", reply_id="msg-050"),
            ],
        )
        context = agent._build_context_lines(msg)
        assert "Replying to [Alice]" in context
        assert "+1 cocktail" in context

    def test_reply_context_includes_user_id(self):
        agent = BeerAgent()
        agent.record_message(
            "group-1", "+1 cocktail", "Alice", message_id="msg-050", user_id="user-042"
        )

        msg = _make_message(
            text="that was a wine not a cocktail",
            attachments=[
                GroupMeAttachment(type="reply", reply_id="msg-050"),
            ],
        )
        context = agent._build_context_lines(msg)
        assert "(id: user-042)" in context

    def test_reply_context_bot_message(self):
        agent = BeerAgent()
        agent.record_message(
            "group-1", "Logged 1 beer!", "Beerius", is_bot=True, message_id="msg-060"
        )

        msg = _make_message(
            text="undo that",
            attachments=[
                GroupMeAttachment(type="reply", reply_id="msg-060"),
            ],
        )
        context = agent._build_context_lines(msg)
        assert "Replying to [Beerius]: Logged 1 beer!" in context

    def test_reply_context_graceful_when_not_found(self):
        agent = BeerAgent()
        agent.record_message("group-1", "hello", "Bob", message_id="msg-001", user_id="user-002")

        msg = _make_message(
            text="that was wrong",
            attachments=[
                GroupMeAttachment(type="reply", reply_id="msg-999"),
            ],
        )
        context = agent._build_context_lines(msg)
        assert "Replying to" not in context

    def test_reply_context_no_reply_attachment(self):
        agent = BeerAgent()
        agent.record_message("group-1", "hello", "Bob", message_id="msg-001")

        msg = _make_message(text="just a normal message")
        context = agent._build_context_lines(msg)
        assert "Replying to" not in context

    @patch("src.beerbot.agent.settings")
    @patch("src.beerbot.agent.genai")
    async def test_process_message_passes_message_id_to_history(self, mock_genai, mock_settings):
        mock_settings.gemini_api_key = "test-key"
        mock_settings.image_analysis_enabled = False
        mock_settings.agent_max_tool_calls = 5

        mock_response = MagicMock()
        mock_response.text = "Got it!"
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        mock_genai.Client.return_value = mock_client

        agent = BeerAgent()
        await agent.process_message(_make_message(id="msg-123", text="cheers"))

        history = agent._get_history("group-1")
        # First entry is the user message with message_id and user_id
        assert history[0].message_id == "msg-123"
        assert history[0].user_id == "user-001"
        # Bot reply has no message_id or user_id
        assert history[1].message_id is None
        assert history[1].user_id is None

    @patch("src.beerbot.agent.settings")
    @patch("src.beerbot.agent.genai")
    async def test_image_only_message_recorded_for_reply_lookup(self, mock_genai, mock_settings):
        """Image-only messages (no text) are still recorded so reply lookup works."""
        mock_settings.gemini_api_key = "test-key"
        mock_settings.image_analysis_enabled = False
        mock_settings.agent_max_tool_calls = 5

        mock_response = MagicMock()
        mock_response.text = "Logged 1 beer!"
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        mock_genai.Client.return_value = mock_client

        agent = BeerAgent()
        # Bryan sends image-only message (text=None)
        bryan_msg = _make_message(
            id="msg-070",
            text=None,
            name="Bryan",
            user_id="user-bryan",
            sender_id="user-bryan",
            attachments=[GroupMeAttachment(type="image", url="https://example.com/beer.jpg")],
        )
        await agent.process_message(bryan_msg)

        history = agent._get_history("group-1")
        # Bryan's message should be recorded with "(image)" placeholder
        bryan_entry = next(m for m in history if m.user_id == "user-bryan")
        assert bryan_entry.message_id == "msg-070"
        assert bryan_entry.text == "(image)"

    @patch("src.beerbot.agent.settings")
    @patch("src.beerbot.agent.genai")
    async def test_reply_to_image_only_message_resolves_user(self, mock_genai, mock_settings):
        """Replying to an image-only message should resolve the original sender."""
        mock_settings.gemini_api_key = "test-key"
        mock_settings.image_analysis_enabled = False
        mock_settings.agent_max_tool_calls = 5

        mock_response = MagicMock()
        mock_response.text = "Fixed!"
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        mock_genai.Client.return_value = mock_client

        agent = BeerAgent()
        # Bryan sends image-only message
        bryan_msg = _make_message(
            id="msg-070",
            text=None,
            name="Bryan",
            user_id="user-bryan",
            sender_id="user-bryan",
        )
        await agent.process_message(bryan_msg)

        # Ace replies to Bryan's image
        captured_ctx = {}
        original_create_tools = __import__(
            "src.beerbot.tools", fromlist=["create_tools"]
        ).create_tools

        def spy_create_tools(ctx):
            captured_ctx["ctx"] = ctx
            return original_create_tools(ctx)

        with patch("src.beerbot.agent.create_tools", side_effect=spy_create_tools):
            ace_msg = _make_message(
                id="msg-071",
                text="that was a wine not a cocktail",
                name="Ace",
                user_id="user-ace",
                sender_id="user-ace",
                attachments=[GroupMeAttachment(type="reply", reply_id="msg-070")],
            )
            await agent.process_message(ace_msg)

        ctx = captured_ctx["ctx"]
        assert ctx.replied_to_user == ("user-bryan", "Bryan")

    @patch("src.beerbot.agent.settings")
    @patch("src.beerbot.agent.genai")
    async def test_process_message_sets_replied_to_user_in_ctx(self, mock_genai, mock_settings):
        """Bob replies to Alice's message — ToolContext.replied_to_user should be Alice."""
        mock_settings.gemini_api_key = "test-key"
        mock_settings.image_analysis_enabled = False
        mock_settings.agent_max_tool_calls = 5

        mock_response = MagicMock()
        mock_response.text = "Fixed!"
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        mock_genai.Client.return_value = mock_client

        agent = BeerAgent()
        # Alice sends a message first
        alice_msg = _make_message(
            id="msg-050",
            text="+1 cocktail",
            name="Alice",
            user_id="user-042",
            sender_id="user-042",
        )
        await agent.process_message(alice_msg)

        # Capture the ToolContext from Bob's reply
        captured_ctx = {}
        original_create_tools = __import__(
            "src.beerbot.tools", fromlist=["create_tools"]
        ).create_tools

        def spy_create_tools(ctx):
            captured_ctx["ctx"] = ctx
            return original_create_tools(ctx)

        with patch("src.beerbot.agent.create_tools", side_effect=spy_create_tools):
            bob_msg = _make_message(
                id="msg-051",
                text="that was a wine not a cocktail",
                name="Bob",
                user_id="user-099",
                sender_id="user-099",
                attachments=[GroupMeAttachment(type="reply", reply_id="msg-050")],
            )
            await agent.process_message(bob_msg)

        ctx = captured_ctx["ctx"]
        assert ctx.replied_to_user == ("user-042", "Alice")


class TestBeerAgentImageFetching:
    @patch("src.beerbot.agent.httpx.AsyncClient")
    async def test_fetches_image(self, mock_http_cls):
        mock_resp = MagicMock()
        mock_resp.content = b"fake-image-data"
        mock_resp.headers = {"content-type": "image/jpeg"}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        agent = BeerAgent()
        part = await agent._fetch_image("https://example.com/beer.jpg")
        assert part is not None

    @patch("src.beerbot.agent.httpx.AsyncClient")
    async def test_returns_none_on_error(self, mock_http_cls):
        import httpx

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=httpx.RequestError("timeout"))
        mock_http_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        agent = BeerAgent()
        part = await agent._fetch_image("https://example.com/beer.jpg")
        assert part is None
