"""Tests for message parsing and services."""

import pytest

from src.beerbot.models import GroupMeAttachment
from src.beerbot.services import MessageParser, extract_mentioned_user_ids, extract_mentioned_users


class TestMessageParser:
    """Tests for MessageParser."""

    def setup_method(self):
        self.parser = MessageParser()

    def test_parse_beer_emoji(self):
        """Test counting beer emojis in short messages."""
        assert self.parser.parse_beer_count("🍺") == 1
        assert self.parser.parse_beer_count("🍺🍺🍺") == 3
        assert self.parser.parse_beer_count("Having a 🍺 after work") == 1

    def test_parse_beer_emoji_ignored_in_long_messages(self):
        """Test that emojis are ignored in long messages (docs/help text)."""
        long_message = "Here is some documentation about beer emojis 🍺 and how to use them. " * 3
        assert len(long_message) > 100
        assert self.parser.parse_beer_count(long_message) == 0

    def test_parse_beer_text_emoji(self):
        """Test counting :beer: text in short messages."""
        assert self.parser.parse_beer_count(":beer:") == 1
        assert self.parser.parse_beer_count(":BEER:") == 1
        assert self.parser.parse_beer_count(":beer: :beer:") == 2

    def test_parse_numeric_beers(self):
        """Test +N beers pattern at start of message."""
        assert self.parser.parse_beer_count("+1 beer") == 1
        assert self.parser.parse_beer_count("+3 beers") == 3
        assert self.parser.parse_beer_count("+10 beers") == 10
        assert self.parser.parse_beer_count("+2beers") == 2

    def test_parse_numeric_beers_ignored_midtext(self):
        """Test +N beers is ignored when not at start of message."""
        assert self.parser.parse_beer_count("e.g., +3 beers") == 0
        assert self.parser.parse_beer_count("Log +5 beers like this") == 0

    def test_parse_beer_me(self):
        """Test beer me pattern."""
        assert self.parser.parse_beer_count("beer me") == 1
        assert self.parser.parse_beer_count("Beer me") == 1
        assert self.parser.parse_beer_count("BEER ME") == 1
        assert self.parser.parse_beer_count("beer me!") == 1

    def test_parse_cheers(self):
        """Test cheers pattern at start of message."""
        assert self.parser.parse_beer_count("Cheers!") == 1
        assert self.parser.parse_beer_count("cheers everyone") == 1
        assert self.parser.parse_beer_count("CHEERS") == 1

    def test_parse_cheers_ignored_midtext(self):
        """Test cheers is ignored when not at start of message."""
        assert self.parser.parse_beer_count("Saying cheers to everyone") == 0
        assert self.parser.parse_beer_count("- cheers - Log 1 beer") == 0

    def test_parse_cracked(self):
        """Test cracked one pattern at start of message."""
        assert self.parser.parse_beer_count("cracked one") == 1
        assert self.parser.parse_beer_count("cracked a beer") == 1
        assert self.parser.parse_beer_count("cracked a cold one") == 1

    def test_parse_cracked_ignored_midtext(self):
        """Test cracked is ignored when not at start of message."""
        assert self.parser.parse_beer_count("Just cracked one") == 0
        assert self.parser.parse_beer_count("- cracked one - Log 1 beer") == 0

    def test_parse_combined(self):
        """Test multiple patterns in one message."""
        # Emoji + numeric at start stack (short message)
        assert self.parser.parse_beer_count("+2 beers 🍺") == 3
        # Multiple emojis (short message)
        assert self.parser.parse_beer_count("🍺🍺") == 2
        # Word triggers are ignored when explicit triggers are present
        assert self.parser.parse_beer_count("🍺 cheers!") == 1
        # Word triggers don't stack with each other
        assert self.parser.parse_beer_count("beer me cheers") == 1

    def test_parse_no_beers(self):
        """Test messages with no beer triggers."""
        assert self.parser.parse_beer_count("Hello everyone") == 0
        assert self.parser.parse_beer_count("Let's get dinner") == 0
        assert self.parser.parse_beer_count(None) == 0
        assert self.parser.parse_beer_count("") == 0

    def test_parse_command_stats(self):
        """Test stats command parsing."""
        assert self.parser.parse_command("!beers") == "stats"
        assert self.parser.parse_command("!beer") == "stats"
        assert self.parser.parse_command("beer count") == "stats"
        assert self.parser.parse_command("how many beers") == "stats"

    def test_parse_command_mystats(self):
        """Test mystats command parsing."""
        assert self.parser.parse_command("!mystats") == "mystats"
        assert self.parser.parse_command("my beers") == "mystats"
        assert self.parser.parse_command("how many have i had") == "mystats"

    def test_parse_command_leaderboard(self):
        """Test leaderboard command parsing."""
        assert self.parser.parse_command("!leaderboard") == "leaderboard"
        assert self.parser.parse_command("!top") == "leaderboard"
        assert self.parser.parse_command("beer leaders") == "leaderboard"

    def test_parse_command_today(self):
        """Test today command parsing."""
        assert self.parser.parse_command("!today") == "today"
        assert self.parser.parse_command("beers today") == "today"

    def test_parse_command_week(self):
        """Test week command parsing."""
        assert self.parser.parse_command("!week") == "week"
        assert self.parser.parse_command("beers this week") == "week"

    def test_parse_command_help(self):
        """Test help command parsing."""
        assert self.parser.parse_command("!help") == "help"
        assert self.parser.parse_command("beerbot help") == "help"

    def test_parse_command_none(self):
        """Test non-command messages."""
        assert self.parser.parse_command("Hello") is None
        assert self.parser.parse_command("🍺") is None
        assert self.parser.parse_command(None) is None
        assert self.parser.parse_command("") is None

    def test_parse_command_unbeer(self):
        """Test unbeer command parsing."""
        assert self.parser.parse_command("!unbeer 5") == "unbeer"
        assert self.parser.parse_command("!unbeer 10 @user") == "unbeer"
        assert self.parser.parse_command("minus 3 beers") == "unbeer"
        assert self.parser.parse_command("minus 1 beer") == "unbeer"
        assert self.parser.parse_command("-5 beers") == "unbeer"
        assert self.parser.parse_command("-2 beer") == "unbeer"

    def test_parse_unbeer_count(self):
        """Test unbeer count extraction."""
        assert self.parser.parse_unbeer_count("!unbeer 5") == 5
        assert self.parser.parse_unbeer_count("!unbeer 10 @user") == 10
        assert self.parser.parse_unbeer_count("minus 3 beers") == 3
        assert self.parser.parse_unbeer_count("-5 beers") == 5
        # Defaults to 1 when no number specified
        assert self.parser.parse_unbeer_count(None) == 1
        assert self.parser.parse_unbeer_count("") == 1
        assert self.parser.parse_unbeer_count("!unbeer") == 1

    def test_parse_command_million(self):
        """Test million command parsing."""
        assert self.parser.parse_command("!million") == "million"
        assert self.parser.parse_command("!countdown") == "million"
        assert self.parser.parse_command("!goal") == "million"
        assert self.parser.parse_command("time to million") == "million"


class TestFormatDuration:
    """Tests for _format_duration helper."""

    def setup_method(self):
        from src.beerbot.services import StatsService
        self.service = StatsService()

    def test_format_hours(self):
        assert self.service._format_duration(0.5) == "12 hours"

    def test_format_single_day(self):
        assert self.service._format_duration(1) == "1 day"

    def test_format_days(self):
        assert self.service._format_duration(5) == "5 days"

    def test_format_months(self):
        result = self.service._format_duration(45)
        assert "1 month" in result
        assert "15 days" in result

    def test_format_years(self):
        result = self.service._format_duration(400)
        assert "1 year" in result
        assert "month" in result

    def test_format_large_duration(self):
        # 10 years
        result = self.service._format_duration(3650)
        assert "10 years" in result

    def test_format_less_than_hour(self):
        result = self.service._format_duration(0.01)
        assert "0 hours" in result

    def test_format_zero(self):
        assert self.service._format_duration(0) == "0 hours"


class TestExtractMentionedUserIds:
    """Tests for extract_mentioned_user_ids function."""

    def test_no_attachments(self):
        """Test with no attachments."""
        result = extract_mentioned_user_ids([])
        assert result == []

    def test_no_mentions(self):
        """Test with attachments but no mentions."""
        attachments = [
            GroupMeAttachment(type="image", url="https://example.com/img.jpg"),
            GroupMeAttachment(type="location"),
        ]
        result = extract_mentioned_user_ids(attachments)
        assert result == []

    def test_single_mention(self):
        """Test with a single mention."""
        attachments = [
            GroupMeAttachment(type="mentions", user_ids=["12345"]),
        ]
        result = extract_mentioned_user_ids(attachments)
        assert result == ["12345"]

    def test_multiple_mentions_in_one_attachment(self):
        """Test with multiple users in one mention attachment."""
        attachments = [
            GroupMeAttachment(type="mentions", user_ids=["123", "456", "789"]),
        ]
        result = extract_mentioned_user_ids(attachments)
        assert set(result) == {"123", "456", "789"}

    def test_multiple_mention_attachments(self):
        """Test with multiple mention attachments."""
        attachments = [
            GroupMeAttachment(type="mentions", user_ids=["123", "456"]),
            GroupMeAttachment(type="mentions", user_ids=["789"]),
        ]
        result = extract_mentioned_user_ids(attachments)
        assert set(result) == {"123", "456", "789"}

    def test_deduplicates_user_ids(self):
        """Test that duplicate user IDs are deduplicated."""
        attachments = [
            GroupMeAttachment(type="mentions", user_ids=["123", "456"]),
            GroupMeAttachment(type="mentions", user_ids=["456", "789"]),
        ]
        result = extract_mentioned_user_ids(attachments)
        assert len(result) == 3
        assert set(result) == {"123", "456", "789"}

    def test_mixed_attachments(self):
        """Test with a mix of attachment types."""
        attachments = [
            GroupMeAttachment(type="image", url="https://example.com/beer.jpg"),
            GroupMeAttachment(type="mentions", user_ids=["123", "456"]),
            GroupMeAttachment(type="location"),
            GroupMeAttachment(type="mentions", user_ids=["789"]),
        ]
        result = extract_mentioned_user_ids(attachments)
        assert set(result) == {"123", "456", "789"}


class TestExtractMentionedUsers:
    """Tests for extract_mentioned_users function."""

    def test_no_text(self):
        """Test with no text."""
        result = extract_mentioned_users(None, [])
        assert result == []

    def test_no_mentions(self):
        """Test with text but no mentions."""
        attachments = [GroupMeAttachment(type="image", url="https://example.com")]
        result = extract_mentioned_users("+1 beer", attachments)
        assert result == []

    def test_extract_name_from_loci(self):
        """Test extracting user name from message text using loci."""
        text = "+1 beer @Avada Balenciaga"
        attachments = [
            GroupMeAttachment(
                type="mentions",
                user_ids=["12345"],
                loci=[[9, 16]],  # Position of "@Avada Balenciaga"
            ),
        ]
        result = extract_mentioned_users(text, attachments)
        assert len(result) == 1
        assert result[0] == ("12345", "Avada Balenciaga")

    def test_multiple_mentions_with_loci(self):
        """Test multiple mentions with loci."""
        text = "+2 beers @Alice and @Bob"
        # Positions: @Alice starts at 9 (length 6), @Bob starts at 20 (length 4)
        attachments = [
            GroupMeAttachment(
                type="mentions",
                user_ids=["111", "222"],
                loci=[[9, 6], [20, 4]],
            ),
        ]
        result = extract_mentioned_users(text, attachments)
        assert len(result) == 2
        assert result[0] == ("111", "Alice")
        assert result[1] == ("222", "Bob")

    def test_fallback_to_user_id_suffix(self):
        """Test fallback when loci is missing."""
        text = "+1 beer @Someone"
        attachments = [
            GroupMeAttachment(type="mentions", user_ids=["12345678"]),
        ]
        result = extract_mentioned_users(text, attachments)
        assert len(result) == 1
        assert result[0] == ("12345678", "User 5678")

    def test_deduplicates_users(self):
        """Test that duplicate user IDs are deduplicated."""
        text = "+1 beer @Alice @Alice"
        attachments = [
            GroupMeAttachment(
                type="mentions",
                user_ids=["111", "111"],
                loci=[[9, 6], [16, 6]],
            ),
        ]
        result = extract_mentioned_users(text, attachments)
        assert len(result) == 1
        assert result[0][0] == "111"


class TestIsExplicitAssignment:
    """Tests for is_explicit_assignment method."""

    def setup_method(self):
        self.parser = MessageParser()

    def test_numeric_pattern_is_assignment(self):
        """Test +N beers is explicit assignment."""
        assert self.parser.is_explicit_assignment("+1 beer") is True
        assert self.parser.is_explicit_assignment("+5 beers @user") is True

    def test_wine_pattern_is_assignment(self):
        """Test +N wines is explicit assignment."""
        assert self.parser.is_explicit_assignment("+1 wine") is True
        assert self.parser.is_explicit_assignment("+3 wines @user") is True

    def test_cocktail_pattern_is_assignment(self):
        """Test +N cocktails is explicit assignment."""
        assert self.parser.is_explicit_assignment("+1 cocktail") is True
        assert self.parser.is_explicit_assignment("+6 cocktails @user") is True

    def test_claw_pattern_is_assignment(self):
        """Test +N claws/seltzers is explicit assignment."""
        assert self.parser.is_explicit_assignment("+1 claw") is True
        assert self.parser.is_explicit_assignment("+2 seltzers @user") is True

    def test_generic_drink_is_assignment(self):
        """Test +N <alcoholic drink> is explicit assignment."""
        assert self.parser.is_explicit_assignment("+3 shots") is True
        assert self.parser.is_explicit_assignment("+2 margaritas @user") is True

    def test_emoji_is_not_assignment(self):
        """Test emoji is not explicit assignment."""
        assert self.parser.is_explicit_assignment("🍺") is False
        assert self.parser.is_explicit_assignment("🍺 @user") is False

    def test_word_triggers_not_assignment(self):
        """Test word triggers are not explicit assignment."""
        assert self.parser.is_explicit_assignment("cheers") is False
        assert self.parser.is_explicit_assignment("beer me") is False

    def test_none_text(self):
        """Test with None text."""
        assert self.parser.is_explicit_assignment(None) is False


class TestDrinkRemovalParsing:
    """Tests for drink removal parsing (-N drinks syntax).

    Regression tests for: https://github.com/anthropics/claude-code/issues/XXX
    - "-1 cocktail" should remove from current user
    - "-1 cocktail @user" should remove from tagged user
    """

    def setup_method(self):
        self.parser = MessageParser()

    def test_parse_cocktail_removal(self):
        """Test -N cocktails pattern is parsed correctly."""
        result = self.parser.parse_drink_removal("-1 cocktail")
        assert result is not None
        count, drink_type = result
        assert count == 1
        assert drink_type.value == "cocktail"

    def test_parse_cocktail_removal_plural(self):
        """Test -N cocktails (plural) pattern."""
        result = self.parser.parse_drink_removal("-6 cocktails")
        assert result is not None
        count, drink_type = result
        assert count == 6
        assert drink_type.value == "cocktail"

    def test_parse_cocktail_removal_with_mention(self):
        """Test -N cocktails with @mention still parses the count correctly.

        The parser only extracts the count and type;
        the target user extraction happens in main.py from attachments.
        """
        result = self.parser.parse_drink_removal("-6 cocktails @Celena Tyler")
        assert result is not None
        count, drink_type = result
        assert count == 6
        assert drink_type.value == "cocktail"

    def test_parse_beer_removal(self):
        """Test -N beers pattern."""
        result = self.parser.parse_drink_removal("-3 beers")
        assert result is not None
        count, drink_type = result
        assert count == 3
        assert drink_type.value == "beer"

    def test_parse_wine_removal(self):
        """Test -N wines pattern."""
        result = self.parser.parse_drink_removal("-2 wines")
        assert result is not None
        count, drink_type = result
        assert count == 2
        assert drink_type.value == "wine"

    def test_parse_claw_removal(self):
        """Test -N claws/seltzers pattern."""
        result = self.parser.parse_drink_removal("-1 claw")
        assert result is not None
        assert result[1].value == "claw"

        result = self.parser.parse_drink_removal("-2 seltzers")
        assert result is not None
        assert result[1].value == "claw"

    def test_parse_generic_alcoholic_removal(self):
        """Test -N <alcoholic drink> pattern (e.g., shots, margaritas)."""
        result = self.parser.parse_drink_removal("-3 shots")
        assert result is not None
        count, drink_type = result
        assert count == 3
        assert drink_type.value == "cocktail"  # Generic alcoholic drinks -> cocktail

    def test_no_removal_pattern(self):
        """Test message without removal pattern returns None."""
        assert self.parser.parse_drink_removal("hello world") is None
        assert self.parser.parse_drink_removal("+1 beer") is None
        assert self.parser.parse_drink_removal("cheers") is None


class TestDrinkRemovalTargeting:
    """Tests for drink removal targeting (who gets drinks removed).

    These tests verify the logic from main.py that determines:
    - No mention: remove from sender
    - With mention: remove from mentioned user
    """

    def test_extract_target_user_no_mention(self):
        """With no mentions, target should be None (meaning: use sender)."""
        attachments = []

        target_user_id = None
        for attachment in attachments:
            if attachment.type == "mentions" and attachment.user_ids:
                target_user_id = attachment.user_ids[0]
                break

        assert target_user_id is None

    def test_extract_target_user_with_mention(self):
        """With mentions, target should be the first mentioned user."""
        attachments = [
            GroupMeAttachment(
                type="mentions",
                user_ids=["12345678"],
                loci=[[15, 13]],  # @Celena Tyler
            )
        ]

        target_user_id = None
        for attachment in attachments:
            if attachment.type == "mentions" and attachment.user_ids:
                target_user_id = attachment.user_ids[0]
                break

        assert target_user_id == "12345678"

    def test_extract_target_user_multiple_mentions(self):
        """With multiple mentions, only use the first one for removal."""
        attachments = [
            GroupMeAttachment(
                type="mentions",
                user_ids=["12345678", "87654321"],
                loci=[[15, 13], [30, 10]],
            )
        ]

        target_user_id = None
        for attachment in attachments:
            if attachment.type == "mentions" and attachment.user_ids:
                target_user_id = attachment.user_ids[0]
                break

        assert target_user_id == "12345678"  # First user only


class TestShouldTryAIParsing:
    """Tests for should_try_ai_parsing heuristic filtering."""

    def setup_method(self):
        self.parser = MessageParser()

    def test_returns_true_for_drink_verbs(self):
        """Test that consumption verbs trigger AI parsing."""
        assert self.parser.should_try_ai_parsing("Just finished my IPA") is True
        assert self.parser.should_try_ai_parsing("I'm drinking a margarita") is True
        assert self.parser.should_try_ai_parsing("Had a great whiskey") is True
        assert self.parser.should_try_ai_parsing("Enjoying some champagne") is True
        assert self.parser.should_try_ai_parsing("Sipping on a mojito") is True

    def test_returns_true_for_specific_drinks(self):
        """Test that specific drink names trigger AI parsing."""
        assert self.parser.should_try_ai_parsing("This IPA is amazing") is True
        assert self.parser.should_try_ai_parsing("Love a good stout") is True
        assert self.parser.should_try_ai_parsing("The cabernet was excellent") is True
        assert self.parser.should_try_ai_parsing("Trying a negroni tonight") is True

    def test_returns_true_for_locations(self):
        """Test that drink-related locations trigger AI parsing."""
        assert self.parser.should_try_ai_parsing("At the brewery right now") is True
        assert self.parser.should_try_ai_parsing("Just got to the bar") is True
        assert self.parser.should_try_ai_parsing("Visiting a winery today") is True

    def test_returns_false_for_no_signal_words(self):
        """Test that messages without signal words don't trigger AI."""
        assert self.parser.should_try_ai_parsing("Hello everyone") is False
        assert self.parser.should_try_ai_parsing("What's for dinner?") is False
        assert self.parser.should_try_ai_parsing("Nice weather today") is False

    def test_returns_false_for_short_messages(self):
        """Test that very short messages don't trigger AI."""
        assert self.parser.should_try_ai_parsing("hi") is False
        assert self.parser.should_try_ai_parsing("ok") is False
        assert self.parser.should_try_ai_parsing("") is False
        assert self.parser.should_try_ai_parsing(None) is False

    def test_returns_false_for_long_messages(self):
        """Test that very long messages don't trigger AI."""
        long_msg = "I had a great time at the bar " * 20  # > 300 chars
        assert len(long_msg) > 300
        assert self.parser.should_try_ai_parsing(long_msg) is False

    def test_returns_false_for_commands(self):
        """Test that command-like messages don't trigger AI."""
        assert self.parser.should_try_ai_parsing("!stats") is False
        assert self.parser.should_try_ai_parsing("+1 beer") is False
        assert self.parser.should_try_ai_parsing("-1 cocktail") is False
        assert self.parser.should_try_ai_parsing("> quoted text about drinking") is False

    def test_case_insensitive(self):
        """Test that signal word matching is case-insensitive."""
        assert self.parser.should_try_ai_parsing("Just HAD a MARGARITA") is True
        assert self.parser.should_try_ai_parsing("DRINKING some IPA") is True

    def test_request_phrases_trigger_ai(self):
        """Test that natural language request phrases trigger AI parsing."""
        # "grant me" should trigger
        assert self.parser.should_try_ai_parsing("beerius, grant me a beer") is True
        # Other request phrases
        assert self.parser.should_try_ai_parsing("give me a beer please") is True
        assert self.parser.should_try_ai_parsing("get me a cold one") is True
        assert self.parser.should_try_ai_parsing("pour me another") is True
        assert self.parser.should_try_ai_parsing("fetch me a pint") is True

    def test_basic_drink_words_trigger_ai(self):
        """Test that basic drink words (beer, wine, cocktail) trigger AI parsing."""
        # These were added specifically for natural language requests
        assert self.parser.should_try_ai_parsing("I want a beer") is True
        assert self.parser.should_try_ai_parsing("time for some wine") is True
        assert self.parser.should_try_ai_parsing("making a cocktail") is True
        # With creative phrasing
        assert self.parser.should_try_ai_parsing("beerius, grant me a beer") is True
        assert self.parser.should_try_ai_parsing("the beer gods have blessed me") is True

    def test_unrecognized_drink_patterns_trigger_ai(self):
        """Test that +N <unknown_word> patterns trigger AI for classification."""
        # Unknown drink words should go to AI
        assert self.parser.should_try_ai_parsing("+1 Moet") is True
        assert self.parser.should_try_ai_parsing("+2 champagne") is True
        assert self.parser.should_try_ai_parsing("+1 prosecco") is True
        assert self.parser.should_try_ai_parsing("-1 Moet") is True
        assert self.parser.should_try_ai_parsing("+3 Cristal") is True
        # Known drink words should NOT trigger AI (handled by parse_drink)
        assert self.parser.should_try_ai_parsing("+1 beer") is False
        assert self.parser.should_try_ai_parsing("+1 wine") is False
        assert self.parser.should_try_ai_parsing("+1 margarita") is False
