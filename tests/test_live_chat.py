"""Live chat scenarios: real model, real tools, real PostgreSQL, real durable pipeline.

Skipped unless BEERBOT_LIVE_LLM=1 (plus a model key and BEERBOT_TEST_DATABASE_URL).
beerdev runs these before shipping; add one scenario per chat feature.
"""

import json
import os

import httpx
import pytest

from src.beerbot import beerdev, database
from src.beerbot.agent import BeerAgent
from src.beerbot.delivery import accept_context, accept_message, execute_one
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

    async def send(text: str, sender: str = "alice", attachments: tuple = ()) -> str | None:
        number = await pg.fetchval("SELECT COUNT(*) FROM message_inbox")
        message = GroupMeMessage(
            id=f"live-{number}",
            group_id="g1",
            user_id=sender,
            sender_id=sender,
            sender_type="user",
            name=sender.title(),
            text=text,
            created_at=0,
            attachments=list(attachments),
        )
        # Routed exactly as the callback routes: beerdev's messages never reach Beerius.
        await accept_message(message, "beerdev" if await beerdev.addressed(message) else "beerius")
        assert await execute_one(agent)
        row = await pg.fetchrow("SELECT state,reply FROM message_inbox ORDER BY id DESC LIMIT 1")
        assert row["state"] == "completed"
        return row["reply"]

    async def echo(text: str) -> str:
        """GroupMe posting Beerius's reply back to the callback. Returns the post's id."""
        number = await pg.fetchval("SELECT COUNT(*) FROM message_inbox")
        post = GroupMeMessage(
            id=f"live-{number}",
            group_id="g1",
            user_id="beerius-bot",
            sender_id="beerius-bot",
            sender_type="bot",
            name="beerius",
            text=text,
            created_at=0,
        )
        await accept_context(post, "beerius")
        return post.id

    send.echo = echo
    return send


async def test_logs_a_beer_and_answers_stats(chat, pg):
    assert await chat("+1 beer 🍺")
    assert await pg.fetchval("SELECT SUM(quantity) FROM beers WHERE drink_type='beer'") == 1
    # Spend tracking sees the real API's usage: the system prompt alone is thousands of tokens.
    calls, tokens = await pg.fetchrow(
        "SELECT calls, input_tokens FROM model_usage WHERE source = 'beerius'"
    )
    assert calls >= 1 and tokens > 1000
    assert await chat("how many beers have I had?")


async def test_logs_zyn_pouches_without_touching_drinks(chat, pg):
    assert await chat("+2 zyn")
    assert await pg.fetchval("SELECT COALESCE(SUM(quantity),0) FROM zyn_logs") == 2
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0


async def test_answers_questions_it_has_no_dedicated_tool_for(chat, pg):
    # On long histories these ran out of tool rounds in production and were silently dropped.
    await chat("+2 beers")
    await pg.execute("""INSERT INTO beers(user_id, group_id, quantity, drink_type, logged_at)
        SELECT id, 'g1', 2, 'beer', now() - make_interval(days => d)
        FROM users, generate_series(4, 300, 4) d""")
    assert await chat("Beerius, when did I log my first drink, what date?")
    assert await chat("Beerius what is my average daily consumption since your inception?")


async def test_owner_commands_override_house_rules(chat, pg, monkeypatch):
    # Beerius refused this exact ask ("No retroactive bulk dumps") until the owner rule.
    from src.beerbot.agent import settings

    monkeypatch.setattr(settings, "owner_user_id", "alice")
    text = "+25 from last weekend, half beer half rbv, for @Jay"
    mention = {"type": "mentions", "user_ids": ["jay"], "loci": [[text.index("@Jay"), 4]]}
    assert await chat(text, attachments=[mention])
    totals = dict(
        await pg.fetch("""SELECT b.drink_type, SUM(b.quantity) FROM beers b
            JOIN users u ON u.id = b.user_id WHERE u.groupme_user_id = 'jay' GROUP BY 1""")
    )
    assert sum(totals.values()) == 25 and set(totals) == {"beer", "cocktail"}


def tools_called(rows) -> list[str]:
    return [tool["name"] for row in rows for tool in json.loads(row["tool_results"] or "[]")]


async def test_beerdev_answers_spend_and_health_from_live_status(chat, pg):
    reply = await chat("Beerdev what's our most recent monthly spend")
    rows = await pg.fetch("SELECT agent, tool_results FROM message_inbox ORDER BY id DESC LIMIT 1")
    assert rows[0]["agent"] == "beerdev" and "app_status" in tools_called(rows)
    assert reply and "$" in reply, reply  # Answered, not filed for a human.


async def test_beerdev_files_requests_and_only_the_owner_steers(chat, pg, monkeypatch):
    from src.beerbot.agent import settings

    monkeypatch.setattr(settings, "owner_user_id", "boss")
    writes = []

    def respond(request):
        if request.method == "GET" and request.url.path.endswith("/issues"):
            gym = {
                "number": 31,
                "title": "Track gym sessions",
                "state": "open",
                "body": "",
                "labels": [{"name": "beerdev"}],
            }
            label = request.url.params["labels"]
            return httpx.Response(200, json=[gym] if label == "beerdev" else [])
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "number": 31,
                    "title": "Track gym sessions",
                    "state": "open",
                    "labels": [{"name": "beerdev"}],
                },
            )
        writes.append((request.method, request.url.path))
        return httpx.Response(201, json={"number": 77})

    monkeypatch.setattr(settings, "github_token", "t")
    monkeypatch.setattr(beerdev, "dispatch", lambda workflow, every: True)
    monkeypatch.setattr(
        beerdev,
        "_github",
        lambda: httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(respond)
        ),
    )

    assert await chat("beerdev can you make the bot track how long we nap?")
    assert ("POST", "/repos/alexeldeib/beerbot/issues") in writes  # Filed, not just answered.
    writes.clear()
    await chat("beerdev veto the gym tracker", sender="pal")
    assert writes == []  # Not the owner: refused in code.
    await chat("beerdev veto the gym tracker", sender="boss")
    assert ("PATCH", "/repos/alexeldeib/beerbot/issues/31") in writes


async def test_a_correction_replying_to_beerius_fixes_the_drinker(chat, pg):
    # Replying to Beerius's confirmation used to correct the sender instead of the drinker.
    post = await chat.echo(await chat("+1 cocktail", sender="jerry"))
    thread = {"type": "reply", "reply_id": post, "base_reply_id": post}
    await chat("that was a wine not a cocktail", sender="bob", attachments=[thread])
    drinks = await pg.fetch("""SELECT u.groupme_user_id, b.drink_type, SUM(b.quantity)
        FROM beers b JOIN users u ON u.id = b.user_id GROUP BY 1, 2 HAVING SUM(b.quantity) > 0""")
    assert {tuple(row) for row in drinks} == {("jerry", "wine", 1)}


async def test_beerius_points_capability_asks_to_beerdev(chat, pg):
    reply = await chat("Beerius can you track our gym sessions too?")
    assert reply and "beerdev" in reply.lower(), reply
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0
