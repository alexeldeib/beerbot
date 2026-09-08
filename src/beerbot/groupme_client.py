"""GroupMe API client for sending bot messages."""

import logging
from dataclasses import dataclass
from typing import Literal

import httpx

from .config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    state: Literal["sent", "retry", "failed", "uncertain"]
    code: str | None = None
    retry_after: int = 0


class GroupMeClient:
    """Client for interacting with the GroupMe Bot API.

    Supports multi-group operation by looking up bot_id per group.
    Falls back to default bot_id from settings if group not registered.
    """

    API_URL = "https://api.groupme.com/v3/bots/post"

    def __init__(self, default_bot_id: str | None = None):
        self.default_bot_id = default_bot_id or settings.beerbot_bot_id
        self._bot_id_cache: dict[str, str] = {}

    async def _get_bot_id(self, group_id: str | None) -> str | None:
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

        if settings.require_registered_groups:
            logger.warning("Refusing outbound message for unregistered group %s", group_id)
            return None

        logger.warning("Group %s not registered; using legacy default bot_id", group_id)
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
        return (await self.deliver_message(text, group_id)).state == "sent"

    async def deliver_message(self, text: str, group_id: str | None = None) -> DeliveryResult:
        """Classify delivery without automatically repeating ambiguous sends."""
        bot_id = await self._get_bot_id(group_id)
        if not bot_id:
            return DeliveryResult("failed", "unregistered_group")

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.API_URL,
                    json={"bot_id": bot_id, "text": text},
                    timeout=10.0,
                )
                if 200 <= response.status_code < 300:
                    return DeliveryResult("sent")
                code = f"http_{response.status_code}"
                if response.status_code == 429:
                    try:
                        delay = min(86400, max(0, int(response.headers.get("retry-after", "60"))))
                    except ValueError:
                        delay = 60
                    return DeliveryResult("retry", code, delay)
                if response.status_code >= 500 or response.status_code == 408:
                    return DeliveryResult("uncertain", code)
                return DeliveryResult("failed", code)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                return DeliveryResult("retry", type(exc).__name__)
            except httpx.RequestError as exc:
                return DeliveryResult("uncertain", type(exc).__name__)


# Singleton instance
groupme_client = GroupMeClient()
