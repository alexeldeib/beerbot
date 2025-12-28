"""Tests for vision service."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.beerbot.models import GroupMeAttachment, VisionResult
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
        """Test successful image analysis returns 1 beer when beer detected."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_response = MagicMock()
                mock_response.text = '{"has_beer": true, "split_the_g": false}'
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
                        # Now returns 1 beer per image (not counting individual beers)
                        assert result == VisionResult(beer_count=1, split_the_g_count=0, analyzed=True)

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
        """Test analyze_attachments sums results from multiple images (1 beer per image)."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                mock_genai.Client.return_value = MagicMock()
                service = VisionService()
                # Each image with beer returns 1 beer
                service.analyze_image = AsyncMock(side_effect=[
                    VisionResult(beer_count=1, split_the_g_count=0, analyzed=True),
                    VisionResult(beer_count=1, split_the_g_count=1, analyzed=True),
                ])

                attachments = [
                    GroupMeAttachment(type="image", url="https://example.com/beer1.jpg"),
                    GroupMeAttachment(type="image", url="https://example.com/beer2.jpg"),
                ]
                result = await service.analyze_attachments(attachments)
                # 2 images with beer = 2 beers, 1 split
                assert result == VisionResult(beer_count=2, split_the_g_count=1, analyzed=True)

    def test_call_gemini_parses_json(self):
        """Test _call_gemini parses JSON response with has_beer boolean."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                with patch("src.beerbot.vision.types") as mock_types:
                    mock_response = MagicMock()
                    mock_response.text = '{"has_beer": true, "split_the_g": true}'
                    mock_client = MagicMock()
                    mock_client.models.generate_content.return_value = mock_response
                    mock_genai.Client.return_value = mock_client
                    mock_types.Part.from_bytes.return_value = MagicMock()

                    service = VisionService()
                    result = service._call_gemini("https://example.com/beer.jpg", b"fake-image", "image/jpeg")
                    # Returns 1 beer when has_beer is true
                    assert result == VisionResult(beer_count=1, split_the_g_count=1, analyzed=True)

    def test_call_gemini_fallback_detects_true(self):
        """Test _call_gemini falls back to detecting 'true' in response."""
        with patch("src.beerbot.vision.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            with patch("src.beerbot.vision.genai") as mock_genai:
                with patch("src.beerbot.vision.types") as mock_types:
                    mock_response = MagicMock()
                    mock_response.text = "Yes, there is a beer. has_beer: true"
                    mock_client = MagicMock()
                    mock_client.models.generate_content.return_value = mock_response
                    mock_genai.Client.return_value = mock_client
                    mock_types.Part.from_bytes.return_value = MagicMock()

                    service = VisionService()
                    result = service._call_gemini("https://example.com/beer.jpg", b"fake-image", "image/jpeg")
                    # Fallback detects "true" in response
                    assert result == VisionResult(beer_count=1, split_the_g_count=0, analyzed=True)

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
