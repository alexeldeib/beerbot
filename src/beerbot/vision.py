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
from .models import DrinkType, VisionResult

if TYPE_CHECKING:
    from .models import GroupMeAttachment

logger = logging.getLogger(__name__)


class VisionService:
    """Service for analyzing images using Gemini Vision API."""

    PROMPT = """Analyze this image for alcoholic drinks.

Identify drink type by GLASS/CONTAINER TYPE (not the liquid inside):
- "wine": wine glass, champagne flute, stemmed glass
- "beer": pint glass, beer mug, beer can, beer bottle
- "cocktail": cocktail glass, rocks glass, highball, martini glass, drink with garnish
- "claw": hard seltzer can (White Claw, Truly, etc.)

If no alcoholic drink container visible, return null.
If glass type is ambiguous, return null.

Split the G: true ONLY if there is a GUINNESS pint glass with beer level at the G.

Return JSON: {"drink_type": <"beer"|"wine"|"cocktail"|"claw"|null>, "split_the_g": <bool>}"""

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
                drink_type_str = data.get("drink_type")
                split_the_g = bool(data.get("split_the_g", False))

                # Parse drink type
                if drink_type_str:
                    drink_type = DrinkType.from_string(drink_type_str)
                    drink_count = 1
                else:
                    drink_type = DrinkType.BEER
                    drink_count = 0

                split_count = 1 if split_the_g else 0
                result = VisionResult(
                    drink_count=drink_count,
                    drink_type=drink_type,
                    split_the_g_count=split_count,
                    analyzed=True,
                )
            except (json.JSONDecodeError, KeyError, TypeError):
                # Fallback: check for drink types in response
                raw_lower = raw_text.lower()
                drink_type = DrinkType.BEER
                drink_count = 0
                for dt in ["beer", "wine", "cocktail", "claw"]:
                    if dt in raw_lower:
                        drink_type = DrinkType.from_string(dt)
                        drink_count = 1
                        break
                result = VisionResult(
                    drink_count=drink_count,
                    drink_type=drink_type,
                    split_the_g_count=0,
                    analyzed=True,
                )

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
        """Analyze all image attachments and return aggregated results.

        For drink type, returns the first detected drink type (most images have one drink).
        """
        if not self.client:
            return VisionResult()

        total_drinks = 0
        total_splits = 0
        any_analyzed = False
        detected_type = DrinkType.BEER  # Default
        for attachment in attachments:
            if attachment.type == "image" and attachment.url:
                result = await self.analyze_image(attachment.url)
                total_drinks += result.drink_count
                total_splits += result.split_the_g_count
                if result.analyzed:
                    any_analyzed = True
                    # Use first detected drink type
                    if result.drink_count > 0 and detected_type == DrinkType.BEER:
                        detected_type = result.drink_type

        return VisionResult(
            drink_count=total_drinks,
            drink_type=detected_type,
            split_the_g_count=total_splits,
            analyzed=any_analyzed,
        )


    async def generate_no_beer_quip(self) -> str:
        """Generate a witty quip for when an image has no beers."""
        if not self.client:
            return "Nice pic, but I don't see any beers!"

        try:
            result = await asyncio.to_thread(self._generate_quip)
            return result
        except Exception:
            logger.exception("Failed to generate quip")
            return "Nice pic, but I don't see any beers!"

    def _generate_quip(self) -> str:
        """Generate quip synchronously."""
        prompt = """Generate a single short, witty quip (under 50 characters) for a beer-tracking bot
to say when someone posts an image with no beers in it. Be playful and funny.
Just return the quip text, nothing else. No quotes."""

        response = self.client.models.generate_content(
            model=self.MODEL,
            contents=[prompt],
        )
        return response.text.strip().strip('"')

    async def generate_toast(self) -> str:
        """Generate a fun, sassy drinking toast."""
        if not self.client:
            return "Here's to good drinks and better friends! Cheers! 🍻"

        try:
            result = await asyncio.to_thread(self._generate_toast)
            return result
        except Exception:
            logger.exception("Failed to generate toast")
            return "Here's to good drinks and better friends! Cheers! 🍻"

    def _generate_toast(self) -> str:
        """Generate toast synchronously."""
        prompt = """Generate a drinking toast for a group chat's beer-tracking bot. The toast should be 2-4 sentences.

Style guidelines:
- Be genuinely funny, clever, or heartwarming - not generic
- Can be a mini-story, a fake quote from a historical figure, absurdist humor, or sincere friendship vibes
- Reference drinking culture, bad decisions, friendship, or the passage of time
- Avoid clichés like "here's to..." unless you're subverting them
- Can be slightly roast-y or self-deprecating
- End with a toast phrase and a relevant emoji

Examples of the VIBE (don't copy these, just get the energy):
- "They say you can't buy happiness, but you can buy another round, and that's basically the same thing. To poor decisions and great stories! 🍺"
- "Winston Churchill once said 'I have taken more out of alcohol than alcohol has taken out of me.' He was also wrong about a lot of things, but not this. Cheers! 🥃"
- "Here's to the nights we'll never remember with the friends we'll never forget... and to whoever's buying the next round. 🍻"

Generate ONE original toast. Just the toast text, nothing else."""

        response = self.client.models.generate_content(
            model=self.MODEL,
            contents=[prompt],
        )
        return response.text.strip().strip('"')


# Singleton instance
vision_service = VisionService()
