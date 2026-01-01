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

    PROMPT = """Analyze this image for alcoholic drinks. Return JSON with drink_type and split_the_g.

STEP 0: IS THIS A REAL PHOTO OF DRINKS?
FIRST check if this is actually a photo of real drinks. Return NULL immediately if:
- Screenshot of an app, website, or UI (text interfaces, menus, chat apps)
- Meme, cartoon, or illustration
- Photo of a screen/monitor
- Text-heavy image without real drinks visible
If you see phone UI elements, app interfaces, or mostly text = return NULL immediately!

STEP 1: CHECK FOR NON-ALCOHOLIC VARIANTS
If bottle/can shows "0.0%", "Cero", "Zero", "NA", or "Non-Alcoholic" = NULL (even if it looks like beer!)
- Corona Cero (0.0%) = NULL
- Heineken 0.0 = NULL
- Athletic Brewing = NULL
- O'Doul's = NULL

STEP 2: CHECK FOR TEQUILA/MEZCAL SHOT GLASSES
Look carefully for Mexican-style shot glasses (caballitos):
- Small narrow glass with GREEN or BLUE colored rim = COCKTAIL (tequila/mezcal glass)
- This is true even if the glass appears empty!
- Often seen with Mexican food, salsa, or in restaurant settings
- The colored rim is the key identifier - if you see green/blue rim on a shot glass = COCKTAIL

STEP 3: CHECK FOR OTHER CONSUMED COCKTAILS
Even if the glass is nearly empty, count it as COCKTAIL if you see:
- Creamy/white residue coating inside of glass + fruit (pineapple, cherry) = consumed piña colada/daiquiri → COCKTAIL
- Martini glass with olive/toothpick but no liquid → COCKTAIL
- Cocktail glass with melted ice + fruit garnish remnants → COCKTAIL

STEP 4: IDENTIFY HARD SELTZER (CLAW) - BE SPECIFIC!
Only classify as CLAW if you see these SPECIFIC BRANDS:
- White Claw (white can with colored wave logo)
- Truly (colorful can with "Truly" branding)
- High Noon (sun logo)
- Topo Chico Hard Seltzer
Any other tall can with artistic/craft design = likely BEER, not claw!

STEP 5: IDENTIFY THE CONTAINER
- Beer can (ANY tall/slim can with craft/artistic design) → BEER
- Beer flight paddle (wooden board with multiple small glasses) → BEER
- Beer glass, mug, pint, bottle → BEER
- Wine glass, champagne flute → Check liquid color
- Rocks glass, highball, martini → Could be cocktail or non-alcoholic

STEP 6: IDENTIFY THE LIQUID
- Golden/amber with foam → BEER
- Red/burgundy or pale yellow (no mixing) → WINE
- Orange, layered, or mixed colors → COCKTAIL
- Creamy/milky (like coffee with milk, no fruit/garnish) → Probably NOT alcohol, return NULL
- Clear with no alcohol indicators → NULL

STEP 7: CHECK FOR NON-ALCOHOLIC DRINKS
These are NOT alcoholic - return NULL:
- Iced coffee/latte (creamy, brown/tan, often with straw, NO fruit garnish)
- Water glasses (plain clear glass, no colored rim)
- Soft drinks
- Plain empty glasses with no distinctive features (no colored rim, no residue)

KEY RULES:
1. Craft beer cans with artistic designs = BEER (not claw!)
2. Only White Claw, Truly, High Noon, Topo Chico = CLAW
3. Beer flights (multiple small glasses on paddle/board) = BEER
4. Corona/beer with lime/orange = BEER (garnish doesn't make it cocktail)
5. Mimosa (orange in champagne flute) = COCKTAIL
6. Iced coffee in rocks glass = NULL (not alcohol!)
7. GREEN or BLUE rimmed shot glass = COCKTAIL (Mexican tequila/mezcal glass, even if empty!)
8. Glass with creamy residue + tropical fruit = COCKTAIL (consumed piña colada)
9. Check label for "0.0%" or "Cero" = NULL (non-alcoholic)
10. Screenshots/memes/UI images = NULL (not real photos!)

SPLIT THE G:
Check if Guinness glass shows beer level at/through the "G" in logo. Be generous!

Output: {"drink_type": <"beer"|"wine"|"cocktail"|"claw"|null>, "split_the_g": <bool>}"""

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
        import random

        # Pick a random style to force variety
        styles = [
            "A fake quote from a random historical figure (not Churchill or Franklin) saying something absurd about drinking",
            "A toast in the style of a medieval knight or royalty",
            "A scientific-sounding toast about the chemistry of alcohol",
            "A toast that references a specific country's drinking tradition (not Ireland or Germany)",
            "A pirate-themed toast",
            "A toast that sounds like a sports commentator",
            "A toast written like a haiku or short poem",
            "A toast that's a fake proverb from a made-up culture",
            "A toast in the style of a nature documentary narrator",
            "A toast that references Greek/Roman gods",
            "A toast written like a fortune cookie",
            "A toast in the style of a dramatic movie trailer voiceover",
        ]
        chosen_style = random.choice(styles)

        prompt = f"""Write a drinking toast in this specific style: {chosen_style}

Rules:
- 1-2 sentences max
- DO NOT start with "Here's to" or "May our" or "May your"
- DO NOT mention apps, bots, tracking, or technology
- Use a creative emoji that matches the style (not just 🍻)
- Be genuinely funny and original

Output ONLY the toast text, nothing else."""

        response = self.client.models.generate_content(
            model=self.MODEL,
            contents=[prompt],
        )
        return response.text.strip().strip('"')


class AITextParser:
    """AI-powered text parser for detecting drinks in natural language."""

    PROMPT = """Analyze this message for alcoholic drink mentions.

Return JSON: {{"drink_type": "beer"|"wine"|"cocktail"|"claw"|null, "count": 1}}

Rules:
- Look for drinking verbs: drinking, drank, had, having, finished, enjoying, sipping, nursing, polishing off
- Beer includes: IPA, lager, ale, stout, pilsner, porter, hazy, pale ale, wheat beer, hefeweizen, kolsch, etc.
- Wine includes: red, white, rosé, champagne, prosecco, pinot, chardonnay, cabernet, merlot, etc.
- Cocktail includes: margarita, martini, mojito, old fashioned, manhattan, negroni, mixed drinks, shots, whiskey, vodka, tequila, rum, gin neat, etc.
- Claw includes: White Claw, Truly, hard seltzer, High Noon only
- Return null if no alcoholic drink clearly mentioned
- Return count = 1 unless explicitly stated otherwise ("had 3 IPAs" = count 3)
- Be conservative: if uncertain, return null

Message: "{text}"
"""

    MODEL = "gemini-2.0-flash"

    def __init__(self):
        self.client = None
        if settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)

    async def parse_drink_text(self, text: str) -> tuple[int, DrinkType]:
        """Parse natural language for drink mentions using AI.

        Returns (count, drink_type) tuple. Returns (0, BEER) if no drink detected.
        """
        if not self.client or not text:
            return 0, DrinkType.BEER

        try:
            result = await asyncio.to_thread(self._call_gemini, text)
            return result
        except Exception:
            logger.exception("AI text parsing failed")
            return 0, DrinkType.BEER

    def _call_gemini(self, text: str) -> tuple[int, DrinkType]:
        """Call Gemini API synchronously (runs in thread pool)."""
        try:
            prompt = self.PROMPT.format(text=text)
            response = self.client.models.generate_content(
                model=self.MODEL,
                contents=[prompt],
            )

            raw_text = response.text.strip()
            logger.debug("AI text parser response: %r", raw_text)

            # Extract JSON from response
            json_text = raw_text
            fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
            if fence_match:
                json_text = fence_match.group(1)

            try:
                data = json.loads(json_text)
                drink_type_str = data.get("drink_type")
                count = int(data.get("count", 1))

                if drink_type_str and drink_type_str.lower() != "null":
                    drink_type = DrinkType.from_string(drink_type_str)
                    logger.info(
                        "AI text parser detected: text=%r count=%d type=%s",
                        text[:50], count, drink_type.value
                    )
                    return count, drink_type
                else:
                    return 0, DrinkType.BEER

            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                logger.warning("AI text parser JSON decode failed: %r", raw_text)
                return 0, DrinkType.BEER

        except Exception:
            logger.exception("AI text parser Gemini error")
            return 0, DrinkType.BEER


class SassyResponder:
    """AI-powered responder for generating witty replies to interesting messages."""

    DRINK_SASSY_PROMPT = """You are a witty beer-tracking bot. Someone just logged {count} {drink_type}(s).

Sender: {user_name}
Their message: "{text}"

Generate a SHORT, sassy one-liner (under 80 characters) celebrating/teasing their drink. Be:
- Playful about their drinking pace or choice
- Light trash talk about catching up on the leaderboard
- Occasionally genuinely encouraging

DO NOT:
- Repeat the drink count (they already saw that)
- Be offensive or preachy about alcohol
- Mention being a bot
- Use hashtags

Output ONLY the quip, nothing else."""

    CLASSIFICATION_PROMPT = """Analyze this GroupMe message from a beer-tracking chat group.

Message: "{text}"
Sender: {user_name}

Is this message "interesting" enough for a sassy bot reply? Return JSON:
{{"interesting": true/false, "reason": "brief reason"}}

Messages ARE interesting if they:
- Show competitive banter about drinking/leaderboard positions
- Ask questions about beer/drinking rules or pace
- Make bold claims about drinking prowess
- Express regret, excuses, or justifications about drinking
- Reference specific drinking occasions or strategies
- Trash talk other group members about their stats

Messages are NOT interesting if they:
- Are generic short responses ("Sick", "Nice", "Lol", "Ok")
- Are technical questions about the bot itself
- Are completely off-topic from drinking
- Are just sharing links without comment
- Are asking for help with bot commands

Be selective - only ~30% of messages should be interesting."""

    RESPONSE_PROMPT = """You are a witty, sassy beer-tracking bot responding to a message in a GroupMe chat.

Message: "{text}"
Sender: {user_name}
Context: {reason}

Generate a SHORT, clever response (under 100 characters). Be:
- Playful and teasing, not mean
- Reference their drinking habits or the leaderboard when relevant
- Use light trash talk about their beer count
- Occasionally encouraging but mostly sarcastic

DO NOT:
- Be offensive or hurtful
- Mention being a bot or AI
- Use generic responses
- Be preachy about drinking
- Include hashtags

Output ONLY the response text, nothing else."""

    MODEL = "gemini-2.0-flash"

    def __init__(self):
        self.client = None
        if settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)

    async def maybe_respond(self, text: str, user_name: str) -> str | None:
        """Check if message is interesting and generate a sassy response if so.

        Returns response text or None if not responding.
        """
        if not self.client or not text:
            return None

        # Skip very short or very long messages
        if len(text) < 10 or len(text) > 300:
            return None

        try:
            result = await asyncio.to_thread(self._classify_and_respond, text, user_name)
            return result
        except Exception:
            logger.exception("Sassy responder failed")
            return None

    async def generate_drink_quip(
        self, text: str, user_name: str, count: int, drink_type: str
    ) -> str | None:
        """Generate a sassy quip for a drink that was just logged.

        Returns quip text or None on failure.
        """
        if not self.client:
            return None

        try:
            result = await asyncio.to_thread(
                self._generate_drink_quip, text, user_name, count, drink_type
            )
            return result
        except Exception:
            logger.exception("Drink quip generation failed")
            return None

    def _generate_drink_quip(
        self, text: str, user_name: str, count: int, drink_type: str
    ) -> str:
        """Generate drink quip synchronously."""
        prompt = self.DRINK_SASSY_PROMPT.format(
            text=text or "",
            user_name=user_name,
            count=count,
            drink_type=drink_type,
        )
        response = self.client.models.generate_content(
            model=self.MODEL,
            contents=[prompt],
        )
        return response.text.strip().strip('"')

    def _classify_and_respond(self, text: str, user_name: str) -> str | None:
        """Classify message and generate response synchronously."""
        # Step 1: Classify if interesting
        classify_prompt = self.CLASSIFICATION_PROMPT.format(text=text, user_name=user_name)
        response = self.client.models.generate_content(
            model=self.MODEL,
            contents=[classify_prompt],
        )

        raw_text = response.text.strip()

        # Parse classification
        try:
            # Extract JSON from response
            json_text = raw_text
            fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
            if fence_match:
                json_text = fence_match.group(1)

            data = json.loads(json_text)
            is_interesting = data.get("interesting", False)
            reason = data.get("reason", "")

            if not is_interesting:
                logger.debug("Message not interesting: %r reason=%s", text[:50], reason)
                return None

            logger.info("Interesting message detected: %r reason=%s", text[:50], reason)

        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Failed to parse classification: %r", raw_text)
            return None

        # Step 2: Generate sassy response
        response_prompt = self.RESPONSE_PROMPT.format(
            text=text,
            user_name=user_name,
            reason=reason
        )
        response = self.client.models.generate_content(
            model=self.MODEL,
            contents=[response_prompt],
        )

        sassy_response = response.text.strip().strip('"')
        logger.info("Generated sassy response: %r -> %r", text[:50], sassy_response)
        return sassy_response


# Singleton instances
vision_service = VisionService()
ai_text_parser = AITextParser()
sassy_responder = SassyResponder()
