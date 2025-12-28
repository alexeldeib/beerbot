"""Vision service for analyzing images for beer content using Gemini."""

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

from google import genai
from google.genai import types
import httpx

from .config import settings
from .models import VisionResult

if TYPE_CHECKING:
    from .models import GroupMeAttachment

logger = logging.getLogger(__name__)


class VisionService:
    """Service for analyzing images using Gemini Vision API."""

    PROMPT = """Analyze this image for BEERS ONLY and Guinness "Split the G" achievements.

TASK 1 - Count beers: Count ONLY actual beers (beer glasses, beer cans, beer bottles, pints, mugs with beer).
DO NOT count: wine glasses, champagne flutes, cocktails, spirits, water, soda, or any non-beer drinks.
Wine glasses are tall and thin with a stem - these are NOT beers.

TASK 2 - Detect "Split the G": For any Guinness pint glass, check if the beer level
(where the liquid meets the glass) crosses through or touches the "G" letter in
"GUINNESS" printed on the glass. If the liquid line is at, near, or through the G,
count it as a split.

Return JSON: {"beer_count": <int>, "split_the_g_count": <int>}

IMPORTANT: Only count BEERS. Wine, champagne, and cocktails do NOT count.
If the beer level is anywhere near the word GUINNESS (especially touching or crossing the G),
that counts as split_the_g_count = 1. If no beers visible, return {"beer_count": 0, "split_the_g_count": 0}"""

    MODEL = "gemini-2.0-flash"

    def __init__(self):
        self.client = None
        if settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)

    async def analyze_image(self, image_url: str) -> VisionResult:
        """Analyze an image URL and return beer count and split-the-G detection."""
        if not self.client:
            logger.warning("Vision analysis skipped: no Gemini API key configured")
            return VisionResult()

        try:
            # Fetch the image (follow redirects - GroupMe redirects to CDN)
            async with httpx.AsyncClient(follow_redirects=True) as http_client:
                response = await http_client.get(image_url, timeout=10.0)
                response.raise_for_status()
                image_data = response.content
                content_type = response.headers.get("content-type", "image/jpeg")
                logger.debug(
                    "Fetched image: url=%s size=%d content_type=%s",
                    image_url, len(image_data), content_type
                )

            # Run Gemini in a thread pool (it's sync)
            result = await asyncio.to_thread(
                self._call_gemini, image_url, image_data, content_type
            )
            return result

        except httpx.RequestError as e:
            logger.error("Failed to fetch image: url=%s error=%s", image_url, str(e))
            return VisionResult()
        except Exception:
            logger.exception("Unexpected error analyzing image: url=%s", image_url)
            return VisionResult()

    def _call_gemini(self, image_url: str, image_data: bytes, content_type: str) -> VisionResult:
        """Call Gemini API synchronously (runs in thread pool)."""
        try:
            # Create image part for the new SDK
            image_part = types.Part.from_bytes(data=image_data, mime_type=content_type)

            response = self.client.models.generate_content(
                model=self.MODEL,
                contents=[self.PROMPT, image_part],
            )

            raw_text = response.text.strip()

            # Try to extract JSON from response (may be wrapped in markdown or text)
            json_text = raw_text

            # Look for JSON in markdown code fence anywhere in response
            fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
            if fence_match:
                json_text = fence_match.group(1)

            try:
                data = json.loads(json_text)
                beer_count = int(data.get("beer_count", 0))
                split_count = int(data.get("split_the_g_count", 0))
                result = VisionResult(beer_count=beer_count, split_the_g_count=split_count, analyzed=True)
            except (json.JSONDecodeError, KeyError, TypeError):
                # Fallback: extract first integer for beer count (backward compat)
                match = re.search(r"\d+", raw_text)
                beer_count = int(match.group()) if match else 0
                result = VisionResult(beer_count=beer_count, split_the_g_count=0, analyzed=True)

            logger.info(
                "Gemini analysis: url=%s raw_response=%r beer_count=%d split_the_g=%d",
                image_url, raw_text, result.beer_count, result.split_the_g_count
            )
            return result

        except Exception:
            logger.exception("Gemini API error: url=%s", image_url)
            return VisionResult()

    def analyze_image_sync(self, image_data: bytes, content_type: str = "image/jpeg") -> VisionResult:
        """Analyze image data synchronously (for local testing)."""
        if not self.client:
            return VisionResult()
        return self._call_gemini("local", image_data, content_type)

    async def analyze_attachments(
        self, attachments: list["GroupMeAttachment"]
    ) -> VisionResult:
        """Analyze all image attachments and return aggregated results."""
        if not self.client:
            return VisionResult()

        total_beers = 0
        total_splits = 0
        any_analyzed = False
        for attachment in attachments:
            if attachment.type == "image" and attachment.url:
                result = await self.analyze_image(attachment.url)
                total_beers += result.beer_count
                total_splits += result.split_the_g_count
                if result.analyzed:
                    any_analyzed = True

        return VisionResult(beer_count=total_beers, split_the_g_count=total_splits, analyzed=any_analyzed)


# Singleton instance
vision_service = VisionService()
