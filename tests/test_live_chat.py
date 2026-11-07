"""Live chat scenarios: real model, real tools, real PostgreSQL, real durable pipeline.

Skipped unless BEERBOT_LIVE_LLM=1 (plus a model key and BEERBOT_TEST_DATABASE_URL).
beerdev runs these before shipping; add one scenario per chat feature.
"""

import os

import pytest

from src.beerbot import database
from src.beerbot.agent import BeerAgent
from src.beerbot.delivery import accept_message, execute_one
from src.beerbot.models import GroupMeMessage

pytestmark = pytest.mark.skipif(
    os.environ.get("BEERBOT_LIVE_LLM") != "1", reason="set BEERBOT_LIVE_LLM=1 to call the model"
)


@pytest.fixture
async def chat(pg):
    """Returns send(text, sender) -> Beerius's reply (None means it chose silence)."""
    await database.init_db()
    await pg.execute("INSERT INTO groups(group_id,bot_id) VALUES('g1','fake')")
    agent = BeerAgent()

    async def send(text: str, sender: str = "alice") -> str | None:
        number = await pg.fetchval("SELECT COUNT(*) FROM message_inbox")
        await accept_message(
            GroupMeMessage(
                id=f"live-{number}",
                group_id="g1",
                user_id=sender,
                sender_id=sender,
                sender_type="user",
                name=sender.title(),
                text=text,
                created_at=0,
            )
        )
        assert await execute_one(agent)
        row = await pg.fetchrow("SELECT state,reply FROM message_inbox ORDER BY id DESC LIMIT 1")
        assert row["state"] == "completed"
        return row["reply"]

    return send


async def test_logs_a_beer_and_answers_stats(chat, pg):
    assert await chat("+1 beer 🍺")
    assert await pg.fetchval("SELECT SUM(quantity) FROM beers WHERE drink_type='beer'") == 1
    assert await chat("how many beers have I had?")


async def test_logs_zyn_pouches_without_touching_drinks(chat, pg):
    assert await chat("+2 zyn")
    assert await pg.fetchval("SELECT COALESCE(SUM(quantity),0) FROM zyn_logs") == 2
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0


async def test_leaves_feature_requests_to_beerdev(chat, pg):
    assert await chat("someone should make this bot track our gym sessions") is None
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0
