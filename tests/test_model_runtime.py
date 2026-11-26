import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from google.genai import types
from openai import AsyncOpenAI

from src.beerbot.model_runtime import (
    Call,
    Turn,
    Media,
    GoogleSession,
    CompatibleSession,
    ModelProtocolError,
    ToolBudgetExceeded,
    run_tools,
)


async def count_drinks(count: int, drink_type: str = "beer") -> dict:
    """Count a synthetic drink."""
    return {"count": count, "drink_type": drink_type}


async def test_google_preserves_native_thought_signatures_and_disables_afc():
    native = types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(name="count_drinks", args={"count": 1}, id="fc1"),
                thought_signature=b"opaque-signature",
            )
        ],
    )
    responses = [
        types.GenerateContentResponse(
            candidates=[types.Candidate(content=native, finish_reason="STOP")]
        ),
        types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(role="model", parts=[types.Part(text="Done")]),
                    finish_reason="STOP",
                )
            ]
        ),
    ]
    from google import genai

    client = genai.Client(api_key="synthetic")
    client.aio.models.generate_content = AsyncMock(side_effect=responses)
    session = GoogleSession(
        client, "test-model", "system", ["hello", Media(b"img", "image/png")], [count_drinks]
    )
    assert await run_tools(session, [count_drinks], max_rounds=3) == "Done"
    assert session.contents[1].parts[0].thought_signature == b"opaque-signature"
    assert session.contents[2].parts[0].function_response.id == "fc1"
    assert session.config.automatic_function_calling.disable is True
    assert session.contents[0].parts[1].inline_data.data == b"img"


async def test_compatible_http_tools_images_and_reasoning_round_trip():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        message = (
            {
                "role": "assistant",
                "reasoning_content": "opaque reasoning",
                "tool_calls": [
                    {
                        "id": "call1",
                        "type": "function",
                        "function": {"name": "count_drinks", "arguments": '{"count":2}'},
                    }
                ],
            }
            if len(requests) == 1
            else {"role": "assistant", "content": "Done"}
        )
        return httpx.Response(
            200,
            json={
                "id": "r",
                "object": "chat.completion",
                "created": 0,
                "model": "self-hosted",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                        "message": message,
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AsyncOpenAI(
            api_key="test", base_url="http://compatible.test/v1", http_client=http, max_retries=0
        )
        session = CompatibleSession(
            client, "self-hosted", "system", ["hello", Media(b"png", "image/png")], [count_drinks]
        )
        assert await run_tools(session, [count_drinks], max_rounds=3) == "Done"
    assert requests[0]["model"] == "self-hosted"
    assert requests[0]["messages"][1]["content"][1]["type"] == "image_url"
    assert requests[1]["messages"][2]["reasoning_content"] == "opaque reasoning"
    assert requests[1]["messages"][3]["tool_call_id"] == "call1"
    assert json.loads(requests[1]["messages"][3]["content"])["count"] == 2


async def test_protocol_violations_execute_no_tools():
    executed = []

    async def count_drinks(count: int) -> dict:
        executed.append(count)
        return {}

    calls = [Call("1", "count_drinks", {"count": 1}), Call("1", "count_drinks", {"count": 1})]
    session = MagicMock()
    session.next_turn = AsyncMock(return_value=Turn(calls))
    with pytest.raises(ModelProtocolError):
        await run_tools(session, [count_drinks], max_rounds=2)
    assert executed == []


async def test_an_unknown_tool_goes_back_to_the_model_and_runs_nothing():
    executed = []

    async def count_drinks(count: int) -> dict:
        executed.append(count)
        return {}

    session = MagicMock()
    session.next_turn = AsyncMock(
        side_effect=[
            Turn([Call("1", "count_drinks", {"count": 1}), Call("2", "log_gym", {})]),
            Turn([], "Logged it."),
        ]
    )
    assert await run_tools(session, [count_drinks], max_rounds=3) == "Logged it."
    assert executed == []  # Nothing from the batch ran, as with bad arguments.
    first_results = session.add_results.call_args_list[0].args[1]
    assert all("log_gym: no such tool" in result["error"] for result in first_results)


@pytest.mark.parametrize(
    "arguments,problem",
    [({"count": True}, "count_drinks.count"), ({"count": 1, "tenant": "x"}, "count_drinks.tenant")],
)
async def test_invalid_arguments_run_nothing_and_go_back_to_the_model(arguments, problem):
    executed = []

    async def count_drinks(count: int) -> dict:
        executed.append(count)
        return {}

    async def reply(message: str) -> dict:
        executed.append(message)
        return {}

    session = MagicMock()
    session.next_turn = AsyncMock(
        side_effect=[
            Turn([Call("1", "count_drinks", arguments), Call("2", "reply", {"message": "a"})]),
            Turn([Call("3", "reply", {"message": "fixed"})]),
        ]
    )
    tools = [count_drinks, reply]
    assert await run_tools(session, tools, max_rounds=3, terminal_tool="reply") == ""
    assert executed == ["fixed"]  # Nothing from the invalid batch ran, including its reply.
    first_results = session.add_results.call_args_list[0].args[1]
    assert len(first_results) == 2 and all(problem in r["error"] for r in first_results)


async def test_budget_and_terminal_reply():
    async def reply(message: str) -> dict:
        return {"queued": message}

    session = MagicMock()
    session.next_turn = AsyncMock(return_value=Turn([Call("1", "reply", {"message": "hello"})]))
    assert await run_tools(session, [reply], max_rounds=1, terminal_tool="reply") == ""
    with pytest.raises(ToolBudgetExceeded):
        await run_tools(session, [reply], max_rounds=1)
    with pytest.raises(ToolBudgetExceeded):
        await run_tools(session, [reply], max_rounds=1, max_calls=0)


@pytest.mark.parametrize("max_rounds,max_calls", [(2, 20), (5, 1)])
async def test_exhausted_budget_gets_one_reply_only_turn(max_rounds, max_calls):
    replies = []

    async def lookup() -> dict:
        return {}

    async def reply(message: str) -> dict:
        replies.append(message)
        return {}

    session = MagicMock()
    session.next_turn = AsyncMock(
        side_effect=[
            Turn([Call("1", "lookup", {})]),
            Turn([Call("2", "lookup", {}), Call("3", "lookup", {})]),
            Turn([Call("4", "reply", {"message": "Best I can do"})]),
        ]
    )
    tools = [lookup, reply]
    kwargs = {"max_rounds": max_rounds, "max_calls": max_calls, "terminal_tool": "reply"}
    assert await run_tools(session, tools, **kwargs) == ""
    assert replies == ["Best I can do"]
    assert session.next_turn.call_args_list[-1].kwargs == {"only": "reply"}

    lookups = [Turn([Call(str(i), "lookup", {})]) for i in (5, 6)]
    session.next_turn = AsyncMock(side_effect=lookups + [Turn([], "no tool call")])
    with pytest.raises(ToolBudgetExceeded):
        await run_tools(session, tools, max_rounds=2, terminal_tool="reply")


async def test_forced_reply_reaches_both_providers():
    from google import genai

    client = genai.Client(api_key="synthetic")
    done = types.Content(role="model", parts=[types.Part(text="ok")])
    client.aio.models.generate_content = AsyncMock(
        return_value=types.GenerateContentResponse(
            candidates=[types.Candidate(content=done, finish_reason="STOP")]
        )
    )
    google = GoogleSession(client, "m", "s", ["hi"], [count_drinks])
    await google.next_turn(only="count_drinks")
    forced = client.aio.models.generate_content.call_args.kwargs["config"].tool_config
    assert forced.function_calling_config.mode == "ANY"
    assert forced.function_calling_config.allowed_function_names == ["count_drinks"]
    assert "Out of lookups" in google.contents[-1].parts[-1].text
    assert google.contents[-1].role == "user" and len(google.contents) == 1

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        message = {"role": "assistant", "content": "ok"}
        choice = {"index": 0, "finish_reason": "stop", "message": message}
        body = {"id": "1", "object": "chat.completion", "created": 0, "model": "m"}
        return httpx.Response(200, json=body | {"choices": [choice]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        compatible = AsyncOpenAI(api_key="t", base_url="http://c.test/v1", http_client=http)
        await CompatibleSession(compatible, "m", "s", ["hi"], [count_drinks]).next_turn(
            only="count_drinks"
        )
    assert requests[0]["tool_choice"] == {"type": "function", "function": {"name": "count_drinks"}}
    assert "Out of lookups" in requests[0]["messages"][-1]["content"]


def test_video_is_explicit_opt_in():
    with pytest.raises(ModelProtocolError):
        CompatibleSession(None, "m", "s", [Media(b"video", "video/mp4")], [])
    session = CompatibleSession(
        None, "m", "s", [Media(b"video", "video/mp4")], [], video_format="video_url"
    )
    assert session.messages[1]["content"][0]["type"] == "video_url"


@pytest.mark.parametrize("finish", ["length", "content_filter"])
async def test_truncated_compatible_response_cannot_finish(finish):
    from openai.types.chat import ChatCompletion

    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=ChatCompletion.model_validate(
            {
                "id": "r",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish,
                        "message": {"role": "assistant", "content": "partial"},
                    }
                ],
            }
        )
    )
    with pytest.raises(ModelProtocolError):
        await CompatibleSession(client, "m", "s", ["hi"], []).next_turn()
