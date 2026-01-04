"""Business logic for message parsing and statistics."""

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import DrinkType, GroupMeAttachment, GroupMeMessage, GroupStats, UserStats
from .repositories import beer_repo, debt_repo, user_repo

logger = logging.getLogger(__name__)

# Use Eastern time for all date calculations
EASTERN = ZoneInfo("America/New_York")


def extract_mentioned_user_ids(attachments: list[GroupMeAttachment]) -> list[str]:
    """Extract unique mentioned user IDs from attachments."""
    user_ids: set[str] = set()
    for attachment in attachments:
        if attachment.type == "mentions":
            user_ids.update(attachment.user_ids)
    return list(user_ids)


def extract_mentioned_users(
    text: str | None, attachments: list[GroupMeAttachment]
) -> list[tuple[str, str]]:
    """Extract mentioned user IDs and their names from message.

    Returns list of (user_id, name) tuples.
    Names are extracted from the message text using loci positions.
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

                # Try to extract name from loci
                name = None
                if i < len(attachment.loci):
                    start, length = attachment.loci[i]
                    if start >= 0 and start + length <= len(text):
                        # Extract name, remove leading @ if present
                        name = text[start : start + length].lstrip("@").strip()

                if not name:
                    name = f"User {user_id[-4:]}"

                result.append((user_id, name))

    return result


class MessageParser:
    """Parse messages for drink triggers and commands."""

    # Drink emojis
    BEER_EMOJI = "\U0001F37A"  # 🍺
    WINE_EMOJI = "\U0001F377"  # 🍷
    COCKTAIL_EMOJI = "\U0001F378"  # 🍸
    TROPICAL_EMOJI = "\U0001F379"  # 🍹
    TUMBLER_EMOJI = "\U0001F943"  # 🥃

    # Patterns for logging beers - all require start of message to avoid mid-text matches
    NUMERIC_PATTERN = re.compile(r"^\+(\d+)\s*beers?", re.IGNORECASE)
    BEER_ME_PATTERN = re.compile(r"^beer me\b", re.IGNORECASE)
    CHEERS_PATTERN = re.compile(r"^cheers\b", re.IGNORECASE)
    CRACKED_PATTERN = re.compile(r"^cracked (one|a beer|a cold one)\b", re.IGNORECASE)

    # Patterns for other drink types
    WINE_NUMERIC_PATTERN = re.compile(r"^\+(\d+)\s*wines?", re.IGNORECASE)
    WINE_ME_PATTERN = re.compile(r"^wine me\b", re.IGNORECASE)
    COCKTAIL_NUMERIC_PATTERN = re.compile(r"^\+(\d+)\s*cocktails?", re.IGNORECASE)
    COCKTAIL_ME_PATTERN = re.compile(r"^(cocktail me|mix me)\b", re.IGNORECASE)
    CLAW_NUMERIC_PATTERN = re.compile(r"^\+(\d+)\s*(claws?|seltzers?)", re.IGNORECASE)
    CLAW_ME_PATTERN = re.compile(r"^(claw me|seltzer me)\b", re.IGNORECASE)

    # Generic pattern for +N <word> (catches unknown drink types)
    GENERIC_DRINK_PATTERN = re.compile(r"^\+(\d+)\s*(\w+)", re.IGNORECASE)

    # Negative patterns for removing drinks
    NEGATIVE_BEER_PATTERN = re.compile(r"^-(\d+)\s*beers?", re.IGNORECASE)
    NEGATIVE_WINE_PATTERN = re.compile(r"^-(\d+)\s*wines?", re.IGNORECASE)
    NEGATIVE_COCKTAIL_PATTERN = re.compile(r"^-(\d+)\s*cocktails?", re.IGNORECASE)
    NEGATIVE_CLAW_PATTERN = re.compile(r"^-(\d+)\s*(claws?|seltzers?)", re.IGNORECASE)
    NEGATIVE_GENERIC_PATTERN = re.compile(r"^-(\d+)\s*(\w+)", re.IGNORECASE)

    # Alcoholic drink words that should count as cocktails when not matching specific types
    ALCOHOLIC_DRINK_WORDS = {
        # Cocktails/mixed drinks
        "mimosa", "mimosas", "margarita", "margaritas", "martini", "martinis",
        "mojito", "mojitos", "daiquiri", "daiquiris", "cosmopolitan", "cosmopolitans",
        "manhattan", "manhattans", "negroni", "negronis", "maitai", "maitais",
        "pinacolada", "pinacoladas", "bloodymary", "bloodymaries", "oldfashioned",
        "whiskeysour", "whiskeysours", "ginandtonic", "sangria", "sangrias",
        "bellini", "bellinis", "spritz", "spritzes", "aperol", "aperols",
        "highball", "highballs", "shot", "shots", "shooter", "shooters",
        "paloma", "palomas", "caipirinha", "caipirinhas", "mule", "mules",
        # Spirits (drinking neat)
        "whiskey", "whiskeys", "bourbon", "bourbons", "scotch", "scotches",
        "vodka", "vodkas", "rum", "rums", "tequila", "tequilas",
        "gin", "gins", "brandy", "brandies", "cognac", "cognacs",
        "mezcal", "mezcals", "sake", "sakes",
        # Generic terms
        "drink", "drinks", "beverage", "beverages", "booze",
        "round", "rounds", "pour", "pours", "dram", "drams",
    }

    # Max message length for emoji-only beer counting (prevents counting in long docs)
    MAX_EMOJI_MESSAGE_LENGTH = 100

    # Command patterns
    COMMANDS = {
        "stats": re.compile(r"^(!beers?|beer count|how many beers)\b", re.IGNORECASE),
        "mystats": re.compile(r"^(!mystats?|my beers|how many have i had)\b", re.IGNORECASE),
        "leaderboard": re.compile(r"^(!leaderboard|!top|beer leaders)\b", re.IGNORECASE),
        "today": re.compile(r"^(!today|beers today)\b", re.IGNORECASE),
        "week": re.compile(r"^(!week|beers this week)\b", re.IGNORECASE),
        "undo": re.compile(r"^(!undo|-1 beer)\b", re.IGNORECASE),
        "unbeer": re.compile(r"^(!unbeer(\s+\d+)?|minus\s+\d+\s*beers?|-\d+\s*beers?)\b", re.IGNORECASE),
        "million": re.compile(r"^(!million|!countdown|!goal|time to million)\b", re.IGNORECASE),
        "splitg": re.compile(r"^(!splitg|split the g)\b", re.IGNORECASE),
        "split": re.compile(r"^!split(\s|$)", re.IGNORECASE),
        "unsplit": re.compile(r"^(!unsplit(\s+\d+)?)\b", re.IGNORECASE),
        "owe": re.compile(r"^(!owe(\s+\d+)?\s*@)", re.IGNORECASE),
        "forgive": re.compile(r"^!forgive\b", re.IGNORECASE),
        "debts": re.compile(r"^(!debts?|debt leaderboard|who owes)\b", re.IGNORECASE),
        "help": re.compile(r"^(!help|beerbot help)\b", re.IGNORECASE),
        "toast": re.compile(r"^(!toast|make a toast|give us a toast)\b", re.IGNORECASE),
    }

    # Pattern to extract unbeer count
    UNBEER_COUNT_PATTERN = re.compile(r"(\d+)", re.IGNORECASE)

    # Pattern to extract mentioned user ID from GroupMe mentions
    MENTION_PATTERN = re.compile(r"@(\S+)")

    def parse_beer_count(self, text: str | None) -> int:
        """Count beers mentioned in a message.

        To avoid false positives in long documentation/help messages,
        triggers are strict:
        - Emojis only count in short messages (< 100 chars)
        - +N beers must be at start of message
        - Word triggers (beer me, cheers, cracked) must be at start
        """
        if not text:
            return 0

        count = 0

        # Emoji counting only for short messages (avoids docs with emoji examples)
        if len(text) <= self.MAX_EMOJI_MESSAGE_LENGTH:
            count += text.count(self.BEER_EMOJI)
            count += text.lower().count(":beer:")

        # +N beers at start of message
        match = self.NUMERIC_PATTERN.search(text)
        if match:
            count += int(match.group(1))

        # Word-based triggers: only count 1 beer max (mutually exclusive)
        # These are casual phrases that shouldn't stack
        if count == 0:
            if (self.BEER_ME_PATTERN.search(text) or
                self.CHEERS_PATTERN.search(text) or
                self.CRACKED_PATTERN.search(text)):
                count = 1

        return count

    def parse_drink(self, text: str | None) -> tuple[int, DrinkType]:
        """Parse drink count and type from message.

        Returns (count, drink_type) tuple. Checks non-beer types first,
        falls back to beer parsing.
        """
        if not text:
            return 0, DrinkType.BEER

        # Check for wine first
        if len(text) <= self.MAX_EMOJI_MESSAGE_LENGTH:
            wine_count = text.count(self.WINE_EMOJI)
            if wine_count > 0:
                return wine_count, DrinkType.WINE

        match = self.WINE_NUMERIC_PATTERN.search(text)
        if match:
            return int(match.group(1)), DrinkType.WINE

        if self.WINE_ME_PATTERN.search(text):
            return 1, DrinkType.WINE

        # Check for cocktails
        if len(text) <= self.MAX_EMOJI_MESSAGE_LENGTH:
            cocktail_count = (
                text.count(self.COCKTAIL_EMOJI)
                + text.count(self.TROPICAL_EMOJI)
                + text.count(self.TUMBLER_EMOJI)
            )
            if cocktail_count > 0:
                return cocktail_count, DrinkType.COCKTAIL

        match = self.COCKTAIL_NUMERIC_PATTERN.search(text)
        if match:
            return int(match.group(1)), DrinkType.COCKTAIL

        if self.COCKTAIL_ME_PATTERN.search(text):
            return 1, DrinkType.COCKTAIL

        # Check for claws/seltzers
        match = self.CLAW_NUMERIC_PATTERN.search(text)
        if match:
            return int(match.group(1)), DrinkType.CLAW

        if self.CLAW_ME_PATTERN.search(text):
            return 1, DrinkType.CLAW

        # Check for generic alcoholic drinks (+N <word> where word is alcoholic)
        match = self.GENERIC_DRINK_PATTERN.search(text)
        if match:
            count = int(match.group(1))
            word = match.group(2).lower()
            # Skip known types that were already checked above
            known_types = {"beer", "beers", "wine", "wines", "cocktail", "cocktails",
                           "claw", "claws", "seltzer", "seltzers"}
            if word not in known_types and word in self.ALCOHOLIC_DRINK_WORDS:
                return count, DrinkType.COCKTAIL

        # Default: existing beer parsing
        beer_count = self.parse_beer_count(text)
        return beer_count, DrinkType.BEER

    def parse_drink_removal(self, text: str | None) -> tuple[int, DrinkType] | None:
        """Parse drink removal from message (-N drinks syntax).

        Returns (count, drink_type) tuple if removal detected, None otherwise.
        """
        if not text:
            return None

        # Check for specific drink types first
        match = self.NEGATIVE_WINE_PATTERN.search(text)
        if match:
            return int(match.group(1)), DrinkType.WINE

        match = self.NEGATIVE_COCKTAIL_PATTERN.search(text)
        if match:
            return int(match.group(1)), DrinkType.COCKTAIL

        match = self.NEGATIVE_CLAW_PATTERN.search(text)
        if match:
            return int(match.group(1)), DrinkType.CLAW

        match = self.NEGATIVE_BEER_PATTERN.search(text)
        if match:
            return int(match.group(1)), DrinkType.BEER

        # Check for generic alcoholic drinks
        match = self.NEGATIVE_GENERIC_PATTERN.search(text)
        if match:
            count = int(match.group(1))
            word = match.group(2).lower()
            # Skip known types already checked
            known_types = {"beer", "beers", "wine", "wines", "cocktail", "cocktails",
                           "claw", "claws", "seltzer", "seltzers"}
            if word not in known_types and word in self.ALCOHOLIC_DRINK_WORDS:
                return count, DrinkType.COCKTAIL

        return None

    def parse_command(self, text: str | None) -> str | None:
        """Check if message is a command. Returns command name or None."""
        if not text:
            return None

        for cmd_name, pattern in self.COMMANDS.items():
            if pattern.search(text):
                return cmd_name

        return None

    def parse_unbeer_count(self, text: str | None) -> int:
        """Extract the number of beers to remove from an unbeer command.

        Defaults to 1 if no number specified.
        """
        if not text:
            return 1
        match = self.UNBEER_COUNT_PATTERN.search(text)
        if match:
            return int(match.group(1))
        return 1  # Default to 1 beer

    def parse_unsplit_count(self, text: str | None) -> int:
        """Extract the number of splits to remove from an unsplit command.

        Defaults to 1 if no number specified.
        """
        if not text:
            return 1
        match = self.UNBEER_COUNT_PATTERN.search(text)
        if match:
            return int(match.group(1))
        return 1  # Default to 1 split

    def is_explicit_assignment(self, text: str | None) -> bool:
        """Check if the message is an explicit drink assignment (+N drinks pattern).

        When used with mentions, explicit assignments give drinks TO others,
        not to the sender.
        """
        if not text:
            return False
        # Check all +N drink patterns (beer, wine, cocktail, claw, generic)
        return bool(
            self.NUMERIC_PATTERN.search(text) or  # +N beers
            self.WINE_NUMERIC_PATTERN.search(text) or  # +N wines
            self.COCKTAIL_NUMERIC_PATTERN.search(text) or  # +N cocktails
            self.CLAW_NUMERIC_PATTERN.search(text) or  # +N claws
            self.GENERIC_DRINK_PATTERN.search(text)  # +N <any drink word>
        )

    def parse_debt_amount(self, text: str | None) -> int:
        """Extract the number of beers from a debt command (!owe N, !payoff N).

        Defaults to 1 if no number specified.
        """
        if not text:
            return 1
        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else 1

    def parse_million_filter(self, text: str | None) -> DrinkType | None:
        """Parse drink type filter from !million command.

        Returns None for 'all' or no filter (counts all drinks).
        Returns specific DrinkType for filtered view.
        """
        if not text:
            return None
        match = re.search(r"!million\s+(beer|wine|cocktail|claw|all)", text, re.IGNORECASE)
        if match:
            filter_type = match.group(1).lower()
            if filter_type == "all":
                return None
            return DrinkType.from_string(filter_type)
        return None  # Default: count all drinks

    def parse_stats_filter(self, text: str | None, command: str) -> DrinkType | None:
        """Parse drink type filter from stats commands (!today, !week, !beers).

        Returns None for 'all' or no filter (counts all drinks).
        Returns specific DrinkType for filtered view.
        """
        if not text:
            return None
        pattern = rf"!{command}\s+(beer|wine|cocktail|claw|all)"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            filter_type = match.group(1).lower()
            if filter_type == "all":
                return None
            return DrinkType.from_string(filter_type)
        return None  # Default: count all drinks


class StatsService:
    """Service for generating statistics responses."""

    def __init__(self):
        self.parser = MessageParser()

    async def log_beers(
        self,
        message: GroupMeMessage,
        quantity: int,
        split_the_g: int = 0,
        drink_type: DrinkType = DrinkType.BEER,
    ) -> str | None:
        """Log drinks for a user and return confirmation message.

        Returns None if this was a duplicate message (idempotency).
        """
        logger.info(
            "log_beers called: user=%s qty=%d split_g=%d drink_type=%s msg_id=%s",
            message.name, quantity, split_the_g, drink_type.value, message.id
        )

        # Get or create user
        user = await user_repo.get_or_create(
            groupme_user_id=message.user_id,
            name=message.name,
            avatar_url=message.avatar_url,
        )

        # Log the drinks (returns None if duplicate)
        beer = await beer_repo.create(
            user_id=user.id,
            group_id=message.group_id,
            quantity=quantity,
            message_id=message.id,
            split_the_g=split_the_g,
            drink_type=drink_type,
        )

        logger.info(
            "beer_repo.create returned: beer=%s (id=%s)",
            beer is not None, beer.id if beer else None
        )

        if beer is None:
            # Duplicate message - already processed
            logger.info("Duplicate message, returning None")
            return None

        # Get new total for this drink type
        total = await beer_repo.get_user_total_by_type(user.id, message.group_id, drink_type)

        # Auto-reduce debt when drinking
        debt_reduced = await debt_repo.reduce_debt(user.id, message.group_id, quantity)
        debt_msg = ""
        if debt_reduced > 0:
            remaining_debt = await debt_repo.get_debt(user.id, message.group_id)
            if remaining_debt > 0:
                debt_msg = f" (-{debt_reduced} debt, {remaining_debt} left)"
            else:
                debt_msg = f" (-{debt_reduced} debt, all paid!)"

        # Build response with split-the-G celebration if applicable
        split_msg = ""
        if split_the_g > 0:
            split_total = await beer_repo.get_user_split_g_total(user.id, message.group_id)
            split_msg = f" 🍀 You split the G! ({split_total} total)"

        # Drink-type-specific wording
        drink_names = {
            DrinkType.BEER: ("beer", "beers", "Cheers"),
            DrinkType.WINE: ("wine", "wines", "Salut"),
            DrinkType.COCKTAIL: ("cocktail", "cocktails", "Cheers"),
            DrinkType.CLAW: ("claw", "claws", "Cheers"),
        }
        singular, plural, greeting = drink_names.get(drink_type, ("drink", "drinks", "Cheers"))

        if quantity == 1:
            return f"{greeting}, {message.name}!{split_msg}{debt_msg} You've now had {total} {singular if total == 1 else plural} total."
        else:
            return f"{greeting}, {message.name}!{split_msg}{debt_msg} +{quantity} {plural} logged. You've now had {total} total."

    async def log_beers_for_users(
        self,
        message: GroupMeMessage,
        quantity: int,
        mentioned_users: list[tuple[str, str]],
        include_sender: bool = True,
        split_the_g: int = 0,
        drink_type: DrinkType = DrinkType.BEER,
    ) -> str | None:
        """Log drinks for users.

        Args:
            message: The GroupMe message
            quantity: Number of drinks to log
            mentioned_users: List of (user_id, name) tuples for mentioned users
            include_sender: If True, include the sender; if False, only log for mentions
            split_the_g: Number of split-the-G achievements detected
            drink_type: Type of drink to log

        Returns a formatted response message, or None if all were duplicates (idempotency).
        Drink logging for all users is atomic (all succeed or none).
        """
        # Build list of all users to log for
        users_to_log: list[tuple[str, str]] = []  # (groupme_user_id, name)

        if include_sender:
            users_to_log.append((message.user_id, message.name))

        # Add mentioned users (deduped, skip sender if already added)
        seen_ids = {message.user_id} if include_sender else set()
        for user_id, name in mentioned_users:
            if user_id not in seen_ids:
                users_to_log.append((user_id, name))
                seen_ids.add(user_id)

        # If no users to log for, return early
        if not users_to_log:
            return "No users to log beers for."

        # If only the sender and include_sender is True, use simpler method
        if len(users_to_log) == 1 and include_sender:
            return await self.log_beers(message, quantity, split_the_g, drink_type)

        # First pass: get or create all users (each is atomic via ON CONFLICT)
        users: list[tuple[int, str]] = []  # (internal_user_id, name)
        for groupme_user_id, display_name in users_to_log:
            if groupme_user_id == message.user_id:
                user = await user_repo.get_or_create(
                    groupme_user_id=message.user_id,
                    name=message.name,
                    avatar_url=message.avatar_url,
                )
            else:
                user = await user_repo.get_or_create(
                    groupme_user_id=groupme_user_id,
                    name=display_name,
                    avatar_url=None,
                )
            users.append((user.id, user.name))

        # Second pass: log all drinks in a single transaction (atomic)
        # Split-the-G only applies to sender (the one who took the photo)
        entries = [
            (user_id, message.group_id, quantity, message.id, split_the_g if i == 0 and include_sender else 0, drink_type)
            for i, (user_id, _) in enumerate(users)
        ]
        results = await beer_repo.create_batch(entries)

        # Collect names of users that were actually logged (not duplicates)
        # Also reduce debt for each logged user
        logged_names = []
        debt_reductions = []
        for (user_id, name), beer in zip(users, results):
            if beer is not None:
                logged_names.append(name)
                # Auto-reduce debt when drinking
                reduced = await debt_repo.reduce_debt(user_id, message.group_id, quantity)
                if reduced > 0:
                    debt_reductions.append((name, reduced))

        # If all were duplicates, don't send a message
        if not logged_names:
            return None

        # Format response with drink-type-specific wording
        drink_names = {
            DrinkType.BEER: ("beer", "beers", "Cheers"),
            DrinkType.WINE: ("wine", "wines", "Salut"),
            DrinkType.COCKTAIL: ("cocktail", "cocktails", "Cheers"),
            DrinkType.CLAW: ("claw", "claws", "Cheers"),
        }
        singular, plural, greeting = drink_names.get(drink_type, ("drink", "drinks", "Cheers"))
        drink_word = singular if quantity == 1 else plural

        split_msg = ""
        if split_the_g > 0:
            split_msg = " 🍀 Split the G!"

        # Add debt reduction info
        debt_msg = ""
        if debt_reductions:
            debt_parts = [f"{name} -{reduced}" for name, reduced in debt_reductions]
            debt_msg = f" (debt: {', '.join(debt_parts)})"

        if len(logged_names) == 1:
            return f"{greeting}, {logged_names[0]}!{split_msg}{debt_msg} +{quantity} {drink_word} logged."
        elif len(logged_names) == 2:
            names_str = f"{logged_names[0]} and {logged_names[1]}"
        else:
            names_str = ", ".join(logged_names[:-1]) + f", and {logged_names[-1]}"

        return f"{greeting}!{split_msg}{debt_msg} +{quantity} {drink_word} logged for {names_str}."

    async def get_group_stats(self, group_id: str) -> str:
        """Get formatted group statistics."""
        stats = await beer_repo.get_group_stats(group_id)
        return self._format_group_stats(stats)

    async def get_today_stats(
        self, group_id: str, drink_type: DrinkType | None = None
    ) -> str:
        """Get formatted stats for today (Eastern time), optionally filtered by drink type."""
        now = datetime.now(EASTERN)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        stats = await beer_repo.get_group_stats(group_id, since=start_of_day, drink_type=drink_type)
        return self._format_group_stats(stats, drink_type)

    async def get_week_stats(
        self, group_id: str, drink_type: DrinkType | None = None
    ) -> str:
        """Get formatted stats for this week (Eastern time), optionally filtered by drink type."""
        now = datetime.now(EASTERN)
        start_of_week = now - timedelta(days=now.weekday())
        start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
        stats = await beer_repo.get_group_stats(group_id, since=start_of_week, drink_type=drink_type)
        return self._format_group_stats(stats, drink_type)

    async def get_user_stats(self, message: GroupMeMessage) -> str:
        """Get formatted stats for the requesting user."""
        user = await user_repo.get_or_create(
            groupme_user_id=message.user_id,
            name=message.name,
            avatar_url=message.avatar_url,
        )

        stats = await beer_repo.get_user_stats_in_group(user.id, message.group_id)

        if not stats or stats.total_beers == 0:
            return f"{message.name}, you haven't logged any drinks yet! Send 🍺 or 🍷 to get started."

        # Get drink type breakdown
        by_type = await beer_repo.get_user_stats_by_type(user.id, message.group_id)

        # Get today's count (Eastern time)
        now = datetime.now(EASTERN)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_stats = await beer_repo.get_group_stats(message.group_id, since=start_of_day)
        today_count = next(
            (u.total_beers for u in today_stats.user_stats if u.name == message.name),
            0,
        )

        # Get this week's count (Eastern time)
        start_of_week = now - timedelta(days=now.weekday())
        start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
        week_stats = await beer_repo.get_group_stats(message.group_id, since=start_of_week)
        week_count = next(
            (u.total_beers for u in week_stats.user_stats if u.name == message.name),
            0,
        )

        # Build drink type breakdown (always show)
        type_parts = []
        for dt in [DrinkType.BEER, DrinkType.WINE, DrinkType.COCKTAIL, DrinkType.CLAW]:
            count = by_type.get(dt, 0)
            emoji = {"beer": "🍺", "wine": "🍷", "cocktail": "🍸", "claw": "🥤"}[dt.value]
            type_parts.append(f"{emoji}{count}")

        lines = [
            f"Your Stats, {message.name}:",
            f"Total: {stats.total_beers} | " + " ".join(type_parts),
        ]

        lines.append(f"Today: {today_count}")
        lines.append(f"This week: {week_count}")

        if stats.last_beer_at:
            diff = now - stats.last_beer_at.astimezone(EASTERN)
            if diff < timedelta(hours=1):
                time_ago = f"{int(diff.total_seconds() // 60)} minutes ago"
            elif diff < timedelta(days=1):
                time_ago = f"{int(diff.total_seconds() // 3600)} hours ago"
            else:
                time_ago = f"{diff.days} days ago"
            lines.append(f"Last drink: {time_ago}")

        return "\n".join(lines)

    async def get_leaderboard(
        self, group_id: str, drink_type: DrinkType | None = None, limit: int = 5
    ) -> str:
        """Get formatted leaderboard, optionally filtered by drink type."""
        leaderboard = await beer_repo.get_leaderboard_with_breakdown(
            group_id, drink_type, limit
        )

        if not leaderboard:
            if drink_type:
                return f"No {drink_type.value}s logged yet! Be the first to log one."
            return "No drinks logged yet! Be the first to log one."

        # Determine title and format based on filter
        if drink_type:
            type_name = drink_type.value.title()
            lines = [f"{type_name} Leaderboard:"]
            plural = f"{drink_type.value}s"
        else:
            lines = ["Drink Leaderboard:"]

        medals = ["1.", "2.", "3."]

        for i, (name, total, breakdown) in enumerate(leaderboard[:limit]):
            prefix = medals[i] if i < len(medals) else f"{i + 1}."

            if drink_type:
                # Simple format when filtered
                lines.append(f"{prefix} {name} - {total} {plural}")
            else:
                # Show breakdown when unfiltered
                breakdown_parts = []
                for dt, emoji in [("beer", "🍺"), ("wine", "🍷"), ("cocktail", "🍸"), ("claw", "🥤")]:
                    count = breakdown.get(dt, 0)
                    if count > 0:
                        breakdown_parts.append(f"{emoji}{count}")

                breakdown_str = " ".join(breakdown_parts) if breakdown_parts else ""
                if breakdown_str:
                    lines.append(f"{prefix} {name} - {total} | {breakdown_str}")
                else:
                    lines.append(f"{prefix} {name} - {total}")

        return "\n".join(lines)

    async def get_leaderboard_summary(
        self, group_id: str, limit: int = 10
    ) -> list[tuple[str, int, dict[str, int]]]:
        """Get leaderboard with drink type breakdown for AI context.

        Returns list of (name, total, {type: count}) tuples.
        """
        return await beer_repo.get_leaderboard_with_breakdown(group_id, None, limit)

    async def get_sender_stats_summary(
        self, groupme_user_id: str, group_id: str
    ) -> dict | None:
        """Get sender's stats for AI context.

        Returns {"total": N, "rank": M, "breakdown": {type: count}} or None if user not found.
        """
        user = await user_repo.get_by_groupme_id(groupme_user_id)
        if not user:
            return None

        total = await beer_repo.get_user_total(user.id, group_id)
        by_type = await beer_repo.get_user_stats_by_type(user.id, group_id)

        # Convert DrinkType keys to strings
        breakdown = {dt.value: count for dt, count in by_type.items()}

        # Get rank by counting users with more drinks
        leaderboard = await beer_repo.get_leaderboard_with_breakdown(group_id, None, 100)
        rank = 1
        for name, count, _ in leaderboard:
            if name == user.name:
                break
            rank += 1

        return {"total": total, "rank": rank, "breakdown": breakdown}

    async def undo_beer(self, message: GroupMeMessage, target_user_id: str | None = None) -> str:
        """Undo the last beer for a user.

        If target_user_id is provided (from a mention), undo for that user.
        Otherwise, undo for the sender.
        """
        # Determine which user to undo for
        if target_user_id:
            target_user = await user_repo.get_by_groupme_id(target_user_id)
            if not target_user:
                return "Could not find that user. They may not have logged any beers yet."
            target_name = target_user.name
        else:
            target_user = await user_repo.get_or_create(
                groupme_user_id=message.user_id,
                name=message.name,
                avatar_url=message.avatar_url,
            )
            target_name = message.name

        # Delete the last beer entry (returns quantity removed)
        quantity_removed = await beer_repo.delete_last_beer(target_user.id, message.group_id)

        if quantity_removed == 0:
            return f"{target_name} has no beers to undo!"

        # Get new total
        total = await beer_repo.get_user_total(target_user.id, message.group_id)
        beer_word = "beer" if quantity_removed == 1 else "beers"
        return f"Removed {quantity_removed} {beer_word} from {target_name}. New total: {total}."

    async def unbeer(
        self,
        message: GroupMeMessage,
        quantity: int,
        target_user_id: str | None = None,
    ) -> str:
        """Remove a specific number of beers from a user.

        If target_user_id is provided (from a mention), remove from that user.
        Otherwise, remove from the sender.
        """
        # Determine which user to remove from
        if target_user_id:
            target_user = await user_repo.get_by_groupme_id(target_user_id)
            if not target_user:
                return "Could not find that user. They may not have logged any beers yet."
            target_name = target_user.name
        else:
            target_user = await user_repo.get_or_create(
                groupme_user_id=message.user_id,
                name=message.name,
                avatar_url=message.avatar_url,
            )
            target_name = message.name

        # Remove the beers
        quantity_removed = await beer_repo.remove_beers(
            target_user.id, message.group_id, quantity
        )

        if quantity_removed == 0:
            return f"{target_name} has no beers to remove!"

        # Get new total
        total = await beer_repo.get_user_total(target_user.id, message.group_id)
        beer_word = "beer" if quantity_removed == 1 else "beers"

        if quantity_removed < quantity:
            return f"Removed all {quantity_removed} {beer_word} from {target_name} (only had {quantity_removed}). New total: {total}."

        return f"Removed {quantity_removed} {beer_word} from {target_name}. New total: {total}."

    async def remove_drinks_by_type(
        self,
        message: GroupMeMessage,
        quantity: int,
        drink_type: DrinkType,
        target_user_id: str | None = None,
    ) -> str:
        """Remove a specific number of drinks of a specific type.

        If target_user_id is provided (from a mention), remove from that user.
        Otherwise, remove from the sender.
        """
        # Determine which user to remove from
        if target_user_id:
            target_user = await user_repo.get_by_groupme_id(target_user_id)
            if not target_user:
                return "Could not find that user. They may not have logged any drinks yet."
            user = target_user
            target_name = target_user.name
        else:
            user = await user_repo.get_or_create(
                groupme_user_id=message.user_id,
                name=message.name,
                avatar_url=message.avatar_url,
            )
            target_name = message.name

        # Remove drinks of this type
        quantity_removed = await beer_repo.remove_beers_by_type(
            user.id, message.group_id, quantity, drink_type
        )

        # Drink type naming
        drink_names = {
            DrinkType.BEER: ("beer", "beers"),
            DrinkType.WINE: ("wine", "wines"),
            DrinkType.COCKTAIL: ("cocktail", "cocktails"),
            DrinkType.CLAW: ("claw", "claws"),
        }
        singular, plural = drink_names.get(drink_type, ("drink", "drinks"))
        drink_word = singular if quantity_removed == 1 else plural

        if quantity_removed == 0:
            return f"{target_name} has no {plural} to remove!"

        # Get new total for this drink type
        new_total = await beer_repo.get_user_total_by_type(user.id, message.group_id, drink_type)

        if quantity_removed < quantity:
            return f"Removed all {quantity_removed} {drink_word} from {target_name} (only had {quantity_removed}). {singular.title()} total: {new_total}."

        return f"Removed {quantity_removed} {drink_word} from {target_name}. {singular.title()} total: {new_total}."

    async def add_split(
        self,
        message: GroupMeMessage,
        target_user_id: str | None = None,
        target_name: str | None = None,
    ) -> str:
        """Manually add a split-the-G (also logs 1 beer).

        If target_user_id is provided (from a mention), add for that user.
        Otherwise, add for the sender.
        """
        # Determine which user to add for
        if target_user_id:
            user = await user_repo.get_or_create(
                groupme_user_id=target_user_id,
                name=target_name or f"User {target_user_id[-4:]}",
                avatar_url=None,
            )
            display_name = user.name
        else:
            user = await user_repo.get_or_create(
                groupme_user_id=message.user_id,
                name=message.name,
                avatar_url=message.avatar_url,
            )
            display_name = message.name

        # Log 1 beer with split_the_g=1
        beer = await beer_repo.create(
            user_id=user.id,
            group_id=message.group_id,
            quantity=1,
            message_id=message.id,
            split_the_g=1,
            drink_type=DrinkType.BEER,
        )

        if beer is None:
            return "Already processed this message."

        # Get totals
        beer_total = await beer_repo.get_user_total(user.id, message.group_id)
        split_total = await beer_repo.get_user_split_g_total(user.id, message.group_id)

        return f"🍀 Split the G logged for {display_name}! +1 beer, {split_total} total splits, {beer_total} beers."

    async def unsplit(
        self,
        message: GroupMeMessage,
        quantity: int,
        target_user_id: str | None = None,
    ) -> str:
        """Remove a specific number of split-the-G counts from a user.

        If target_user_id is provided (from a mention), remove from that user.
        Otherwise, remove from the sender.
        """
        # Determine which user to remove from
        if target_user_id:
            target_user = await user_repo.get_by_groupme_id(target_user_id)
            if not target_user:
                return "Could not find that user. They may not have logged any beers yet."
            target_name = target_user.name
        else:
            target_user = await user_repo.get_or_create(
                groupme_user_id=message.user_id,
                name=message.name,
                avatar_url=message.avatar_url,
            )
            target_name = message.name

        # Remove the splits
        quantity_removed = await beer_repo.remove_splits(
            target_user.id, message.group_id, quantity
        )

        if quantity_removed == 0:
            return f"{target_name} has no splits to remove!"

        # Get new total
        total = await beer_repo.get_user_split_g_total(target_user.id, message.group_id)
        split_word = "split" if quantity_removed == 1 else "splits"

        if quantity_removed < quantity:
            return f"Removed all {quantity_removed} {split_word} from {target_name} (only had {quantity_removed}). New total: {total}."

        return f"Removed {quantity_removed} {split_word} from {target_name}. New total: {total}."

    async def get_million_countdown(self, group_id: str, drink_type: DrinkType | None = None) -> str:
        """Get time-to-million-drinks projection.

        Args:
            group_id: The group to query
            drink_type: Filter by drink type, or None for all drinks
        """
        GOAL = 1_000_000

        # Get total for the filtered type (or all)
        total = await beer_repo.get_group_total_by_type(group_id, drink_type)

        # Get rate data for projections (filtered by drink type if specified)
        _, beers_7d, days_7d = await beer_repo.get_rate_stats(group_id, days=7, drink_type=drink_type)
        _, beers_30d, days_30d = await beer_repo.get_rate_stats(group_id, days=30, drink_type=drink_type)

        # Determine label based on filter
        if drink_type is None:
            drink_label = "drinks"
            title = "Road to 1 Million Drinks:"
        else:
            drink_label = f"{drink_type.value}s"
            title = f"Road to 1 Million {drink_type.value.title()}s:"

        # Handle no data case
        if total == 0:
            return f"No {drink_label} logged yet! Start drinking to see your countdown to 1 million."

        remaining = GOAL - total

        if remaining <= 0:
            return f"Congratulations! You've reached {total:,} {drink_label} - you've passed 1 million!"

        lines = [
            title,
            f"Current total: {total:,}",
            f"Remaining: {remaining:,}",
            "",
        ]

        # Calculate and display rates
        if days_7d > 0 and beers_7d > 0:
            rate_7d = beers_7d / days_7d
            days_to_goal_7d = remaining / rate_7d
            projection_7d = self._format_duration(days_to_goal_7d)
            lines.append(f"7-day pace: {rate_7d:.1f} {drink_label}/day")
            lines.append(f"At this pace: {projection_7d}")
        else:
            lines.append("7-day pace: Not enough recent data")

        lines.append("")

        if days_30d > 0 and beers_30d > 0:
            rate_30d = beers_30d / days_30d
            days_to_goal_30d = remaining / rate_30d
            projection_30d = self._format_duration(days_to_goal_30d)
            lines.append(f"30-day pace: {rate_30d:.1f} {drink_label}/day")
            lines.append(f"At this pace: {projection_30d}")

            # Add estimated date (Eastern time)
            target_date = datetime.now(EASTERN) + timedelta(days=days_to_goal_30d)
            lines.append(f"Target date: {target_date.strftime('%B %d, %Y')}")
        else:
            lines.append("30-day pace: Not enough data")

        return "\n".join(lines)

    def _format_duration(self, total_days: float) -> str:
        """Format a number of days into human-readable duration."""
        if total_days < 1:
            hours = int(total_days * 24)
            return f"{hours} hours"

        years = int(total_days // 365)
        remaining_days = total_days % 365
        months = int(remaining_days // 30)
        days = int(remaining_days % 30)

        parts = []
        if years > 0:
            parts.append(f"{years} year{'s' if years != 1 else ''}")
        if months > 0:
            parts.append(f"{months} month{'s' if months != 1 else ''}")
        if days > 0 and years == 0:  # Only show days if less than a year
            parts.append(f"{days} day{'s' if days != 1 else ''}")

        if not parts:
            return "less than a day"

        return ", ".join(parts)

    async def get_split_g_leaderboard(self, group_id: str, limit: int = 10) -> str:
        """Get formatted split-the-G leaderboard."""
        stats = await beer_repo.get_split_g_stats(group_id)

        if not stats.user_stats:
            return "No one has split the G yet! Post a Guinness with the perfect pour. 🍀"

        lines = ["🍀 Split the G Leaderboard:"]

        for i, user_stat in enumerate(stats.user_stats[:limit]):
            prefix = f"{i + 1}."
            lines.append(f"{prefix} {user_stat.name} - {user_stat.split_the_g_count} splits")

        lines.append(f"\nTotal splits: {stats.total_splits}")
        return "\n".join(lines)

    async def add_debt(
        self,
        message: GroupMeMessage,
        amount: int,
        debtor_user_id: str,
        debtor_name: str,
    ) -> str:
        """Add debt to a user (they owe the group beers)."""
        # Get or create the debtor
        debtor = await user_repo.get_or_create(
            groupme_user_id=debtor_user_id,
            name=debtor_name,
            avatar_url=None,
        )

        # Add to their debt
        new_total = await debt_repo.add_debt(debtor.id, message.group_id, amount)

        beer_word = "beer" if amount == 1 else "beers"
        return f"{debtor.name} now owes {amount} {beer_word}. Total debt: {new_total} beers."

    async def forgive_debt(
        self,
        message: GroupMeMessage,
        amount: int,
        debtor_user_id: str,
        debtor_name: str,
    ) -> str:
        """Forgive (reduce) debt for a user without them drinking."""
        # Get the debtor
        debtor = await user_repo.get_by_groupme_id(debtor_user_id)
        if not debtor:
            return f"Could not find {debtor_name}. They may not have any debt."

        # Get current debt
        current_debt = await debt_repo.get_debt(debtor.id, message.group_id)
        if current_debt <= 0:
            return f"{debtor.name} doesn't owe any beers!"

        # Reduce debt
        reduced = await debt_repo.reduce_debt(debtor.id, message.group_id, amount)
        remaining = await debt_repo.get_debt(debtor.id, message.group_id)

        beer_word = "beer" if reduced == 1 else "beers"
        if remaining > 0:
            return f"Forgave {reduced} {beer_word} for {debtor.name}. They still owe {remaining}."
        else:
            return f"Forgave {reduced} {beer_word} for {debtor.name}. Debt cleared! 🎉"

    async def get_debt_leaderboard(self, group_id: str) -> str:
        """Get the debt leaderboard (who owes the most)."""
        leaderboard = await debt_repo.get_debt_leaderboard(group_id)

        if not leaderboard:
            return "No one owes any beers! 🎉"

        lines = ["🍺 Beer Debt Leaderboard (who owes the most):"]
        for i, entry in enumerate(leaderboard):
            lines.append(f"{i + 1}. {entry.name} - {entry.amount} beers")

        return "\n".join(lines)

    def get_help(self) -> str:
        """Get help message."""
        return """Beerbot Commands:
Log drinks:
- 🍺 beer me / cheers / +N beers
- 🍷 wine me / +N wines
- 🍸 cocktail me / mix me / +N cocktails
- 🥤 claw me / seltzer me / +N claws
- +N mimosas/shots/etc → counts as cocktails
- Post a photo - Auto-detects by glass type!

Tagging others:
- +N drinks @user → logs for @user only
- 🍺 @user → logs for you AND @user
- -N drinks @user → removes from @user

Stats:
- !beers - Group drink count
- !mystats - Your breakdown by type
- !leaderboard [beer|wine|cocktail|claw]
- !today / !week [beer|wine|cocktail|claw]
- !million [beer|wine|cocktail|claw] - Road to 1M

Split the G:
- Post a Guinness at the G level 🍀
- !split [@user] - Manually add a split
- !splitg - Leaderboard
- !unsplit [N] [@user]

Debts:
- !owe [@user] or !owe N @user
- !forgive @user or !forgive N @user
- !debts - Who owes

Other:
- !undo / !unbeer N [@user]
- !toast - Get a fun drinking toast"""

    def _format_group_stats(
        self, stats: GroupStats, drink_type: DrinkType | None = None
    ) -> str:
        """Format GroupStats into a message."""
        # Determine title based on filter
        if drink_type:
            type_name = drink_type.value.title()
            type_label = f"{type_name}s"
        else:
            type_label = "Drinks"

        if stats.total_beers == 0:
            return f"No {type_label.lower()} logged {stats.period_description}. Time to change that!"

        # Build drink type breakdown with emojis
        type_counts = stats.drink_type_counts
        breakdown_parts = []
        for dt, emoji in [("beer", "🍺"), ("wine", "🍷"), ("cocktail", "🍸"), ("claw", "🥤")]:
            count = type_counts.get(dt, 0)
            breakdown_parts.append(f"{emoji}{count}")

        lines = [
            f"{type_label} ({stats.period_description}):",
            f"Total: {stats.total_beers} | " + " ".join(breakdown_parts),
            f"Drinkers: {stats.unique_drinkers}",
        ]

        if stats.user_stats:
            lines.append("")
            lines.append("Top 3:")
            for i, user_stat in enumerate(stats.user_stats[:3]):
                lines.append(f"{i + 1}. {user_stat.name} - {user_stat.total_beers}")

        return "\n".join(lines)


# Singleton instances
message_parser = MessageParser()
stats_service = StatsService()
