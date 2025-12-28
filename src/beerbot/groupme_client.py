"""GroupMe API client for sending bot messages."""

import logging

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class GroupMeClient:
    """Client for interacting with the GroupMe Bot API.

    Supports multi-group operation by looking up bot_id per group.
    Falls back to default bot_id from settings if group not registered.
    """

    API_URL = "https://api.groupme.com/v3/bots/post"

    def __init__(self, default_bot_id: str | None = None):
        self.default_bot_id = default_bot_id or settings.beerbot_bot_id
        self._bot_id_cache: dict[str, str] = {}

    async def _get_bot_id(self, group_id: str | None) -> str:
        """Get the bot_id for a group, with caching.

        Falls back to default if group not registered.
        """
        if group_id is None:
            return self.default_bot_id

        # Check cache first
        if group_id in self._bot_id_cache:
            return self._bot_id_cache[group_id]

        # Look up in database
        from .repositories import group_repo

        bot_id = await group_repo.get_bot_id(group_id)

        if bot_id:
            self._bot_id_cache[group_id] = bot_id
            return bot_id

        # Fall back to default
        logger.debug("Group %s not registered, using default bot_id", group_id)
        return self.default_bot_id

    def clear_cache(self, group_id: str | None = None) -> None:
        """Clear cached bot_id lookup. Call after group registration changes."""
        if group_id:
            self._bot_id_cache.pop(group_id, None)
        else:
            self._bot_id_cache.clear()

    async def send_message(self, text: str, group_id: str | None = None) -> bool:
        """Send a message to a group.

        Args:
            text: The message text to send
            group_id: The GroupMe group ID. If provided, looks up the
                     registered bot_id for that group. Falls back to
                     default bot_id if group not registered.

        Returns:
            True if successful, False otherwise.
        """
        bot_id = await self._get_bot_id(group_id)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.API_URL,
                    json={"bot_id": bot_id, "text": text},
                    timeout=10.0,
                )
                # GroupMe returns 202 on success
                return response.status_code == 202
            except httpx.RequestError:
                return False


# Singleton instance
groupme_client = GroupMeClient()
