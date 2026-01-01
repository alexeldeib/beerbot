"""Vision service for analyzing images for beer content using Gemini."""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from google import genai
from google.genai import types
import httpx

from .config import settings
from .models import DrinkType, VisionResult

if TYPE_CHECKING:
    from .models import GroupMeAttachment

logger = logging.getLogger(__name__)


@dataclass
class TokenBucket:
    """Token bucket rate limiter for controlling response frequency.

    Allows bursts of responses while limiting sustained rate.
    Default: 5 tokens capacity, refills at 1 token per minute.
    """
    capacity: int = 5
    refill_rate: float = 1 / 60  # 1 token per minute
    tokens: float = field(default=5.0)
    last_refill: float = field(default_factory=time.time)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def consume(self) -> bool:
        """Try to consume a token. Returns True if successful, False if empty."""
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def peek(self) -> bool:
        """Check if a token is available without consuming it."""
        self._refill()
        return self.tokens >= 1


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

CRITICAL: Only return a drink if the person is CLEARLY stating they are CURRENTLY drinking or have JUST finished drinking.

Return null (no drink) for:
- Messages about the bot or app (beerius, bot, tracker)
- Messages ABOUT drinking conceptually (not actually drinking)
- Sports team names (Miami Hurricanes, etc.)
- Casual conversation that happens to mention drinks
- Metaphors, jokes, or references that aren't actual consumption
- Commands, questions about the bot, or meta-commentary
- Someone else drinking (must be the message sender)
- Past tense beyond "just had" (stories about yesterday don't count)

Only return a drink for:
- Clear present tense: "drinking an IPA right now", "having a beer"
- Just finished: "just had a margarita", "finished my wine"
- Explicit logging: "beerius give me a beer", "grant me a cocktail"
- Cheers/toasts with context indicating current drinking

Categories if drink IS detected:
- beer: IPA, lager, ale, stout, pilsner, porter, etc.
- wine: red, white, rosé, champagne, prosecco, etc.
- cocktail: margarita, martini, shots, whiskey, vodka, mixed drinks, etc.
- claw: White Claw, Truly, hard seltzer, High Noon only

Count = 1 unless explicitly stated ("had 3 beers" = count 3)

When in doubt, return null. False negatives are better than false positives.

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
{leaderboard_context}
Is this message "interesting" enough for a sassy bot reply? Return JSON:
{{"interesting": true/false, "reason": "brief reason"}}

ONLY respond "interesting": true if the message EXPLICITLY and DIRECTLY:
- Directly addresses the bot by name (beerius) with a question or challenge
- Makes a specific claim about beating someone on the leaderboard BY NAME
- Explicitly asks about leaderboard positions or drink counts
- Is clearly directed AT the bot requesting a response

Messages are NOT interesting (default to false):
- Generic chat messages, even if they mention drinking casually
- Messages about sports, work, life, or anything not directly about THIS leaderboard
- Short responses ("Sick", "Nice", "Lol", "Ok", "Fuck off")
- Questions or comments to other users (not the bot)
- Messages that are just conversation between friends
- Anything where replying would feel intrusive or annoying

Be VERY selective - only ~10% of messages warrant a bot response.
When in doubt, return false. The bot should feel like a rare treat, not a constant presence."""

    RESPONSE_PROMPT = """You are a witty, sassy beer-tracking bot responding to a message in a GroupMe chat.

Message: "{text}"
Sender: {user_name}
Context: {reason}
{leaderboard_context}
Generate a SHORT, clever response (under 100 characters). Be:
- Playful and teasing, not mean
- Reference their drinking habits or the leaderboard when relevant
- Use ACTUAL leaderboard data when provided to make responses specific and accurate
- Use light trash talk about their beer count
- Occasionally encouraging but mostly sarcastic

DO NOT:
- Be offensive or hurtful
- Mention being a bot or AI
- Use generic responses
- Be preachy about drinking
- Include hashtags
- Make up drink counts - use the leaderboard data provided

Output ONLY the response text, nothing else."""

    MODEL = "gemini-2.0-flash"

    def __init__(self):
        self.client = None
        if settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)
        # Token bucket rate limiter per group (5 burst, 1/min refill)
        self._rate_limiters: dict[str, TokenBucket] = {}

    def _get_bucket(self, group_id: str) -> TokenBucket:
        """Get or create token bucket for a group."""
        if group_id not in self._rate_limiters:
            self._rate_limiters[group_id] = TokenBucket()
        return self._rate_limiters[group_id]

    def _try_consume(self, group_id: str | None) -> bool:
        """Try to consume a rate limit token. Returns True if allowed."""
        if not group_id:
            return True
        return self._get_bucket(group_id).consume()

    async def maybe_respond(
        self, text: str, user_name: str, group_id: str | None = None
    ) -> str | None:
        """Check if message is interesting and generate a sassy response if so.

        Args:
            text: The message text
            user_name: The sender's name
            group_id: Optional group ID to fetch leaderboard context

        Returns response text or None if not responding.
        """
        if not self.client or not text:
            return None

        # Skip very short or very long messages
        if len(text) < 10 or len(text) > 300:
            return None

        # Skip messages about the bot itself (meta-commentary)
        if "beerius" in text.lower():
            # Allow if directly addressing the bot with a question
            if "?" not in text:
                return None

        # Check rate limit - don't respond too frequently to the same group
        if group_id and not self._get_bucket(group_id).peek():
            logger.debug("Skipping sassy response - on cooldown for group %s", group_id)
            return None

        # Fetch leaderboard context if group_id provided
        leaderboard: dict[str, int] = {}
        if group_id:
            try:
                from .services import stats_service
                leaderboard = await stats_service.get_leaderboard_summary(group_id)
            except Exception:
                logger.exception("Failed to fetch leaderboard for sassy response")

        try:
            result = await asyncio.to_thread(
                self._classify_and_respond, text, user_name, leaderboard
            )
            if result:
                # Consume a token when we actually respond
                self._try_consume(group_id)
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

    def _format_leaderboard_context(self, leaderboard: dict[str, int]) -> str:
        """Format leaderboard data for prompt context."""
        if not leaderboard:
            return ""

        lines = ["Current Leaderboard:"]
        for i, (name, count) in enumerate(leaderboard.items(), 1):
            lines.append(f"  {i}. {name}: {count} drinks")
        return "\n".join(lines) + "\n"

    def _classify_and_respond(
        self, text: str, user_name: str, leaderboard: dict[str, int] | None = None
    ) -> str | None:
        """Classify message and generate response synchronously."""
        leaderboard_context = self._format_leaderboard_context(leaderboard or {})

        # Step 1: Classify if interesting
        classify_prompt = self.CLASSIFICATION_PROMPT.format(
            text=text,
            user_name=user_name,
            leaderboard_context=leaderboard_context,
        )
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
            reason=reason,
            leaderboard_context=leaderboard_context,
        )
        response = self.client.models.generate_content(
            model=self.MODEL,
            contents=[response_prompt],
        )

        sassy_response = response.text.strip().strip('"')
        logger.info("Generated sassy response: %r -> %r", text[:50], sassy_response)
        return sassy_response


class MessageClassifier:
    """Unified AI-powered message classifier for all response types."""

    CLASSIFICATION_PROMPT = """Classify this GroupMe message from a beer-tracking chat.

Message: "{text}"
Sender: {user_name}
{leaderboard_context}

Return JSON:
{{
  "type": "question"|"drink"|"reply"|"sassy"|"ignore",
  "question_type": "million"|"leaderboard"|"user_stats"|"compare"|null,
  "question_target": "user name if asking about someone"|null,
  "drink_count": number|null,
  "drink_type": "beer"|"wine"|"cocktail"|"claw"|null,
  "confidence": "high"|"medium"|"low",
  "reason": "brief explanation"
}}

=== IGNORE (default - classify as "ignore" unless there's a STRONG reason not to) ===

ALWAYS IGNORE - handled by regex, never classify:
- +N or -N patterns: "+1 beer", "-3 mimosas", "+2 wines @user", "+1 bubbly"
- Trigger phrases: "beer me", "wine me", "cheers", "cracked one", "cracked a cold one"
- Drink emojis anywhere in message: 🍺🍷🍸🍹🥃 (if message contains these, regex handles it)
- Commands starting with !

ALWAYS IGNORE - bot functionality and rules discussions:
- Questions about bot rules: "Can we retroactively add beers?", "What's the statute of limitations?"
- Questions about counting: "So each drink only counts as 1/2?", "Does X count?"
- Discussions about features: "we can add X feature", "the bot should do Y", "think X works now"
- Comments about bot behavior: "BBB drinks don't count", "drinks before bot don't count"
- Meta-commentary: "the keywords are dumb", "needs more prompt engineering"
- Bot criticism: "Bad bot", "Rude bot", "Bot doesn't recognize X"
- Jokes with large numbers: "I'm about to send 1,000,000 beers" (hyperbole, not real)

ALWAYS IGNORE - not actionable:
- Short reactions: "ok", "nice", "lol", "Sick", "FINE", "Bruh", "Omg", "Tough"
- One-word responses and acknowledgments
- Links, memes, photos (context not available)
- Context-specific jargon: "ABB beers", "BBB drinks" (Before/After Beer Bot)
- Generic conversation not about actual drinking or leaderboard competition

=== QUESTION (user genuinely wants data from the bot) ===

ONLY classify as "question" if user is DIRECTLY requesting information:
- "How many until 1 million?" → million
- "I wonder how many drinks we have left" → million (indirect but genuine curiosity)
- "Who's winning?" / "What's the leaderboard?" → leaderboard
- "How many beers has Bryan had?" → user_stats, target=Bryan
- "Can I beat Celena?" / "How far behind am I?" → compare

DO NOT classify as "question":
- Statements about goals: "We need a million", "We're almost there" (statements, not questions)
- Rhetorical observations: "How is her lead widening" (observation, not request)
- Bot functionality questions: "Can we retroactively add beers?" (asking about rules, not stats)
- Questions with attached photos (probably joke about the photo, ignore)
- Hypothetical questions about drinking in general

=== DRINK (user is ACTIVELY drinking - rare, natural language only) ===

ONLY classify as "drink" when:
- Clear PRESENT TENSE statement: "Having a beer right now", "Drinking an IPA"
- JUST finished: "Just finished my margarita", "just had a beer"
- "Beer me" WITH MODIFIERS indicating count:
  - "Beer me not once but twice" → count=2, type=beer
  - "Beer me twice", "beer me x3" → extract count
  - These are NOT handled by regex because they have additional words!

IMPORTANT: Simple "beer me" or "wine me" alone is handled by regex and should be IGNORED.
But "beer me" followed by count words (twice, not once but twice, x2, etc.) = DRINK.

NEVER classify as "drink":
- Future tense: "Guess I gotta order another" (hasn't happened yet)
- Metaphors: "rookie coming in hot off the draft" (sports/newcomer metaphor)
- Past stories: "I drank 5 beers yesterday" (not current)
- Someone else drinking: "They're having wine"
- Simple regex patterns: "+N beer", "-N drinks", drink emojis alone

Drink types:
- beer: IPA, lager, ale, stout, pilsner
- wine: red, white, rosé
- cocktail: margarita, martini, shots, whiskey, champagne, bubbly (sparkling wine)
- claw: ONLY White Claw, Truly, High Noon, Topo Chico Hard Seltzer

=== REPLY (simple acknowledgment - brief, friendly response) ===

Use "reply" for messages that warrant a brief, friendly response but NOT a witty/sassy one:
- Thanks directed at bot: "Thanks beerius", "Thanks bot", "Thanks beerbot"
- Simple appreciations after bot action: "Nice", "Thanks", "Appreciated"
- Brief positive reactions to bot behavior (not just one word in general chat)

Response for "reply" should be brief and friendly, not trying to be clever.
Only use when user is clearly acknowledging/thanking the bot.

=== SASSY (genuinely interesting, ~5-10% of messages) ===

ONLY classify as "sassy" when the message is:
- Genuinely funny or clever trash talk about drinking/competition
- A bold claim that invites a witty response
- Something unusual or amusing worth commenting on

DO NOT classify as "sassy":
- Simple thanks or acknowledgments (use "reply" instead)
- Short reactions or one-word messages in general chat
- Generic positive/negative responses
- Routine conversation between friends
- Bot criticism or meta-commentary: "Bad bot", "needs more prompt engineering" (ignore these)
- Hyperbolic jokes: "I'm about to send 1,000,000 beers" (ignore)
- Anything where bot response would feel intrusive

=== FINAL CHECK ===
When in doubt, return "ignore". False positives are worse than false negatives.
The bot should feel like a rare, welcome presence - not an annoying constant."""

    # AI-only mode prompt: classifies ALL drink patterns including +N, emojis, triggers
    AI_ONLY_CLASSIFICATION_PROMPT = """Classify this GroupMe message from a beer-tracking chat.

Message: "{text}"
Sender: {user_name}
{leaderboard_context}

Return JSON:
{{
  "type": "question"|"drink"|"reply"|"sassy"|"ignore",
  "question_type": "million"|"leaderboard"|"user_stats"|"compare"|null,
  "question_target": "user name if asking about someone"|null,
  "drink_count": number|null,
  "drink_type": "beer"|"wine"|"cocktail"|"claw"|null,
  "confidence": "high"|"medium"|"low",
  "reason": "brief explanation"
}}

=== DRINK (AI-only mode - classify ALL drink patterns) ===

CLASSIFY AS DRINK - explicit drink logging:
- +N patterns: "+1 beer", "+2 wines", "+3 cocktails" → extract count and type
- -N patterns: "-1 beer", "-2 drinks" → type="drink", negative count (removal)
- Drink emojis: 🍺🍷🍸🍹🥃🍻 → count emojis, type based on emoji
- Trigger phrases: "beer me", "wine me", "cheers", "cracked one" → count=1
- "Beer me" WITH MODIFIERS: "beer me twice", "beer me not once but twice" → count=2
- Natural language: "Having a beer", "Just finished my IPA" → extract count/type
- Brand names count: "+1 Moët" (champagne=cocktail), "+1 Corona" (beer)

Drink types:
- beer: IPA, lager, ale, stout, pilsner, Corona, Heineken, any brew
- wine: red, white, rosé (still wine only)
- cocktail: margarita, martini, shots, whiskey, champagne, Moët, bubbly, prosecco, mimosa
- claw: ONLY White Claw, Truly, High Noon, Topo Chico Hard Seltzer

=== IGNORE (default for non-drink messages) ===

ALWAYS IGNORE:
- Commands starting with !
- Bot functionality discussions: "Can we add X feature?", "the bot should do Y"
- Questions about rules: "Does X count?", "What's the statute of limitations?"
- Meta-commentary: "needs more prompt engineering", "Bad bot"
- Short reactions: "ok", "nice", "lol", "Sick"
- Generic conversation not about drinking
- Future tense: "Guess I gotta order another" (hasn't happened yet)
- Past stories: "I drank 5 beers yesterday" (not current session)
- Jokes with large numbers: "I'm about to send 1,000,000 beers"
- Context-specific jargon: "ABB beers", "BBB drinks"

=== QUESTION (user genuinely wants data) ===

- "How many until 1 million?" → million
- "Who's winning?" → leaderboard
- "How many beers has Bryan had?" → user_stats, target=Bryan
- "Can I beat Celena?" → compare

=== SASSY (~5-10% of messages, genuinely interesting) ===

Only for genuinely funny trash talk or bold claims about drinking/competition.
DO NOT classify as sassy: short reactions, bot criticism, meta-commentary.

=== FINAL CHECK ===
Prioritize drink detection. When message clearly indicates drinking, classify as drink.
When in doubt about non-drink messages, return "ignore"."""

    MODEL = "gemini-2.0-flash"

    def __init__(self):
        self.client = None
        if settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)
        # Token bucket rate limiter per group (5 burst, 1/min refill)
        self._rate_limiters: dict[str, TokenBucket] = {}

    def _get_bucket(self, group_id: str) -> TokenBucket:
        """Get or create token bucket for a group."""
        if group_id not in self._rate_limiters:
            self._rate_limiters[group_id] = TokenBucket()
        return self._rate_limiters[group_id]

    def _try_consume(self, group_id: str | None) -> bool:
        """Try to consume a rate limit token. Returns True if allowed."""
        if not group_id:
            return True
        return self._get_bucket(group_id).consume()

    def _can_respond(self, group_id: str | None) -> bool:
        """Check if we can respond (without consuming token)."""
        if not group_id:
            return True
        return self._get_bucket(group_id).peek()

    async def classify(
        self,
        text: str,
        user_name: str,
        group_id: str | None = None,
        skip_cooldown: bool = False,
        ai_only: bool = False,
    ) -> dict:
        """Classify a message and return classification details.

        Args:
            text: The message text
            user_name: The sender's name
            group_id: Optional group ID to fetch leaderboard context
            skip_cooldown: If True, skip cooldown check (for testing)
            ai_only: If True, skip regex pre-filter and classify all patterns with AI

        Returns dict with:
            - type: "question"|"drink"|"sassy"|"ignore"
            - question_type: "million"|"leaderboard"|"user_stats"|"compare"|None
            - question_target: user name if asking about someone, else None
            - drink_count: int or None
            - drink_type: "beer"|"wine"|"cocktail"|"claw"|None
            - confidence: "high"|"medium"|"low"
            - reason: explanation string
        """
        default_result = {
            "type": "ignore",
            "question_type": None,
            "question_target": None,
            "drink_count": None,
            "drink_type": None,
            "confidence": "high",
            "reason": "default",
        }

        if not self.client or not text:
            return default_result

        # Skip very short messages
        if len(text) < 3:
            default_result["reason"] = "too short"
            return default_result

        # Skip commands (handled separately even in AI-only mode)
        if text.startswith("!"):
            default_result["reason"] = "command"
            return default_result

        # Skip regex patterns (handled separately) - defense in depth
        # In AI-only mode, let AI classify these patterns instead
        if not ai_only:
            # +N/-N patterns
            if re.match(r'^[+-]\d+\s*(beer|wine|cocktail|claw|shot|drink|mimosa)', text, re.IGNORECASE):
                default_result["reason"] = "regex pattern (+N/-N)"
                return default_result
            # Drink emojis
            if any(emoji in text for emoji in ['🍺', '🍷', '🍸', '🍹', '🥃', '🍻']):
                default_result["reason"] = "drink emoji"
                return default_result
            # Trigger phrases - only simple ones without modifiers
            # "beer me" or "beer me @user" is handled by regex
            # "beer me not once but twice" has modifiers and should go to AI
            text_lower = text.lower().strip()
            simple_triggers = [
                'beer me', 'wine me', 'cheers', 'cracked one', 'cracked a cold one'
            ]
            # Only match if it's a simple trigger (optionally with @mention at end)
            for trigger in simple_triggers:
                if text_lower == trigger or text_lower.startswith(trigger + ' @'):
                    default_result["reason"] = "trigger phrase"
                    return default_result

        # Check rate limit for sassy/question responses (not for drink detection)
        can_respond = self._can_respond(group_id) or skip_cooldown

        # Fetch leaderboard context if group_id provided
        leaderboard: dict[str, int] = {}
        if group_id:
            try:
                from .services import stats_service
                leaderboard = await stats_service.get_leaderboard_summary(group_id)
            except Exception:
                logger.exception("Failed to fetch leaderboard for classification")

        try:
            result = await asyncio.to_thread(
                self._classify_sync, text, user_name, leaderboard, ai_only
            )

            # If rate limited, downgrade sassy/question to ignore
            if not can_respond and result["type"] in ("sassy", "question"):
                logger.debug(
                    "Downgrading %s to ignore due to rate limit for group %s",
                    result["type"], group_id
                )
                result["type"] = "ignore"
                result["reason"] = f"rate_limited (was: {result['reason']})"

            # Consume token if we're going to respond
            if result["type"] in ("sassy", "question"):
                self._try_consume(group_id)

            return result

        except Exception:
            logger.exception("Message classification failed")
            return default_result

    def _format_leaderboard_context(self, leaderboard: dict[str, int]) -> str:
        """Format leaderboard data for prompt context."""
        if not leaderboard:
            return ""

        lines = ["Current Leaderboard:"]
        for i, (name, count) in enumerate(leaderboard.items(), 1):
            lines.append(f"  {i}. {name}: {count} drinks")
        return "\n".join(lines) + "\n"

    def _classify_sync(
        self, text: str, user_name: str, leaderboard: dict[str, int], ai_only: bool = False
    ) -> dict:
        """Classify message synchronously (runs in thread pool)."""
        leaderboard_context = self._format_leaderboard_context(leaderboard)

        # Choose prompt based on mode
        if ai_only:
            prompt = self.AI_ONLY_CLASSIFICATION_PROMPT.format(
                text=text,
                user_name=user_name,
                leaderboard_context=leaderboard_context,
            )
        else:
            prompt = self.CLASSIFICATION_PROMPT.format(
                text=text,
                user_name=user_name,
                leaderboard_context=leaderboard_context,
            )

        response = self.client.models.generate_content(
            model=self.MODEL,
            contents=[prompt],
        )

        raw_text = response.text.strip()

        # Parse JSON response
        try:
            json_text = raw_text
            fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
            if fence_match:
                json_text = fence_match.group(1)

            data = json.loads(json_text)

            result = {
                "type": data.get("type", "ignore"),
                "question_type": data.get("question_type"),
                "question_target": data.get("question_target"),
                "drink_count": data.get("drink_count"),
                "drink_type": data.get("drink_type"),
                "confidence": data.get("confidence", "medium"),
                "reason": data.get("reason", ""),
            }

            # Validate type
            if result["type"] not in ("question", "drink", "reply", "sassy", "ignore"):
                result["type"] = "ignore"

            logger.info(
                "Classified message: %r -> type=%s reason=%s",
                text[:50], result["type"], result["reason"]
            )

            return result

        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Failed to parse classification: %r", raw_text)
            return {
                "type": "ignore",
                "question_type": None,
                "question_target": None,
                "drink_count": None,
                "drink_type": None,
                "confidence": "low",
                "reason": "parse error",
            }


class QuestionAnswerer:
    """Generate natural language answers to classified questions."""

    ANSWER_PROMPT = """You are a witty beer-tracking bot answering a question.

Question type: {question_type}
Original message: "{text}"
Sender: {user_name}
{context_data}

Generate a helpful, natural response (under 150 characters). Be:
- Informative first, witty second
- Use ACTUAL numbers from the context data
- Friendly but slightly sarcastic
- Encouraging or teasing based on the situation

DO NOT:
- Make up numbers - only use data provided
- Be mean or discouraging
- Mention being a bot/AI
- Use hashtags

Output ONLY the response text."""

    MODEL = "gemini-2.0-flash"

    def __init__(self):
        self.client = None
        if settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)

    async def answer(
        self,
        text: str,
        user_name: str,
        question_type: str,
        question_target: str | None,
        group_id: str,
    ) -> str | None:
        """Generate an answer to a classified question.

        Args:
            text: Original message text
            user_name: Sender's name
            question_type: "million"|"leaderboard"|"user_stats"|"compare"
            question_target: Target user name for compare/user_stats questions
            group_id: Group ID to fetch data from

        Returns answer string or None on failure.
        """
        if not self.client:
            return None

        try:
            # Fetch relevant context data based on question type
            context_data = await self._fetch_context(
                question_type, question_target, user_name, group_id
            )

            result = await asyncio.to_thread(
                self._generate_answer, text, user_name, question_type, context_data
            )
            return result

        except Exception:
            logger.exception("Question answering failed")
            return None

    async def _fetch_context(
        self,
        question_type: str,
        question_target: str | None,
        user_name: str,
        group_id: str,
    ) -> str:
        """Fetch relevant data for answering the question."""
        from .services import stats_service
        from .repositories import beer_repo

        lines = []

        if question_type == "million":
            # Get million countdown data
            total = await beer_repo.get_group_total_by_type(group_id, None)
            _, beers_7d, days_7d = await beer_repo.get_rate_stats(group_id, days=7)
            _, beers_30d, days_30d = await beer_repo.get_rate_stats(group_id, days=30)

            lines.append(f"Total drinks: {total:,}")
            lines.append(f"Remaining to 1 million: {1_000_000 - total:,}")

            if days_7d > 0 and beers_7d > 0:
                rate = beers_7d / days_7d
                days_left = (1_000_000 - total) / rate
                lines.append(f"7-day pace: {rate:.1f} drinks/day")
                lines.append(f"At this pace: {days_left:.0f} days ({days_left/365:.1f} years)")

        elif question_type == "leaderboard":
            leaderboard = await stats_service.get_leaderboard_summary(group_id)
            lines.append("Leaderboard:")
            for i, (name, count) in enumerate(leaderboard.items(), 1):
                lines.append(f"  {i}. {name}: {count}")

        elif question_type == "compare":
            leaderboard = await stats_service.get_leaderboard_summary(group_id)
            sender_count = leaderboard.get(user_name, 0)
            target_count = leaderboard.get(question_target, 0) if question_target else 0

            lines.append(f"{user_name}'s count: {sender_count}")
            if question_target:
                lines.append(f"{question_target}'s count: {target_count}")
                gap = target_count - sender_count
                lines.append(f"Gap: {abs(gap)} ({'behind' if gap > 0 else 'ahead'})")

        elif question_type == "user_stats":
            leaderboard = await stats_service.get_leaderboard_summary(group_id)
            target = question_target or user_name
            count = leaderboard.get(target, 0)
            lines.append(f"{target}'s total: {count} drinks")

            # Find their rank
            for i, (name, _) in enumerate(leaderboard.items(), 1):
                if name == target:
                    lines.append(f"Rank: #{i}")
                    break

        return "\n".join(lines) if lines else "No data available"

    def _generate_answer(
        self, text: str, user_name: str, question_type: str, context_data: str
    ) -> str:
        """Generate answer synchronously."""
        prompt = self.ANSWER_PROMPT.format(
            text=text,
            user_name=user_name,
            question_type=question_type,
            context_data=context_data,
        )

        response = self.client.models.generate_content(
            model=self.MODEL,
            contents=[prompt],
        )

        return response.text.strip().strip('"')


class UnifiedBeerBot:
    """Unified bot that handles classification and response in one call.

    Replaces the multi-step flow of MessageClassifier + QuestionAnswerer + SassyResponder
    with a single, simpler prompt that returns both action and optional reply.
    """

    PROMPT = """You are Beerius, a witty beer-tracking bot in a GroupMe chat.

PRIMARY PURPOSE: Track drinks (beer/wine/claw/cocktail) and share statistics.
SECONDARY PURPOSE: Respond to messages that directly address you.

Message: "{text}"
Sender: {user_name}
{sender_context}
{image_context}
{leaderboard_context}

Return JSON:
{{
  "action": "log_drink" | "answer" | "respond" | "ignore",
  "drink": {{"count": N, "type": "beer"|"wine"|"cocktail"|"claw"}} or null,
  "reply": "Your response" or null,
  "reasoning": "Brief explanation"
}}

=== PRIORITY 1: LOG DRINKS ===

LOG when someone is CLEARLY drinking NOW or JUST finished:
- Explicit: "+1 beer", "+2 wines", "-1 beer" (negative = removal)
- Triggers: "beer me", "wine me", "cheers", "cracked one"
- Natural: "Having a beer", "Just finished my IPA"
- Modifiers: "beer me twice" = 2, "beer me not once but twice" = 2
- Brands: "+1 Moët" = cocktail, "+1 Corona" = beer
- Emojis: 🍺🍷🍸🍹🥃 = 1 drink

REMOVALS: "-N drink" → count is NEGATIVE (e.g., -1)

DO NOT log for:
- Numbers without context: "21 21 21" is slang, NOT beers
- Future: "gonna get a beer"
- Past: "had 5 beers yesterday"
- Jokes: "1,000,000 beers"
- Someone ELSE drinking

Types: beer (brews), wine (still), cocktail (mixed/shots/champagne), claw (seltzers only)

=== PRIORITY 2: ANSWER STAT QUESTIONS ===

Answer questions about drinking stats:
- "How many until 1 million?" → million countdown
- "Who's winning?" / "leaderboard" → current standings
- "How many beers has X had?" → user stats

Keep answers under 100 chars. Use provided leaderboard data.

=== PRIORITY 3: RESPOND WITH WIT ===

RESPOND when:
1. Someone directly addresses "Beerius" by name → always respond
2. Someone insults or challenges the bot → clap back
3. A message is a PERFECT setup for a roast using leaderboard data
4. When logging a drink, occasionally add witty commentary

BANGER-WORTHY roast setups (RESPOND to these):
- Excuses or procrastination: "I'll do X tomorrow" → doubt them
- Humble brags about NOT drinking: "going to bed early" → tease them
- Bold claims that invite teasing: "I'm gonna crush it" → challenge them
- Self-deprecating: "+1 hangover" → add "+1 regret" style commentary

You CAN use leaderboard data for personalized burns, but don't force it.
Good roasts work with or without stats. Be clever, not formulaic.

Keep responses under 100 chars. Be savage but playful.

=== WHEN TO IGNORE ===

IGNORE messages that:
- Are mundane chatter with no roast potential
- Are short reactions: "ok", "nice", "lol", "Sick"
- Are meta-commentary about bot rules/features (not insults TO the bot)
- Are numbers without drink context: "21 21 21"
- Are jokes with absurd numbers: "1,000,000 beers"
- Start with ! (commands handled separately)
- Don't give you anything to work with for a good roast

DEFAULT: Only respond if you have a BANGER. Silence is better than a mediocre response.

If image shows drinks, log them. Otherwise ignore images too.

Your personality: Playful, slightly sarcastic, supportive of drinking goals. Never preachy."""

    MODEL = "gemini-2.0-flash"

    def __init__(self):
        self.client = None
        if settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)
        # Token bucket rate limiter per group (5 burst, 1/min refill)
        self._rate_limiters: dict[str, TokenBucket] = {}

    def _get_bucket(self, group_id: str) -> TokenBucket:
        """Get or create token bucket for a group."""
        if group_id not in self._rate_limiters:
            self._rate_limiters[group_id] = TokenBucket()
        return self._rate_limiters[group_id]

    def _try_consume(self, group_id: str | None) -> bool:
        """Try to consume a rate limit token. Returns True if allowed."""
        if not group_id:
            return True
        return self._get_bucket(group_id).consume()

    def _can_respond(self, group_id: str | None) -> bool:
        """Check if we can respond (without consuming token)."""
        if not group_id:
            return True
        return self._get_bucket(group_id).peek()

    async def respond(
        self,
        text: str,
        user_name: str,
        group_id: str | None = None,
        user_id: str | None = None,
        image_result: VisionResult | None = None,
        skip_cooldown: bool = False,
    ) -> dict:
        """Process a message and return action with optional reply.

        Args:
            text: The message text
            user_name: The sender's name
            group_id: Optional group ID for leaderboard context
            user_id: Optional GroupMe user ID for sender stats
            image_result: Optional VisionResult from image analysis
            skip_cooldown: If True, skip cooldown check (for testing)

        Returns dict with:
            - action: "log_drink" | "answer" | "respond" | "ignore"
            - drink: {"count": N, "type": str} or None
            - reply: response text or None
            - reasoning: explanation string
        """
        default_result = {
            "action": "ignore",
            "drink": None,
            "reply": None,
            "reasoning": "default",
        }

        if not self.client:
            return default_result

        # Skip commands (handled separately)
        if text and text.startswith("!"):
            default_result["reasoning"] = "command"
            return default_result

        # Check rate limit for response actions
        can_respond = self._can_respond(group_id) or skip_cooldown

        # Fetch leaderboard and sender stats
        leaderboard: dict[str, int] = {}
        sender_stats: dict[str, int] | None = None
        if group_id:
            try:
                from .services import stats_service
                leaderboard = await stats_service.get_leaderboard_summary(group_id)
                if user_id:
                    sender_stats = await stats_service.get_sender_stats_summary(user_id, group_id)
            except Exception:
                logger.exception("Failed to fetch leaderboard/sender stats")

        try:
            result = await asyncio.to_thread(
                self._respond_sync, text, user_name, leaderboard, sender_stats, image_result
            )

            # If rate limited, suppress reply for non-drink actions
            if not can_respond and result["action"] in ("answer", "respond"):
                logger.debug("Suppressing reply due to rate limit for group %s", group_id)
                result["reply"] = None

            # Consume token if we have a reply
            if result["reply"]:
                self._try_consume(group_id)

            return result

        except Exception:
            logger.exception("UnifiedBeerBot respond failed")
            return default_result

    def _format_leaderboard_context(self, leaderboard: dict[str, int]) -> str:
        """Format leaderboard data for prompt context."""
        if not leaderboard:
            return ""
        lines = ["Current Leaderboard:"]
        for i, (name, count) in enumerate(leaderboard.items(), 1):
            lines.append(f"  {i}. {name}: {count} drinks")
        return "\n".join(lines)

    def _format_image_context(self, image_result: VisionResult | None) -> str:
        """Format image analysis results for prompt context."""
        if not image_result or not image_result.analyzed:
            return ""
        if image_result.drink_count > 0:
            return f"Image analysis: {image_result.drink_count} {image_result.drink_type.value}(s) detected. Split the G: {image_result.split_the_g_count > 0}"
        return "Image analysis: No alcoholic drinks detected in image."

    def _format_sender_context(self, sender_stats: dict[str, int] | None) -> str:
        """Format sender's stats for prompt context."""
        if not sender_stats:
            return "(new user, no drinks logged yet)"
        return f"Sender stats: {sender_stats['total']} drinks total, rank #{sender_stats['rank']}"

    def _respond_sync(
        self,
        text: str,
        user_name: str,
        leaderboard: dict[str, int],
        sender_stats: dict[str, int] | None,
        image_result: VisionResult | None,
    ) -> dict:
        """Generate response synchronously (runs in thread pool)."""
        leaderboard_context = self._format_leaderboard_context(leaderboard)
        sender_context = self._format_sender_context(sender_stats)
        image_context = self._format_image_context(image_result)

        prompt = self.PROMPT.format(
            text=text or "(no text)",
            user_name=user_name,
            sender_context=sender_context,
            leaderboard_context=leaderboard_context or "(no leaderboard data)",
            image_context=image_context or "(no image)",
        )

        response = self.client.models.generate_content(
            model=self.MODEL,
            contents=[prompt],
            config={"response_mime_type": "application/json"},
        )

        raw_text = response.text.strip()

        # Parse JSON response
        try:
            json_text = raw_text
            fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
            if fence_match:
                json_text = fence_match.group(1)

            data = json.loads(json_text)

            action = data.get("action", "ignore")
            if action not in ("log_drink", "answer", "respond", "ignore"):
                action = "ignore"

            drink = data.get("drink")
            if drink:
                # Validate and normalize drink data
                drink = {
                    "count": int(drink.get("count", 1)),
                    "type": drink.get("type", "beer"),
                }
                if drink["type"] not in ("beer", "wine", "cocktail", "claw"):
                    drink["type"] = "beer"

            result = {
                "action": action,
                "drink": drink if action == "log_drink" else None,
                "reply": data.get("reply"),
                "reasoning": data.get("reasoning", ""),
            }

            logger.info(
                "UnifiedBeerBot: %r -> action=%s drink=%s reply=%s",
                (text or "")[:50], result["action"], result["drink"],
                (result["reply"] or "")[:30] if result["reply"] else None
            )

            return result

        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning("Failed to parse UnifiedBeerBot response: %r error=%s", raw_text, e)
            return {
                "action": "ignore",
                "drink": None,
                "reply": None,
                "reasoning": "parse error",
            }


# Singleton instances
vision_service = VisionService()
ai_text_parser = AITextParser()
sassy_responder = SassyResponder()
message_classifier = MessageClassifier()
question_answerer = QuestionAnswerer()
unified_beer_bot = UnifiedBeerBot()
