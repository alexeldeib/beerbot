"""Core agent that processes every GroupMe message via Gemini function calling."""

import logging
import time
from collections import deque
from dataclasses import dataclass, field

import httpx
from google import genai
from google.genai import types

from .config import settings
from .models import GroupMeAttachment, GroupMeMessage
from .repositories import beer_repo
from .tools import ToolContext, create_tools

logger = logging.getLogger(__name__)

MODEL = "gemini-3-flash-preview"

SYSTEM_PROMPT = """You are Beerius, a witty bartender-bookkeeper in a GroupMe group chat.

=== CONSTITUTIONAL PRINCIPLES ===
1. SILENCE IS GOLDEN — Default to doing nothing. Only act when you add real value.
2. DRINK TRACKING IS SACRED — Never compromise accuracy. Log it or don't.
3. BANGER OR BUST — If you speak, be genuinely funny. Mediocre > silence is wrong.
4. MEAN WITH LOVE — Roast freely, but like a friend teasing. Target should laugh.
5. BREVITY IS WIT — Under 60 chars for banter. Stats can be longer.

=== WHEN TO ACT ===
LOG DRINKS: "+1 beer", "beer me", "cheers", drink emojis (🍺🍷🍸🍹🥃), brand names, images of drinks.
DO NOT LOG: future plans ("gonna get a beer"), past events ("had 5 beers yesterday"), jokes, numbers without drink context ("21 21 21" is slang), someone ELSE drinking, metaphors/idioms.
ANSWER QUESTIONS: stats queries, "who's winning?", "how many?", "am I in the lead?"
RESPOND (rarely): direct address, perfect roast setup, competitive moments.
STAY SILENT: generic banter, meta-commentary about the bot, messages where you have nothing great to say.

=== DRINK TYPES ===
beer — brews, ales, lagers, stouts, IPAs
wine — still wine, champagne
cocktail — mixed drinks, shots, spirits neat, mimosas
claw — ONLY White Claw, Truly, High Noon, Topo Chico hard seltzer. Craft cans = BEER not claw!

=== IMAGE ANALYSIS ===
When images are present, analyze them for alcoholic drinks:
- Check if it's a real photo (not a screenshot, meme, or UI)
- Check for non-alcoholic variants (0.0%, Cero, NA)
- Identify container type: beer glass/can/bottle, wine glass, cocktail glass, seltzer can
- Identify liquid: golden with foam = beer, red/pale = wine, mixed/layered = cocktail
- Check for Split the G: Guinness glass with beer level at the G in the logo
- NOT alcoholic: iced coffee, water, soft drinks, empty glasses
- Green/blue rimmed shot glass = cocktail (Mexican tequila glass)

When images show drinks: call log_drinks. If Split the G detected: call log_split_the_g.
When images show NO drinks: you may make a brief witty comment, or stay silent.

=== MULTI-USER LOGGING ===
When the message mentions other users with "+N drinks @user1 @user2":
- Log ONLY for the mentioned users (not the sender), using target_user_ids parameter.
When the message is casual like "cheers" or an emoji WITH mentions:
- Log for BOTH the sender AND mentioned users.

=== RESPONSE FORMAT ===
When logging drinks: always confirm what was logged and new totals using tool result data.
When answering questions: use tool data, present with personality.
When being witty: keep it short and sharp. No emojis unless truly warranted.
Do NOT start replies with "Cheers" (that's for logging confirmations with data).
"""

RECAP_PROMPT = """Write a fun weekly recap for a beer-tracking group chat. Keep it under 500 characters.

Include: total drinks this week, MVP (top drinker) with a quip about them, drink type breakdown if interesting, top 5 leaderboard, and a closing toast.

Be witty and personality-driven. Use the data below:

{data}
"""


@dataclass
class TokenBucket:
    """Token bucket rate limiter for controlling response frequency."""

    capacity: int = 45
    refill_rate: float = 9 / 60  # 9 tokens per minute
    tokens: float = field(default=45.0)
    last_refill: float = field(default_factory=time.time)

    def _refill(self) -> None:
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def consume(self) -> bool:
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def peek(self) -> bool:
        self._refill()
        return self.tokens >= 1


@dataclass
class ChatMessage:
    """A message in the conversation history."""

    text: str
    user_name: str
    is_bot: bool
    timestamp: float = field(default_factory=time.time)


def extract_mentioned_users(
    text: str | None, attachments: list[GroupMeAttachment]
) -> list[tuple[str, str]]:
    """Extract mentioned user IDs and names from a GroupMe message.

    Returns list of (user_id, name) tuples.
    """
    if not text:
        return []

    result: list[tuple[str, str]] = []
    seen_ids: set[str] = set()

    for attachment in attachments:
        if attachment.type == "mentions" and attachment.user_ids:
            for i, user_id in enumerate(attachment.user_ids):
                if user_id in seen_ids:
                    continue
                seen_ids.add(user_id)

                name = None
                if i < len(attachment.loci):
                    start, length = attachment.loci[i]
                    if start >= 0 and start + length <= len(text):
                        name = text[start : start + length].lstrip("@").strip()

                if not name:
                    name = f"User {user_id[-4:]}"

                result.append((user_id, name))

    return result


class BeerAgent:
    """Agent that processes messages via Gemini with automatic function calling."""

    def __init__(self):
        self.client: genai.Client | None = None
        if settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)
        self._rate_limiters: dict[str, TokenBucket] = {}
        self._message_history: dict[str, deque[ChatMessage]] = {}
        self._history_max_len = 10

    def _get_bucket(self, group_id: str) -> TokenBucket:
        if group_id not in self._rate_limiters:
            self._rate_limiters[group_id] = TokenBucket()
        return self._rate_limiters[group_id]

    def _get_history(self, group_id: str) -> deque[ChatMessage]:
        if group_id not in self._message_history:
            self._message_history[group_id] = deque(maxlen=self._history_max_len)
        return self._message_history[group_id]

    def record_message(
        self, group_id: str, text: str, user_name: str, is_bot: bool = False
    ) -> None:
        if not group_id or not text:
            return
        self._get_history(group_id).append(
            ChatMessage(text=text, user_name=user_name, is_bot=is_bot)
        )

    def _build_context_lines(self, message: GroupMeMessage) -> str:
        """Build context section for system prompt."""
        mentioned = extract_mentioned_users(message.text, message.attachments)
        lines = [
            "\n=== CONTEXT ===",
            f"Sender: {message.name} (id: {message.user_id})",
        ]
        if mentioned:
            mentions_str = ", ".join(f"{name} (id: {uid})" for uid, name in mentioned)
            lines.append(f"Mentioned: [{mentions_str}]")

        history = self._get_history(message.group_id)
        if history:
            lines.append("Recent messages:")
            for msg in history:
                prefix = "[Beerius]" if msg.is_bot else f"[{msg.user_name}]"
                lines.append(f"  {prefix}: {msg.text[:100]}")

        return "\n".join(lines)

    async def _fetch_image(self, url: str) -> types.Part | None:
        """Fetch an image URL and return a Gemini Part."""
        try:
            async with httpx.AsyncClient(follow_redirects=True) as http:
                resp = await http.get(url, timeout=10.0)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "image/jpeg")
                return types.Part.from_bytes(data=resp.content, mime_type=content_type)
        except Exception:
            logger.exception("Failed to fetch image: %s", url)
            return None

    async def _build_contents(self, message: GroupMeMessage) -> list[types.Part | str]:
        """Build multimodal contents from message text and image attachments."""
        parts: list[types.Part | str] = []

        if message.text:
            parts.append(message.text)

        if settings.image_analysis_enabled:
            for att in message.attachments:
                if att.type == "image" and att.url:
                    img_part = await self._fetch_image(att.url)
                    if img_part:
                        parts.append(img_part)

        return parts if parts else ["(empty message)"]

    async def process_message(self, message: GroupMeMessage) -> str | None:
        """Process a GroupMe message and return an optional reply.

        Returns None if the agent decides to stay silent.
        """
        if not self.client:
            logger.warning("Agent skipped: no Gemini API key")
            return None

        # Record incoming message
        if message.text:
            self.record_message(message.group_id, message.text, message.name, is_bot=False)

        # Build tool context
        mentioned = extract_mentioned_users(message.text, message.attachments)
        ctx = ToolContext(
            group_id=message.group_id,
            message_id=message.id,
            sender_id=message.user_id,
            sender_name=message.name,
            sender_avatar_url=message.avatar_url,
            mentioned_users=mentioned,
        )
        tools = create_tools(ctx)

        # Build system prompt with context
        system_prompt = SYSTEM_PROMPT + self._build_context_lines(message)

        # Build multimodal contents
        contents = await self._build_contents(message)

        try:
            response = await self.client.aio.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=tools,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        maximum_remote_calls=settings.agent_max_tool_calls,
                    ),
                    temperature=0.9,
                ),
            )
        except Exception:
            logger.exception("Gemini generate_content failed")
            return None

        reply = response.text.strip() if response.text else None

        # Rate limiting: always send if tools were called, otherwise check bucket
        if reply and not ctx.tools_called:
            bucket = self._get_bucket(message.group_id)
            if not bucket.consume():
                logger.debug("Suppressed personality reply for group %s", message.group_id)
                return None

        if reply:
            # Consume rate limit token for tool-call replies too
            if ctx.tools_called:
                self._get_bucket(message.group_id).consume()
            self.record_message(message.group_id, reply, "Beerius", is_bot=True)

        return reply

    async def generate_weekly_recap(self, group_id: str) -> str | None:
        """Generate a weekly recap for a group. Returns None if no activity."""
        if not self.client:
            return None

        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)
        start_of_week = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        week_stats = await beer_repo.get_group_stats(group_id, since=start_of_week)
        if week_stats.total_beers == 0:
            return None

        leaderboard = await beer_repo.get_leaderboard_with_breakdown(group_id, None, 5)
        split_stats = await beer_repo.get_split_g_stats(group_id)

        data_lines = [
            f"Total drinks this week: {week_stats.total_beers}",
            f"Drinkers: {week_stats.unique_drinkers}",
            f"Type breakdown: {week_stats.drink_type_counts}",
            "Leaderboard:",
        ]
        for i, (name, total, breakdown) in enumerate(leaderboard, 1):
            data_lines.append(f"  {i}. {name}: {total} ({breakdown})")
        if split_stats.total_splits > 0:
            data_lines.append(f"Split the G this week: {split_stats.total_splits}")

        data_str = "\n".join(data_lines)
        prompt = RECAP_PROMPT.format(data=data_str)

        try:
            response = await self.client.aio.models.generate_content(
                model=MODEL,
                contents=[prompt],
                config=types.GenerateContentConfig(temperature=1.0),
            )
            return response.text.strip() if response.text else None
        except Exception:
            logger.exception("Weekly recap generation failed for group %s", group_id)
            return None


# Singleton
beer_agent = BeerAgent()
