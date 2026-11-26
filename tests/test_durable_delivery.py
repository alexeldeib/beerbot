"""Failure-boundary tests using real PostgreSQL and the existing domain tools."""

import asyncio
import httpx
from unittest.mock import AsyncMock

import pytest

from src.beerbot import database, delivery
from src.beerbot.agent import BeerAgent
from src.beerbot.delivery import accept_message, execute_one, deliver_one, maintain_queue
from src.beerbot.delivery import queue_status, retry_delivery
from src.beerbot.groupme_client import DeliveryResult
from src.beerbot.models import GroupMeAttachment, GroupMeMessage
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

    async def process(msg, history):
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

    async def process(msg, history):
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

    async def interrupted(msg, history):
        tools = {t.__name__: t for t in create_tools(context(msg))}
        await tools["undo_last_drink"]()
        raise asyncio.CancelledError()

    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", interrupted)
    await accept_message(message(text="undo"))
    with pytest.raises(asyncio.CancelledError):
        await execute_one(agent)
    assert await pg.fetchval("SELECT SUM(quantity) FROM beers") == 3
    assert await pg.fetchval("SELECT attempts FROM message_inbox") == 1  # Counted at the claim.

    async def recovered(msg, history):
        tools = {t.__name__: t for t in create_tools(context(msg))}
        await tools["undo_last_drink"]()
        return "Undone"

    replacement = BeerAgent()
    monkeypatch.setattr(replacement, "process_message", recovered)
    assert not await execute_one(replacement)  # Leased for the retry backoff.
    await pg.execute("UPDATE message_inbox SET available_at=NOW()")
    assert await execute_one(replacement)
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0
    assert not await execute_one(replacement)


async def test_a_message_that_kills_the_process_stops_blocking_its_group(pg, monkeypatch):
    # Process death rolls the execution back, so only an attempt committed at the claim bounds a
    # message that runs out of memory every time.
    await setup(pg)
    reports = []
    monkeypatch.setattr(delivery, "dispatch", lambda workflow, **kw: reports.append(kw["reason"]))
    monkeypatch.setattr(delivery, "UNPROCESSED", {})
    agent = BeerAgent()

    async def process(msg, history):
        if msg.id == "poison":
            raise asyncio.CancelledError  # To Postgres, the same as the process dying.
        return None

    monkeypatch.setattr(agent, "process_message", process)
    await accept_message(message("poison"))
    await accept_message(message("next"))
    for attempt in range(1, delivery.MAX_EXECUTION_ATTEMPTS + 1):
        with pytest.raises(asyncio.CancelledError):
            await execute_one(agent)
        counted = await pg.fetchval("SELECT attempts FROM message_inbox WHERE message_id='poison'")
        assert counted == attempt
        assert not await execute_one(agent)  # Leased, and "next" waits its turn behind it.
        await pg.execute("UPDATE message_inbox SET available_at=NOW()")
    assert await execute_one(agent)  # Out of attempts: failed instead of run a fourth time.
    poison = await pg.fetchrow(
        "SELECT state,error_code,error_detail FROM message_inbox WHERE message_id='poison'"
    )
    assert (poison["state"], poison["error_code"]) == ("failed", "interrupted")
    assert "3 times" in poison["error_detail"] and reports == ["interrupted"]
    assert await execute_one(agent)
    assert await pg.fetchval("SELECT state FROM message_inbox WHERE message_id='next'") == (
        "completed"
    )
    assert delivery.UNPROCESSED == {}


async def test_parallel_workers_serialize_group_and_allow_other_group(pg, monkeypatch):
    await setup(pg)
    for msg in [message("first"), message("second"), message("other", "g2")]:
        await accept_message(msg)
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def process(msg, history):
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
    from collections import Counter

    reports = []
    monkeypatch.setattr(delivery, "dispatch", lambda workflow, **kw: reports.append((workflow, kw)))
    monkeypatch.setattr(delivery, "TERMINAL_FAILURES", Counter())
    agent = BeerAgent()
    failing = AsyncMock(side_effect=ValueError("chat text"))
    monkeypatch.setattr(agent, "process_message", failing)
    await accept_message(message())
    for _ in range(3):
        await pg.execute("UPDATE message_inbox SET available_at=NOW()")
        assert await execute_one(agent)
    assert await pg.fetchval("SELECT state FROM message_inbox") == "failed"
    assert not await execute_one(agent)
    # Ops gets a publishable summary: type and this repo's frames, never the message.
    detail = await pg.fetchval("SELECT error_detail FROM message_inbox")
    assert detail.startswith("ValueError at delivery.py:") and "chat text" not in detail
    assert (await queue_status())["failed_executions"][0]["error_detail"] == detail
    assert delivery.TERMINAL_FAILURES == {"ValueError": 1}
    assert reports == [("ops.yml", {"every": 600, "reason": "ValueError"})]


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

    async def process(msg, history):
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


async def test_bad_tool_input_undoes_only_that_tool_and_still_replies(pg, monkeypatch):
    await setup(pg)
    from src.beerbot import tools as tools_module

    async def process(msg, history):
        tools = {t.__name__: t for t in create_tools(context(msg))}
        await tools["add_debt"]("bob", 3)
        failing = AsyncMock(side_effect=ValueError("bad drink"))
        monkeypatch.setattr(tools_module.beer_repo, "get_user_total_by_type", failing)
        assert await tools["log_drinks"](2, "beer") == {"error": "bad drink"}
        return "Debt added; say the drink again"

    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", process)
    await accept_message(message())
    assert await execute_one(agent)
    assert await pg.fetchval("SELECT state FROM message_inbox") == "completed"
    assert await pg.fetchval("SELECT SUM(amount) FROM user_debts") == 3
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0  # Inserted, then savepoint undone.
    assert await pg.fetchval("SELECT COUNT(*) FROM message_outbox") == 1


async def test_execution_deadline_rolls_back_mutations(pg, monkeypatch):
    await setup(pg)
    monkeypatch.setattr("src.beerbot.delivery.EXECUTION_TIMEOUT", 0.2)

    async def process(msg, history):
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


async def test_history_is_the_stored_chat_before_the_message(pg, monkeypatch):
    # Read from the inbox for every message: it survives restarts, never outlives retention,
    # and never includes the message itself or what came after it.
    await setup(pg)
    await accept_message(message("expired", text="old news"))
    await pg.execute("UPDATE message_inbox SET payload=NULL, state='completed'")  # Retention.
    first = BeerAgent()
    monkeypatch.setattr(first, "process_message", AsyncMock(return_value="+1🍺 for Alice"))
    await accept_message(message())
    await execute_one(first)
    echo = message("echo", text="+1🍺 for Alice").model_copy(
        update={"sender_type": "bot", "name": "beerius"}
    )
    await delivery.accept_context(echo, "beerius")  # GroupMe echoes the delivered reply.
    for id, text in (("current", "how many?"), ("later", "+1 wine")):
        await accept_message(message(id, text=text))
    seen = {}

    async def process(msg, history):
        seen[msg.id] = [(m.name, m.text) for m in history]
        return None

    replacement = BeerAgent()
    monkeypatch.setattr(replacement, "process_message", process)
    assert await execute_one(replacement)
    assert seen == {"current": [("Alice", "+1 beer"), ("beerius", "+1🍺 for Alice")]}


def google_turn(*calls):
    """One model turn calling these (name, arguments) tools."""
    from google.genai import types

    parts = [types.Part(function_call=types.FunctionCall(name=n, args=a)) for n, a in calls]
    content = types.Content(role="model", parts=parts)
    return types.GenerateContentResponse(
        candidates=[types.Candidate(finish_reason="STOP", content=content)]
    )


def person(id: str, user: str, text: str, reply_to: str | None = None) -> GroupMeMessage:
    thread = [GroupMeAttachment(type="reply", reply_id=reply_to)] if reply_to else []
    return message(id, text=text).model_copy(
        update={"user_id": user, "sender_id": user, "name": user.title(), "attachments": thread}
    )


async def test_reply_targets_resolve_by_id_and_through_beerius_posts(pg, monkeypatch):
    await setup(pg)
    first = BeerAgent()
    monkeypatch.setattr(first, "process_message", AsyncMock(return_value="+1🍸 for Jerry"))
    await accept_message(person("jerry-1", "jerry", "+1 cocktail"))
    assert await execute_one(first)
    echo = message("post-1", text="+1🍸 for Jerry").model_copy(
        update={"sender_type": "bot", "name": "beerius", "user_id": "bot"}
    )
    await delivery.accept_context(echo, "beerius")
    await pg.execute("""INSERT INTO message_inbox(group_id,message_id,payload,state)
        SELECT 'g1','filler-'||i,'{}','completed' FROM generate_series(1,12) i""")
    replied, answered = await delivery.reply_target(person("b1", "bob", "wine", "post-1"))
    assert (replied.text, answered.id) == ("+1🍸 for Jerry", "jerry-1")
    replied, answered = await delivery.reply_target(person("b2", "bob", "lol", "jerry-1"))
    assert (replied.name, answered) == ("Jerry", None)  # Older than the history window, too.
    assert await delivery.reply_target(person("b3", "bob", "hm", "gone")) == (None, None)
    assert await delivery.reply_target(person("b4", "bob", "hi")) == (None, None)


async def test_correcting_by_replying_to_beerius_fixes_the_drinker(pg, monkeypatch):
    # Bob replies to "+1🍸 for Jerry" with "that was a wine": the correction is Jerry's.
    from google import genai

    await setup(pg)
    first = BeerAgent()
    monkeypatch.setattr(first, "process_message", AsyncMock(return_value="+1🍸 for Jerry"))
    await accept_message(person("jerry-1", "jerry", "+1 cocktail"))
    assert await execute_one(first)
    jerry = await pg.fetchval(
        "INSERT INTO users(groupme_user_id,name) VALUES('jerry','Jerry') RETURNING id"
    )
    await pg.execute(
        """INSERT INTO beers(user_id,group_id,quantity,drink_type,message_id)
           VALUES($1,'g1',1,'cocktail','jerry-1')""",
        jerry,
    )
    echo = message("post-1", text="+1🍸 for Jerry").model_copy(
        update={"sender_type": "bot", "name": "beerius", "user_id": "bot"}
    )
    await delivery.accept_context(echo, "beerius")
    client = genai.Client(api_key="synthetic")
    remove = {"count": 1, "drink_type": "cocktail", "target_user_id": "jerry"}
    log = {"count": 1, "drink_type": "wine", "target_user_ids": ["jerry"]}
    client.aio.models.generate_content = AsyncMock(
        side_effect=[
            google_turn(("remove_drinks", remove), ("log_drinks", log)),
            google_turn(("reply", {"message": "Fixed: a wine for Jerry."})),
        ]
    )
    agent = BeerAgent()
    agent.client = client
    await accept_message(person("bob-1", "bob", "that was a wine not a cocktail", "post-1"))
    assert await execute_one(agent)
    drinks = await pg.fetch("""SELECT u.groupme_user_id, b.drink_type, b.quantity
        FROM beers b JOIN users u ON u.id=b.user_id""")
    assert [tuple(row) for row in drinks] == [("jerry", "wine", 1)]
    system = client.aio.models.generate_content.call_args_list[0].kwargs["config"]
    assert "Replying to [beerius]'s answer to [Jerry] (id: jerry)" in system.system_instruction


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


@pytest.mark.parametrize("finish", [True, False])
async def test_explicit_google_loop_commits_or_rolls_back_as_one_message(pg, monkeypatch, finish):
    from google import genai
    from google.genai import types

    await setup(pg)
    client = genai.Client(api_key="synthetic")

    def response(name, args):
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    finish_reason="STOP",
                    content=types.Content(
                        role="model",
                        parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
                    ),
                )
            ]
        )

    replies = [response("log_drinks", {"count": 1.0, "drink_type": "beer"})]
    if finish:
        replies.append(response("reply", {"message": "Logged"}))
    client.aio.models.generate_content = AsyncMock(side_effect=replies)
    agent = BeerAgent()
    agent.client = client
    monkeypatch.setattr("src.beerbot.agent.settings.agent_max_tool_calls", 2 if finish else 1)
    await accept_message(message())
    await execute_one(agent)
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == int(finish)
    assert await pg.fetchval("SELECT COUNT(*) FROM message_outbox") == int(finish)
    assert await pg.fetchval("SELECT state FROM message_inbox") == (
        "completed" if finish else "pending"
    )


async def test_compatible_adapter_uses_same_transactional_agent_tools(pg):
    import json
    from dataclasses import replace
    from openai import AsyncOpenAI

    await setup(pg)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        name, args = (
            ("log_drinks", {"count": 1, "drink_type": "beer"})
            if len(requests) == 1
            else ("reply", {"message": "Logged"})
        )
        return httpx.Response(
            200,
            json={
                "id": "r",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": str(len(requests)),
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    agent = BeerAgent()
    agent.model_profile = replace(agent.model_profile, provider="openai_compatible", model="test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        agent.client = AsyncOpenAI(api_key="test", base_url="http://test/v1", http_client=http)
        await accept_message(message())
        await execute_one(agent)
    assert await pg.fetchval("SELECT SUM(quantity) FROM beers") == 1
    assert await pg.fetchval("SELECT body FROM message_outbox") == "Logged"
    assert len(requests) == 2
