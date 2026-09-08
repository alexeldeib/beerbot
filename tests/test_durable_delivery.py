"""Failure-boundary tests using real PostgreSQL and the existing domain tools."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.beerbot import database
from src.beerbot.agent import BeerAgent
from src.beerbot.delivery import accept_message, execute_one, deliver_one, maintain_queue
from src.beerbot.delivery import retry_delivery
from src.beerbot.groupme_client import DeliveryResult
from src.beerbot.models import GroupMeMessage
from src.beerbot.repositories import debt_repo
from src.beerbot.tools import ToolContext, create_tools


def message(id="message-1", group="g1", text="+1 beer"):
    return GroupMeMessage(
        id=id,
        group_id=group,
        user_id="alice",
        sender_id="alice",
        sender_type="user",
        name="Alice",
        text=text,
        created_at=1788730000,
    )


def context(msg):
    return ToolContext(
        group_id=msg.group_id,
        message_id=msg.id,
        sender_id=msg.user_id,
        sender_name=msg.name,
        sender_avatar_url=None,
        mentioned_users=[("bob", "Bob")],
    )


async def setup(pg):
    await database.init_db()
    await pg.execute("INSERT INTO groups(group_id,bot_id) VALUES('g1','fake'),('g2','fake')")


async def test_duplicate_receipt_and_delivery_retry_never_repeat_mutations(pg, monkeypatch):
    await setup(pg)
    calls = 0

    async def process(msg):
        nonlocal calls
        calls += 1
        tools = {t.__name__: t for t in create_tools(context(msg))}
        await tools["log_drinks"](2, "beer")
        await tools["add_debt"]("bob", 3)
        return "Logged the round"

    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", process)
    receipts = await asyncio.gather(accept_message(message()), accept_message(message()))
    assert {r["action"] for r in receipts} == {"queued", "duplicate"}
    assert await execute_one(agent)
    assert not await execute_one(agent)
    sender = AsyncMock()
    sender.deliver_message.side_effect = [
        DeliveryResult("retry", "ConnectTimeout"),
        DeliveryResult("sent"),
    ]
    assert await deliver_one(sender)
    assert not await deliver_one(sender)  # backoff
    await pg.execute("UPDATE message_outbox SET available_at=NOW()")
    assert await deliver_one(sender)
    await accept_message(message())
    assert not await execute_one(agent)
    assert not await deliver_one(sender)
    assert calls == 1
    assert await pg.fetchval("SELECT SUM(quantity) FROM beers") == 2
    assert await pg.fetchval("SELECT SUM(amount) FROM user_debts") == 3
    assert await pg.fetchval("SELECT jsonb_array_length(tool_results) FROM message_inbox") == 2
    assert sender.deliver_message.await_args.args[0] == "Logged the round"


async def test_partial_round_failure_rolls_back_user_drinks_and_debt(pg, monkeypatch):
    await setup(pg)

    async def process(msg):
        tools = {t.__name__: t for t in create_tools(context(msg))}
        await tools["log_drinks"](2, "beer", ["alice", "bob"])
        return "Must not send"

    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", process)
    monkeypatch.setattr(debt_repo, "reduce_debt", AsyncMock(side_effect=RuntimeError("injected")))
    await accept_message(message())
    assert await execute_one(agent)
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0
    assert await pg.fetchval("SELECT COUNT(*) FROM users") == 0
    assert await pg.fetchval("SELECT COUNT(*) FROM message_outbox") == 0
    assert await pg.fetchval("SELECT attempts FROM message_inbox") == 1
    assert await pg.fetchval("SELECT state FROM message_inbox") == "pending"


async def test_cancellation_after_undo_rolls_back_and_restart_retries_once(pg, monkeypatch):
    await setup(pg)
    user = await pg.fetchval(
        "INSERT INTO users(groupme_user_id,name) VALUES('alice','Alice') RETURNING id"
    )
    await pg.execute("INSERT INTO beers(user_id,group_id,quantity) VALUES($1,'g1',3)", user)

    async def interrupted(msg):
        tools = {t.__name__: t for t in create_tools(context(msg))}
        await tools["undo_last_drink"]()
        raise asyncio.CancelledError()

    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", interrupted)
    await accept_message(message(text="undo"))
    with pytest.raises(asyncio.CancelledError):
        await execute_one(agent)
    assert await pg.fetchval("SELECT SUM(quantity) FROM beers") == 3
    assert await pg.fetchval("SELECT attempts FROM message_inbox") == 0

    async def recovered(msg):
        tools = {t.__name__: t for t in create_tools(context(msg))}
        await tools["undo_last_drink"]()
        return "Undone"

    replacement = BeerAgent()
    monkeypatch.setattr(replacement, "process_message", recovered)
    assert await execute_one(replacement)
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0
    assert not await execute_one(replacement)


async def test_parallel_workers_serialize_group_and_allow_other_group(pg, monkeypatch):
    await setup(pg)
    for msg in [message("first"), message("second"), message("other", "g2")]:
        await accept_message(msg)
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def process(msg):
        seen.append(msg.id)
        if msg.id == "first":
            started.set()
            await release.wait()
        return None

    a, b = BeerAgent(), BeerAgent()
    monkeypatch.setattr(a, "process_message", process)
    monkeypatch.setattr(b, "process_message", process)
    task = asyncio.create_task(execute_one(a))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert await execute_one(b)
        assert seen == ["first", "other"]
        assert not await execute_one(b)
    finally:
        release.set()
        await task
    assert await execute_one(b)
    assert seen == ["first", "other", "second"]


async def test_send_crash_is_uncertain_and_never_automatically_repeated(pg, monkeypatch):
    await setup(pg)
    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", AsyncMock(return_value="Reply"))
    await accept_message(message())
    await execute_one(agent)
    sender = AsyncMock()
    sender.deliver_message.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await deliver_one(sender)
    assert await pg.fetchval("SELECT state FROM message_outbox") == "sending"
    await pg.execute("UPDATE message_outbox SET updated_at=NOW()-INTERVAL '3 minutes'")
    await maintain_queue()
    assert await pg.fetchval("SELECT state FROM message_outbox") == "uncertain"
    assert not await deliver_one(sender)
    sender.deliver_message.assert_awaited_once()


async def test_silence_and_retention_keep_deduplication_tombstones(pg, monkeypatch):
    await setup(pg)
    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", AsyncMock(return_value=None))
    await accept_message(message())
    await execute_one(agent)
    assert await pg.fetchval("SELECT COUNT(*) FROM message_outbox") == 0
    await pg.execute("UPDATE message_inbox SET created_at=NOW()-INTERVAL '4 days'")
    await maintain_queue()
    assert await pg.fetchval("SELECT payload FROM message_inbox") is None
    assert (await accept_message(message()))["action"] == "duplicate"
    assert not await execute_one(agent)


async def test_failed_execution_retries_are_bounded(pg, monkeypatch):
    await setup(pg)
    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", AsyncMock(side_effect=ValueError("bad")))
    await accept_message(message())
    for _ in range(3):
        await pg.execute("UPDATE message_inbox SET available_at=NOW()")
        assert await execute_one(agent)
    assert await pg.fetchval("SELECT state FROM message_inbox") == "failed"
    assert not await execute_one(agent)


async def test_uncertain_admin_retry_only_resends_saved_reply(pg, monkeypatch):
    await setup(pg)
    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", AsyncMock(return_value="Saved reply"))
    await accept_message(message())
    await execute_one(agent)
    sender = AsyncMock()
    sender.deliver_message.side_effect = [
        DeliveryResult("uncertain", "ReadTimeout"),
        DeliveryResult("sent"),
    ]
    await deliver_one(sender)
    id = await pg.fetchval("SELECT id FROM message_outbox")
    assert not await retry_delivery(id)
    assert await retry_delivery(id, acknowledge_uncertain=True)
    await deliver_one(sender)
    assert not await execute_one(agent)
    agent.process_message.assert_awaited_once()
    assert not await retry_delivery(id, acknowledge_uncertain=True)


async def test_swallowed_tool_failure_rolls_back(pg, monkeypatch):
    await setup(pg)

    async def process(msg):
        tools = {t.__name__: t for t in create_tools(context(msg))}
        await tools["log_drinks"](1, "beer")
        try:
            await tools["add_debt"]("bob", object())
        except TypeError:
            pass  # Simulate an SDK handling the exception itself.
        return "Invalid confirmation"

    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", process)
    await accept_message(message())
    await execute_one(agent)
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0
    assert await pg.fetchval("SELECT COUNT(*) FROM message_outbox") == 0


async def test_execution_deadline_rolls_back_mutations(pg, monkeypatch):
    await setup(pg)
    monkeypatch.setattr("src.beerbot.delivery.EXECUTION_TIMEOUT", 0.2)

    async def process(msg):
        tools = {t.__name__: t for t in create_tools(context(msg))}
        await tools["log_drinks"](1, "beer")
        await asyncio.sleep(5)

    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", process)
    await accept_message(message())
    await execute_one(agent)
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0
    assert await pg.fetchval("SELECT error_code FROM message_inbox") == "TimeoutError"


async def test_group_removed_after_acceptance_cannot_mutate_or_send(pg, monkeypatch):
    await setup(pg)
    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", AsyncMock(return_value="Reply"))
    await accept_message(message())
    await pg.execute("DELETE FROM groups WHERE group_id='g1'")
    await execute_one(agent)
    agent.process_message.assert_not_called()
    assert await pg.fetchval("SELECT state FROM message_inbox") == "failed"


async def test_callback_durably_accepts_and_deduplicates(pg, monkeypatch):
    import httpx
    from src.beerbot import main

    await setup(pg)
    monkeypatch.setattr(main, "accept_message", accept_message)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as http:
        first = await http.post("/callback", json=message().model_dump())
        duplicate = await http.post("/callback", json=message().model_dump())
    assert first.status_code == duplicate.status_code == 200
    assert first.json()["action"] == "queued"
    assert duplicate.json()["action"] == "duplicate"
    assert await pg.fetchval("SELECT COUNT(*) FROM message_inbox") == 1
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0


async def test_delivery_retry_limit_and_expiration(pg, monkeypatch):
    await setup(pg)
    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", AsyncMock(return_value="Reply"))
    await accept_message(message())
    await execute_one(agent)
    sender = AsyncMock()
    sender.deliver_message.return_value = DeliveryResult("retry", "ConnectTimeout")
    for _ in range(5):
        await pg.execute("UPDATE message_outbox SET available_at=NOW()")
        assert await deliver_one(sender)
    assert not await deliver_one(sender)
    assert await pg.fetchval("SELECT state FROM message_outbox") == "failed"
    await pg.execute("UPDATE message_outbox SET created_at=NOW()-INTERVAL '4 days'")
    await maintain_queue()
    assert await pg.fetchval("SELECT body FROM message_outbox") is None
    assert not await retry_delivery(await pg.fetchval("SELECT id FROM message_outbox"), True)


async def test_completed_history_survives_new_agent(pg, monkeypatch):
    await setup(pg)
    first = BeerAgent()
    monkeypatch.setattr(first, "process_message", AsyncMock(return_value="Previous reply"))
    await accept_message(message())
    await execute_one(first)
    replacement = BeerAgent()

    async def inspect_history(msg):
        assert [m.text for m in replacement._get_history("g1")] == ["+1 beer", "Previous reply"]
        return None

    monkeypatch.setattr(replacement, "process_message", inspect_history)
    await accept_message(message("follow-up", text="how many?"))
    await execute_one(replacement)
    assert await pg.fetchval("SELECT COUNT(*) FROM message_inbox WHERE state='completed'") == 2


def test_audited_tools_preserve_google_function_schemas():
    from google import genai
    from google.genai import types

    client = genai.Client(api_key="test-key")
    plain = create_tools(context(message()))
    with database.bind_execution(None):
        wrapped = create_tools(context(message()))
    for original, audited in zip(plain, wrapped):
        expected = types.FunctionDeclaration.from_callable(client=client, callable=original)
        actual = types.FunctionDeclaration.from_callable(client=client, callable=audited)
        assert expected == actual
