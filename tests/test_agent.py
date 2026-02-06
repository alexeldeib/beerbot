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
