"""GroupMe API client for sending bot messages."""

import httpx

from .config import settings


class GroupMeClient:
    """Client for interacting with the GroupMe Bot API."""

    API_URL = "https://api.groupme.com/v3/bots/post"

    def __init__(self, bot_id: str | None = None):
        self.bot_id = bot_id or settings.beerbot_bot_id

    async def send_message(self, text: str) -> bool:
        """Send a message to the group.

        Returns True if successful, False otherwise.
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.API_URL,
                    json={"bot_id": self.bot_id, "text": text},
                    timeout=10.0,
                )
                # GroupMe returns 202 on success
                return response.status_code == 202
            except httpx.RequestError:
                return False


# Singleton instance
groupme_client = GroupMeClient()
