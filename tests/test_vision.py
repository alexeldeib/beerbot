"""Tests for vision service."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.beerbot.models import DrinkType, GroupMeAttachment, VisionResult
from src.beerbot.vision import VisionService


class TestVisionService:
    """Tests for VisionService."""

    def test_init_without_api_key(self):
        """Test initialization without API key."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = None
            service = VisionService()
            assert service.client is None

    def test_init_with_api_key(self):
        """Test initialization with API key."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_client = MagicMock()
                mock_genai.Client.return_value = mock_client
                service = VisionService()
                mock_genai.Client.assert_called_once_with(api_key="test-key")
                assert service.client == mock_client

    @pytest.mark.asyncio
    async def test_analyze_image_no_client(self):
        """Test analyze_image returns empty VisionResult when client is not configured."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = None
            service = VisionService()
            result = await service.analyze_image("https://example.com/beer.jpg")
            assert result == VisionResult()

    @pytest.mark.asyncio
    async def test_analyze_image_success(self):
        """Test successful image analysis returns 1 drink when drink detected."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_response = MagicMock()
                mock_response.text = '{"drink_type": "beer", "split_the_g": false}'
                mock_client = MagicMock()
                mock_client.models.generate_content.return_value = mock_response
                mock_genai.Client.return_value = mock_client

                with patch("src.beerbot.vision.types") as mock_types:
                    mock_types.Part.from_bytes.return_value = MagicMock()
                    service = VisionService()

                    with patch("src.beerbot.vision.httpx.AsyncClient") as mock_http:
                        mock_response_http = MagicMock()
                        mock_response_http.content = b"fake-image-data"
                        mock_response_http.headers = {"content-type": "image/jpeg"}
                        mock_response_http.raise_for_status = MagicMock()

                        mock_http_instance = AsyncMock()
                        mock_http_instance.get.return_value = mock_response_http
                        mock_http.return_value.__aenter__.return_value = mock_http_instance

                        result = await service.analyze_image("https://example.com/beer.jpg")
                        # Returns 1 drink per image with detected drink type
                        assert result == VisionResult(drink_count=1, drink_type=DrinkType.BEER, split_the_g_count=0, analyzed=True)

    @pytest.mark.asyncio
    async def test_analyze_image_http_error(self):
        """Test image analysis handles HTTP errors."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_client = MagicMock()
                mock_genai.Client.return_value = mock_client

                service = VisionService()

                with patch("src.beerbot.vision.httpx.AsyncClient") as mock_http:
                    import httpx
                    mock_http_instance = AsyncMock()
                    mock_http_instance.get.side_effect = httpx.RequestError("Connection failed")
                    mock_http.return_value.__aenter__.return_value = mock_http_instance

                    result = await service.analyze_image("https://example.com/beer.jpg")
                    assert result == VisionResult()

    @pytest.mark.asyncio
    async def test_analyze_attachments_no_images(self):
        """Test analyze_attachments with no image attachments."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_genai.Client.return_value = MagicMock()
                service = VisionService()

                attachments = [
                    GroupMeAttachment(type="mentions", user_ids=["123"]),
                    GroupMeAttachment(type="location"),
                ]
                result = await service.analyze_attachments(attachments)
                assert result == VisionResult()

    @pytest.mark.asyncio
    async def test_analyze_attachments_with_images(self):
        """Test analyze_attachments sums results from multiple images (1 drink per image)."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_genai.Client.return_value = MagicMock()
                service = VisionService()
                # Each image with drink returns 1 drink
                service.analyze_image = AsyncMock(side_effect=[
                    VisionResult(drink_count=1, drink_type=DrinkType.BEER, split_the_g_count=0, analyzed=True),
                    VisionResult(drink_count=1, drink_type=DrinkType.BEER, split_the_g_count=1, analyzed=True),
                ])

                attachments = [
                    GroupMeAttachment(type="image", url="https://example.com/beer1.jpg"),
                    GroupMeAttachment(type="image", url="https://example.com/beer2.jpg"),
                ]
                result = await service.analyze_attachments(attachments)
                # 2 images with drinks = 2 drinks, 1 split
                assert result == VisionResult(drink_count=2, drink_type=DrinkType.BEER, split_the_g_count=1, analyzed=True)

    def test_call_gemini_parses_json(self):
        """Test _call_gemini parses JSON response with drink_type."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                with patch("src.beerbot.vision.types") as mock_types:
                    mock_response = MagicMock()
                    mock_response.text = '{"drink_type": "beer", "split_the_g": true}'
                    mock_client = MagicMock()
                    mock_client.models.generate_content.return_value = mock_response
                    mock_genai.Client.return_value = mock_client
                    mock_types.Part.from_bytes.return_value = MagicMock()

                    service = VisionService()
                    result = service._call_gemini("https://example.com/beer.jpg", b"fake-image", "image/jpeg")
                    # Returns 1 drink when drink_type is present
                    assert result == VisionResult(drink_count=1, drink_type=DrinkType.BEER, split_the_g_count=1, analyzed=True)

    def test_call_gemini_fallback_detects_drink_type(self):
        """Test _call_gemini falls back to detecting drink type in response."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                with patch("src.beerbot.vision.types") as mock_types:
                    mock_response = MagicMock()
                    mock_response.text = "I see a beer in this image"
                    mock_client = MagicMock()
                    mock_client.models.generate_content.return_value = mock_response
                    mock_genai.Client.return_value = mock_client
                    mock_types.Part.from_bytes.return_value = MagicMock()

                    service = VisionService()
                    result = service._call_gemini("https://example.com/beer.jpg", b"fake-image", "image/jpeg")
                    # Fallback detects "beer" in response
                    assert result == VisionResult(drink_count=1, drink_type=DrinkType.BEER, split_the_g_count=0, analyzed=True)

    def test_call_gemini_handles_exception(self):
        """Test _call_gemini handles API exceptions."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                with patch("src.beerbot.vision.types") as mock_types:
                    mock_client = MagicMock()
                    mock_client.models.generate_content.side_effect = Exception("API error")
                    mock_genai.Client.return_value = mock_client
                    mock_types.Part.from_bytes.return_value = MagicMock()

                    service = VisionService()
                    result = service._call_gemini("https://example.com/beer.jpg", b"fake-image", "image/jpeg")
                    assert result == VisionResult()


class TestAITextParser:
    """Tests for AITextParser."""

    def test_init_without_api_key(self):
        """Test initialization without API key."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = None
            from src.beerbot.vision import AITextParser
            parser = AITextParser()
            assert parser.client is None

    def test_init_with_api_key(self):
        """Test initialization with API key."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_client = MagicMock()
                mock_genai.Client.return_value = mock_client
                from src.beerbot.vision import AITextParser
                parser = AITextParser()
                assert parser.client == mock_client

    @pytest.mark.asyncio
    async def test_parse_drink_text_no_client(self):
        """Test parse_drink_text returns default when client is not configured."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = None
            from src.beerbot.vision import AITextParser
            parser = AITextParser()
            count, drink_type = await parser.parse_drink_text("Just had a margarita")
            assert count == 0
            assert drink_type == DrinkType.BEER

    @pytest.mark.asyncio
    async def test_parse_drink_text_empty_text(self):
        """Test parse_drink_text returns default for empty text."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_genai.Client.return_value = MagicMock()
                from src.beerbot.vision import AITextParser
                parser = AITextParser()
                count, drink_type = await parser.parse_drink_text("")
                assert count == 0
                assert drink_type == DrinkType.BEER

    def test_call_gemini_parses_beer(self):
        """Test _call_gemini parses beer response."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_response = MagicMock()
                mock_response.text = '{"drink_type": "beer", "count": 1}'
                mock_client = MagicMock()
                mock_client.models.generate_content.return_value = mock_response
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import AITextParser
                parser = AITextParser()
                count, drink_type = parser._call_gemini("Finished my IPA")
                assert count == 1
                assert drink_type == DrinkType.BEER

    def test_call_gemini_parses_cocktail(self):
        """Test _call_gemini parses cocktail response."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_response = MagicMock()
                mock_response.text = '{"drink_type": "cocktail", "count": 2}'
                mock_client = MagicMock()
                mock_client.models.generate_content.return_value = mock_response
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import AITextParser
                parser = AITextParser()
                count, drink_type = parser._call_gemini("Had 2 margaritas")
                assert count == 2
                assert drink_type == DrinkType.COCKTAIL

    def test_call_gemini_parses_wine(self):
        """Test _call_gemini parses wine response."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_response = MagicMock()
                mock_response.text = '{"drink_type": "wine", "count": 1}'
                mock_client = MagicMock()
                mock_client.models.generate_content.return_value = mock_response
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import AITextParser
                parser = AITextParser()
                count, drink_type = parser._call_gemini("Enjoying some cabernet")
                assert count == 1
                assert drink_type == DrinkType.WINE

    def test_call_gemini_returns_null_no_drink(self):
        """Test _call_gemini returns 0 when no drink detected."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_response = MagicMock()
                mock_response.text = '{"drink_type": null, "count": 0}'
                mock_client = MagicMock()
                mock_client.models.generate_content.return_value = mock_response
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import AITextParser
                parser = AITextParser()
                count, drink_type = parser._call_gemini("Hello everyone")
                assert count == 0

    def test_call_gemini_handles_json_in_markdown(self):
        """Test _call_gemini extracts JSON from markdown code fence."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_response = MagicMock()
                mock_response.text = '```json\n{"drink_type": "beer", "count": 1}\n```'
                mock_client = MagicMock()
                mock_client.models.generate_content.return_value = mock_response
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import AITextParser
                parser = AITextParser()
                count, drink_type = parser._call_gemini("Had an IPA")
                assert count == 1
                assert drink_type == DrinkType.BEER

    def test_call_gemini_handles_exception(self):
        """Test _call_gemini handles API exceptions."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_client = MagicMock()
                mock_client.models.generate_content.side_effect = Exception("API error")
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import AITextParser
                parser = AITextParser()
                count, drink_type = parser._call_gemini("Had a beer")
                assert count == 0
                assert drink_type == DrinkType.BEER

    def test_call_gemini_parses_natural_language_request(self):
        """Test _call_gemini parses natural language beer requests like 'beerius, grant me a beer'."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_response = MagicMock()
                mock_response.text = '{"drink_type": "beer", "count": 1}'
                mock_client = MagicMock()
                mock_client.models.generate_content.return_value = mock_response
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import AITextParser
                parser = AITextParser()
                # This is the key test case: natural language request
                count, drink_type = parser._call_gemini("beerius, grant me a beer")
                assert count == 1
                assert drink_type == DrinkType.BEER


class TestSassyResponder:
    """Tests for SassyResponder."""

    def test_init_without_api_key(self):
        """Test initialization without API key."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = None
            from src.beerbot.vision import SassyResponder
            responder = SassyResponder()
            assert responder.client is None

    def test_init_with_api_key(self):
        """Test initialization with API key."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_client = MagicMock()
                mock_genai.Client.return_value = mock_client
                from src.beerbot.vision import SassyResponder
                responder = SassyResponder()
                assert responder.client == mock_client

    @pytest.mark.asyncio
    async def test_maybe_respond_no_client(self):
        """Test maybe_respond returns None when client is not configured."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = None
            from src.beerbot.vision import SassyResponder
            responder = SassyResponder()
            result = await responder.maybe_respond("Test message", "TestUser")
            assert result is None

    @pytest.mark.asyncio
    async def test_maybe_respond_short_message(self):
        """Test maybe_respond skips very short messages."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_genai.Client.return_value = MagicMock()
                from src.beerbot.vision import SassyResponder
                responder = SassyResponder()
                result = await responder.maybe_respond("Hi", "TestUser")
                assert result is None

    @pytest.mark.asyncio
    async def test_maybe_respond_empty_text(self):
        """Test maybe_respond skips empty text."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_genai.Client.return_value = MagicMock()
                from src.beerbot.vision import SassyResponder
                responder = SassyResponder()
                result = await responder.maybe_respond("", "TestUser")
                assert result is None

    def test_classify_and_respond_interesting(self):
        """Test _classify_and_respond returns response for interesting message."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                # Mock two responses: classification and sassy response
                mock_classify = MagicMock()
                mock_classify.text = '{"interesting": true, "reason": "competitive banter"}'
                mock_sassy = MagicMock()
                mock_sassy.text = "Nice try, but you're still losing!"

                mock_client = MagicMock()
                mock_client.models.generate_content.side_effect = [mock_classify, mock_sassy]
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import SassyResponder
                responder = SassyResponder()
                result = responder._classify_and_respond("I'm catching up on the leaderboard!", "TestUser")
                assert result == "Nice try, but you're still losing!"

    def test_classify_and_respond_not_interesting(self):
        """Test _classify_and_respond returns None for uninteresting message."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_classify = MagicMock()
                mock_classify.text = '{"interesting": false, "reason": "generic response"}'

                mock_client = MagicMock()
                mock_client.models.generate_content.return_value = mock_classify
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import SassyResponder
                responder = SassyResponder()
                result = responder._classify_and_respond("Ok", "TestUser")
                assert result is None

    @pytest.mark.asyncio
    async def test_maybe_respond_handles_exception(self):
        """Test maybe_respond handles API exceptions gracefully."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_client = MagicMock()
                mock_client.models.generate_content.side_effect = Exception("API error")
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import SassyResponder
                responder = SassyResponder()
                # maybe_respond catches exceptions and returns None
                result = await responder.maybe_respond("Test message for sassy bot", "TestUser")
                assert result is None

    def test_format_leaderboard_context_empty(self):
        """Test _format_leaderboard_context returns empty string for empty leaderboard."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_genai.Client.return_value = MagicMock()
                from src.beerbot.vision import SassyResponder
                responder = SassyResponder()
                result = responder._format_leaderboard_context({})
                assert result == ""

    def test_format_leaderboard_context_with_data(self):
        """Test _format_leaderboard_context formats leaderboard correctly."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_genai.Client.return_value = MagicMock()
                from src.beerbot.vision import SassyResponder
                responder = SassyResponder()
                leaderboard = {"Alice": 100, "Bob": 75, "Charlie": 50}
                result = responder._format_leaderboard_context(leaderboard)
                assert "Current Leaderboard:" in result
                assert "Alice: 100 drinks" in result
                assert "Bob: 75 drinks" in result
                assert "Charlie: 50 drinks" in result

    def test_classify_and_respond_with_leaderboard(self):
        """Test _classify_and_respond includes leaderboard context in prompts."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                # Mock two responses: classification and sassy response
                mock_classify = MagicMock()
                mock_classify.text = '{"interesting": true, "reason": "asks about leaderboard"}'
                mock_sassy = MagicMock()
                mock_sassy.text = "You're 25 behind Celena - better start chugging!"

                mock_client = MagicMock()
                mock_client.models.generate_content.side_effect = [mock_classify, mock_sassy]
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import SassyResponder
                responder = SassyResponder()
                leaderboard = {"Celena": 100, "TestUser": 75}
                result = responder._classify_and_respond(
                    "Can I beat Celena on the leaderboard?",
                    "TestUser",
                    leaderboard
                )
                assert result == "You're 25 behind Celena - better start chugging!"

                # Verify leaderboard was included in the prompts
                calls = mock_client.models.generate_content.call_args_list
                assert len(calls) == 2
                # Classification prompt should include leaderboard
                classify_call = calls[0]
                assert "Celena: 100 drinks" in classify_call.kwargs["contents"][0]
                # Response prompt should also include leaderboard
                response_call = calls[1]
                assert "Celena: 100 drinks" in response_call.kwargs["contents"][0]

    @pytest.mark.asyncio
    async def test_maybe_respond_fetches_leaderboard(self):
        """Test maybe_respond fetches leaderboard when group_id is provided."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_classify = MagicMock()
                mock_classify.text = '{"interesting": true, "reason": "competitive"}'
                mock_sassy = MagicMock()
                mock_sassy.text = "Nice try!"

                mock_client = MagicMock()
                mock_client.models.generate_content.side_effect = [mock_classify, mock_sassy]
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import SassyResponder
                responder = SassyResponder()

                # Mock the stats_service module that gets imported inside maybe_respond
                with patch.dict("sys.modules", {"src.beerbot.services": MagicMock()}) as mock_modules:
                    mock_stats = MagicMock()
                    mock_stats.get_leaderboard_summary = AsyncMock(
                        return_value={"Alice": 100, "Bob": 75}
                    )
                    mock_modules["src.beerbot.services"].stats_service = mock_stats
                    result = await responder.maybe_respond(
                        "I'm gonna win this leaderboard!",
                        "TestUser",
                        group_id="test-group-123"
                    )
                    assert result == "Nice try!"
                    mock_stats.get_leaderboard_summary.assert_called_once_with("test-group-123")

    @pytest.mark.asyncio
    async def test_maybe_respond_handles_leaderboard_fetch_error(self):
        """Test maybe_respond continues even if leaderboard fetch fails."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_classify = MagicMock()
                mock_classify.text = '{"interesting": true, "reason": "competitive"}'
                mock_sassy = MagicMock()
                mock_sassy.text = "Keep trying!"

                mock_client = MagicMock()
                mock_client.models.generate_content.side_effect = [mock_classify, mock_sassy]
                mock_genai.Client.return_value = mock_client

                from src.beerbot.vision import SassyResponder
                responder = SassyResponder()

                # Mock stats_service.get_leaderboard_summary to fail
                with patch.dict("sys.modules", {"src.beerbot.services": MagicMock()}) as mock_modules:
                    mock_stats = MagicMock()
                    mock_stats.get_leaderboard_summary = AsyncMock(
                        side_effect=Exception("Database error")
                    )
                    mock_modules["src.beerbot.services"].stats_service = mock_stats
                    # Should still work, just without leaderboard data
                    result = await responder.maybe_respond(
                        "I'm gonna win this leaderboard!",
                        "TestUser",
                        group_id="test-group-123"
                    )
                    assert result == "Keep trying!"
