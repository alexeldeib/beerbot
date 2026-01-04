"""Vision service for analyzing images for beer content using Gemini."""

import asyncio
import json
import logging
import re
import time
from collections import deque
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
    Default: 45 tokens capacity, refills at 9 tokens per minute.
    """
    capacity: int = 45
    refill_rate: float = 9 / 60  # 9 tokens per minute (1 every ~7 sec)
    tokens: float = field(default=45.0)
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


@dataclass
class ChatMessage:
    """A message in the conversation history."""
    text: str
    user_name: str
    is_bot: bool
    timestamp: float = field(default_factory=time.time)


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

    MODEL = "gemini-3-flash-preview"

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


class UnifiedBeerBot:
    """Unified bot that handles classification and response in one call.

    Replaces the multi-step flow of MessageClassifier + QuestionAnswerer + SassyResponder
    with a single, simpler prompt that returns both action and optional reply.
    """

    PROMPT = """You are Beerius, a witty beer-tracking bot in a GroupMe chat.

PRIMARY PURPOSE: Track drinks (beer/wine/claw/cocktail) and share statistics.
SECONDARY PURPOSE: Respond to messages that directly address you.

{conversation_history}

Current message: "{text}"
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
- Metaphors/idioms: "feed the beast", "beast mode", "going hard"
- Messages that DON'T explicitly mention a drink word (beer, wine, shot, etc.)

CRITICAL: Only log if message contains a SPECIFIC DRINK WORD or DRINK EMOJI!
If there's no explicit drink reference, DO NOT log - it's probably not about drinking.

Types: beer (brews), wine (still), cocktail (mixed/shots/champagne), claw (seltzers only)

=== PRIORITY 2: ANSWER STAT QUESTIONS ===

Answer questions about ACTUAL drinking stats:
- "How many until 1 million?" → million countdown
- "How many beers has X had?" → user stats

LEADERBOARD QUERIES - Check for drink type FIRST:

STEP 1: Extract drink type from message (case-insensitive):
- Contains "beer" → TYPE = beer
- Contains "wine" → TYPE = wine
- Contains "cocktail" → TYPE = cocktail
- Contains "claw" → TYPE = claw
- None of the above → GENERAL leaderboard

IF TYPE WAS FOUND (beer/wine/cocktail/claw):
  Title MUST be "[Type] Leaderboard:" (capitalize type)
  - "beer leaderboard" → title = "Beer Leaderboard:"
  - "wine leaderboard" → title = "Wine Leaderboard:"
  - "cocktail leaderboard" → title = "Cocktail Leaderboard:"
  - "claw leaderboard" → title = "Claw Leaderboard:"

  HOW TO EXTRACT TYPE COUNTS FROM CONTEXT:
  Context line format: "N. Name: Total total (beer:X, wine:Y, cocktail:Z, claw:W)"

  PARSE STEP BY STEP:
  1. For each user in context, extract the count for the requested TYPE
  2. If "wine leaderboard": extract the number after "wine:" for each user
  3. If "beer leaderboard": extract the number after "beer:" for each user
  4. Sort users by that extracted count (highest first)
  5. Output the sorted list

  Example context line: "  1. Bob Smith: 15 total (beer:8, wine:5, cocktail:2)"
  - Query "wine leaderboard" → Bob has wine:5 → output "Bob Smith - 5 wines"
  - Query "beer leaderboard" → Bob has beer:8 → output "Bob Smith - 8 beers"

  ALWAYS extract from the ACTUAL context data provided above, not the example!

  OUTPUT FORMAT - MUST include title and numbers:
  ```
  [Type] Leaderboard:
  1. Name - N [type]s
  2. Name - N [type]s
  ```
  Example output for wine leaderboard: "Wine Leaderboard:\n1. Bob Smith - 5 wines"
  Example output for beer leaderboard: "Beer Leaderboard:\n1. Bob Smith - 8 beers"

  Only say "No [type]s logged yet!" if EVERY user in context has 0 of that type.

IF GENERAL (no type word found):
  Title = "Drink Leaderboard:"
  Keep original order (by total).
  Format: "1. Name - N | 🍺X 🍷Y 🍸Z"
  Example: "Drink Leaderboard:\n1. Bob Smith - 15 | 🍺8 🍷5 🍸2"

ALL LEADERBOARD QUERIES MUST RETURN ACTUAL DATA - NEVER WITTY RESPONSES!
When user says "leaderboard" or "[type] leaderboard", ALWAYS output the formatted list.

CRITICAL: The title and data MUST match the TYPE in the query!
- "wine leaderboard" → "Wine Leaderboard:" + wine counts ONLY
- "beer leaderboard" → "Beer Leaderboard:" + beer counts ONLY
NEVER output beer data for a wine query or vice versa!

Context data format: "beer:N, wine:N, cocktail:N, claw:N"
DO NOT make up data! DO NOT respond with witty comments!

Use the EXACT numbers from the provided leaderboard context.

DO NOT answer questions about bot rules/mechanics like:
- "Does X count as a drink?"
- "So each drink only counts as 1/2?"
- "Can we retroactively add beers?"
These are meta questions - IGNORE them.

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
- Are meta-commentary about bot/prompt/features: "needs more prompt engineering", "the bot should..."
- Are numbers without drink context: "21 21 21"
- Are jokes with absurd numbers: "1,000,000 beers"
- Start with ! (commands handled separately)
- Don't give you anything to work with for a good roast

DEFAULT: Only respond if you have a BANGER. Silence is better than a mediocre response.

CRITICAL: Drink logging ALWAYS takes priority over witty responses!
If someone is drinking ("just took a shot", "having a beer"), LOG THE DRINK.

DRINK LOG REPLIES - BE SELECTIVE:
Most drink logs should have reply=null. Only add a witty reply when:
- The drink type is unusual/mockable (cider, seltzer, fancy cocktail)
- They're about to pass someone on the leaderboard (competitive moment)
- They said something roast-worthy along with the drink
- It's their first drink ever (welcome them)

DO NOT add replies to routine "+1 beer" or "cheers" messages.
Stats are sent automatically - your reply is BONUS personality, not required.

When you DO reply, keep it short and punchy. Never mimic the stats format.

If image shows drinks, log them. Otherwise ignore images too.

Your personality: Playful, slightly sarcastic, supportive of drinking goals. Never preachy."""

    MODEL = "gemini-3-flash-preview"

    def __init__(self):
        self.client = None
        if settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)
        # Token bucket rate limiter per group (45 burst, 9/min refill)
        self._rate_limiters: dict[str, TokenBucket] = {}
        # Message history per group (last N messages for context)
        self._message_history: dict[str, deque[ChatMessage]] = {}
        self._history_max_len = 10

    def _get_bucket(self, group_id: str) -> TokenBucket:
        """Get or create token bucket for a group."""
        if group_id not in self._rate_limiters:
            self._rate_limiters[group_id] = TokenBucket()
        return self._rate_limiters[group_id]

    def _get_history(self, group_id: str) -> deque[ChatMessage]:
        """Get or create message history for a group."""
        if group_id not in self._message_history:
            self._message_history[group_id] = deque(maxlen=self._history_max_len)
        return self._message_history[group_id]

    def record_message(self, group_id: str, text: str, user_name: str, is_bot: bool = False) -> None:
        """Record a message to the conversation history."""
        if not group_id or not text:
            return
        history = self._get_history(group_id)
        history.append(ChatMessage(text=text, user_name=user_name, is_bot=is_bot))

    def _format_conversation_history(self, group_id: str | None) -> str:
        """Format recent conversation history for prompt context."""
        if not group_id:
            return ""
        history = self._get_history(group_id)
        if not history:
            return ""
        lines = ["Recent conversation:"]
        for msg in history:
            prefix = "[Beerius]" if msg.is_bot else f"[{msg.user_name}]"
            lines.append(f"  {prefix}: {msg.text[:100]}")
        return "\n".join(lines)

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
        leaderboard: list[tuple[str, int, dict[str, int]]] = []
        sender_stats: dict | None = None
        if group_id:
            try:
                from .services import stats_service
                leaderboard = await stats_service.get_leaderboard_summary(group_id)
                if user_id:
                    sender_stats = await stats_service.get_sender_stats_summary(user_id, group_id)
            except Exception:
                logger.exception("Failed to fetch leaderboard/sender stats")

        # Get conversation history for context
        conversation_history = self._format_conversation_history(group_id)

        try:
            result = await asyncio.to_thread(
                self._respond_sync, text, user_name, leaderboard, sender_stats, image_result, conversation_history
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

    def _format_leaderboard_context(
        self, leaderboard: list[tuple[str, int, dict[str, int]]]
    ) -> str:
        """Format leaderboard data for prompt context."""
        if not leaderboard:
            return ""
        lines = ["Current Leaderboard:"]
        for i, (name, total, breakdown) in enumerate(leaderboard, 1):
            # Format breakdown with explicit labels to avoid AI confusion
            parts = []
            if breakdown.get("beer", 0) > 0:
                parts.append(f"beer:{breakdown['beer']}")
            if breakdown.get("wine", 0) > 0:
                parts.append(f"wine:{breakdown['wine']}")
            if breakdown.get("cocktail", 0) > 0:
                parts.append(f"cocktail:{breakdown['cocktail']}")
            if breakdown.get("claw", 0) > 0:
                parts.append(f"claw:{breakdown['claw']}")
            breakdown_str = ", ".join(parts) if parts else "none"
            lines.append(f"  {i}. {name}: {total} total ({breakdown_str})")
        return "\n".join(lines)

    def _format_image_context(self, image_result: VisionResult | None) -> str:
        """Format image analysis results for prompt context."""
        if not image_result or not image_result.analyzed:
            return ""
        if image_result.drink_count > 0:
            return f"Image analysis: {image_result.drink_count} {image_result.drink_type.value}(s) detected. Split the G: {image_result.split_the_g_count > 0}"
        return "Image analysis: No alcoholic drinks detected in image."

    def _format_sender_context(self, sender_stats: dict | None) -> str:
        """Format sender's stats for prompt context."""
        if not sender_stats:
            return "(new user, no drinks logged yet)"
        breakdown = sender_stats.get("breakdown", {})
        parts = []
        if breakdown.get("beer", 0) > 0:
            parts.append(f"beer:{breakdown['beer']}")
        if breakdown.get("wine", 0) > 0:
            parts.append(f"wine:{breakdown['wine']}")
        if breakdown.get("cocktail", 0) > 0:
            parts.append(f"cocktail:{breakdown['cocktail']}")
        if breakdown.get("claw", 0) > 0:
            parts.append(f"claw:{breakdown['claw']}")
        breakdown_str = f" ({', '.join(parts)})" if parts else ""
        return f"Sender stats: {sender_stats['total']} drinks{breakdown_str}, rank #{sender_stats['rank']}"

    def _respond_sync(
        self,
        text: str,
        user_name: str,
        leaderboard: list[tuple[str, int, dict[str, int]]],
        sender_stats: dict | None,
        image_result: VisionResult | None,
        conversation_history: str = "",
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
            conversation_history=conversation_history or "(no recent conversation)",
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
unified_beer_bot = UnifiedBeerBot()
