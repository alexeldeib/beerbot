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

    async def send(text: str, sender: str = "alice", attachments: tuple = ()) -> str | None:
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
                attachments=list(attachments),
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


async def test_hands_feature_requests_and_questions_to_beerdev(chat, pg):
    assert await chat("someone should make this bot track our gym sessions") is None
    assert await chat("beerdev what features are you building right now?") is None
    asks = await pg.fetch("SELECT kind, title FROM dev_requests ORDER BY id")
    assert [ask["kind"] for ask in asks] == ["feature", "question"], asks
    assert "gym" in asks[0]["title"].lower()
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0


async def test_only_the_owner_can_veto_beerdev_work(chat, pg, monkeypatch):
    from src.beerbot.agent import settings
    from src.beerbot.delivery import accept_context

    monkeypatch.setattr(settings, "owner_user_id", "boss")
    line = (
        "💡 Queued: Track gym sessions (https://github.com/o/r/issues/31). I'll post when it ships."
    )
    await accept_context(
        GroupMeMessage(
            id="dev-1",
            group_id="g1",
            user_id="b1",
            sender_id="b1",
            sender_type="bot",
            name="beerdev",
            text=line,
            created_at=0,
        )
    )
    reply = {"type": "reply", "reply_id": "dev-1", "base_reply_id": "dev-1"}
    await chat("veto", sender="pal", attachments=[reply])
    assert not (asks := await pg.fetch("SELECT kind, title FROM dev_requests")), asks
    # Push beerdev's line out of Beerius's history window: the owner's reply still finds it.
    for i in range(10):
        filler = GroupMeMessage(
            id=f"filler-{i}",
            group_id="g1",
            user_id="pal",
            sender_id="pal",
            sender_type="user",
            name="Pal",
            text="lol",
            created_at=0,
        )
        await pg.execute(
            """INSERT INTO message_inbox(group_id,message_id,payload,state)
               VALUES('g1',$1,$2::jsonb,'completed')""",
            filler.id,
            filler.model_dump_json(),
        )
    assert await chat("veto", sender="boss", attachments=[reply]) is None
    assert await chat("beerdev veto the gym tracker", sender="boss") is None
    asks = [tuple(r) for r in await pg.fetch("SELECT kind, issue, title FROM dev_requests")]
    assert asks[0] == ("veto", 31, "") and asks[1][:2] == ("veto", None), asks
    assert "gym" in asks[1][2], asks  # Only the owner's own words name the request.


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
