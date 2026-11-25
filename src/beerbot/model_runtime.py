"""Explicit, bounded tool execution with provider-owned conversation state."""

import base64
import inspect
import json
from dataclasses import dataclass
from typing import Callable, Protocol, get_args, get_type_hints

from google.genai import types
from openai import AsyncOpenAI
from pydantic import ConfigDict, ValidationError, create_model

from . import usage


class ModelProtocolError(RuntimeError):
    pass


class ToolBudgetExceeded(RuntimeError):
    pass


# Sent with the forced last turn; without it, models answer the force with an empty call.
FINAL_TURN_NOTE = (
    "Out of lookups. Call {tool} now with your best answer from the results so far, "
    "and say briefly if something could not be determined."
)


@dataclass(frozen=True)
class Media:
    data: bytes
    mime_type: str


@dataclass(frozen=True)
class Call:
    id: str
    name: str
    arguments: dict


@dataclass
class Turn:
    calls: list[Call]
    text: str = ""
    native: object = None


class ModelSession(Protocol):
    async def next_turn(self, only: str | None = None) -> Turn: ...
    def add_results(self, turn: Turn, results: list[dict]) -> None: ...


def argument_model(tool: Callable):
    signature = inspect.signature(tool)
    hints = get_type_hints(tool)
    fields = {
        name: (hints[name], ... if p.default is inspect.Parameter.empty else p.default)
        for name, p in signature.parameters.items()
    }
    return create_model(
        tool.__name__ + "Arguments", __config__=ConfigDict(extra="forbid"), **fields
    )


def validate_arguments(validator, arguments: dict) -> dict:
    arguments = dict(arguments)
    for name, field in validator.model_fields.items():
        value = arguments.get(name)
        accepts_int = int in (field.annotation, *get_args(field.annotation))  # int | None too
        if accepts_int and isinstance(value, float) and value.is_integer():
            arguments[name] = int(value)
    return validator.model_validate(arguments, strict=True).model_dump()


async def run_tools(
    session: ModelSession,
    tools: list[Callable],
    *,
    max_rounds: int,
    max_calls: int = 20,
    terminal_tool: str | None = None,
) -> str:
    registry = {tool.__name__: tool for tool in tools}
    validators = {name: argument_model(tool) for name, tool in registry.items()}
    count = 0
    seen_ids = set()
    for _ in range(max_rounds):
        turn = await session.next_turn()
        if not turn.calls:
            return turn.text
        if count + len(turn.calls) > max_calls:
            break
        # Validate the entire batch before any tool in it mutates the database.
        validated, errors = [], []
        for call in turn.calls:
            if call.name not in registry or call.id in seen_ids:
                raise ModelProtocolError("Unknown tool or repeated call ID")
            seen_ids.add(call.id)
            try:
                validated.append(validate_arguments(validators[call.name], call.arguments))
            except ValidationError as exc:
                errors += [
                    f"{call.name}.{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
                ]
        if errors:
            # Run nothing from an invalid batch, but tell the model why so it can correct itself.
            error = {"error": "Invalid arguments; no call in this batch ran: " + "; ".join(errors)}
            results = [error] * len(turn.calls)
        else:
            results = [
                await registry[call.name](**arguments)
                for call, arguments in zip(turn.calls, validated)
            ]
        count += len(turn.calls)
        session.add_results(turn, results)
        if not errors and terminal_tool and any(c.name == terminal_tool for c in turn.calls):
            return ""
    if terminal_tool:
        # Out of budget: one last turn that may only reply, so a sender is never silently ignored.
        turn = await session.next_turn(only=terminal_tool)
        for call in turn.calls:
            if call.name == terminal_tool:
                await registry[call.name](
                    **validate_arguments(validators[call.name], call.arguments)
                )
                return ""
    # The surrounding message transaction rolls back, including all tool writes.
    raise ToolBudgetExceeded("Model round budget exhausted")


class GoogleSession:
    def __init__(
        self,
        client,
        model: str,
        system: str,
        contents: list,
        tools: list[Callable],
        source: str = "beerius",
    ):
        self.client, self.model, self.source = client, model, source
        parts = [
            types.Part.from_text(text=p)
            if isinstance(p, str)
            else types.Part.from_bytes(data=p.data, mime_type=p.mime_type)
            for p in contents
        ]
        self.contents = [types.Content(role="user", parts=parts)]
        self.config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration.from_callable(client=client, callable=tool)
                        for tool in tools
                    ]
                )
            ]
            if tools
            else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        self.round = 0

    async def next_turn(self, only: str | None = None) -> Turn:
        config = self.config
        if only:  # "ANY" mode restricted to one function forces exactly that call.
            forced = types.FunctionCallingConfig(mode="ANY", allowed_function_names=[only])
            config = config.model_copy(
                update={"tool_config": types.ToolConfig(function_calling_config=forced)}
            )
            # The last content is always the user turn (prompt or tool results); extend it so
            # the conversation keeps alternating roles.
            self.contents[-1].parts.append(
                types.Part.from_text(text=FINAL_TURN_NOTE.format(tool=only))
            )
        response = await self.client.aio.models.generate_content(
            model=self.model, contents=self.contents, config=config
        )
        usage.record(self.source, self.model, getattr(response, "usage_metadata", None))
        self.round += 1
        candidates = response.candidates
        if not isinstance(candidates, list) or not candidates:
            raise ModelProtocolError("Google returned no candidate")
        candidate = candidates[0]
        if str(candidate.finish_reason) not in ("FinishReason.STOP", "STOP"):
            raise ModelProtocolError("Google response did not complete")
        native = candidate.content
        if native is None:
            raise ModelProtocolError("Google returned no content")
        calls, text = [], []
        for i, part in enumerate(native.parts or []):
            if part.function_call:
                fc = part.function_call
                calls.append(
                    Call(fc.id or f"google-{self.round}-{i}", fc.name, dict(fc.args or {}))
                )
            elif part.text and not part.thought:
                text.append(part.text)
        return Turn(calls, "".join(text), native)

    def add_results(self, turn: Turn, results: list[dict]) -> None:
        # Preserve the entire model Content, including thought signatures.
        self.contents.append(turn.native)
        parts = []
        native_calls = [p.function_call for p in turn.native.parts if p.function_call]
        for call, native, result in zip(turn.calls, native_calls, results):
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=call.name, id=native.id, response=result
                    )
                )
            )
        self.contents.append(types.Content(role="user", parts=parts))


class CompatibleSession:
    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        system: str,
        contents: list,
        tools: list[Callable],
        *,
        video_format: str = "disabled",
        source: str = "beerius",
    ):
        self.client, self.model, self.source = client, model, source
        parts = []
        for part in contents:
            if isinstance(part, str):
                parts.append({"type": "text", "text": part})
                continue
            url = "data:" + part.mime_type + ";base64," + base64.b64encode(part.data).decode()
            if part.mime_type.startswith("image/"):
                parts.append({"type": "image_url", "image_url": {"url": url}})
            elif part.mime_type.startswith("video/") and video_format == "video_url":
                parts.append({"type": "video_url", "video_url": {"url": url}})
            else:
                raise ModelProtocolError("Endpoint does not support this media format")
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": parts}]
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.__name__,
                    "description": inspect.getdoc(tool) or "",
                    "parameters": argument_model(tool).model_json_schema(),
                },
            }
            for tool in tools
        ]

    async def next_turn(self, only: str | None = None) -> Turn:
        kwargs = {"model": self.model, "messages": self.messages}
        if self.tools:
            kwargs["tools"] = self.tools
        if only:
            self.messages.append({"role": "user", "content": FINAL_TURN_NOTE.format(tool=only)})
            kwargs["tool_choice"] = {"type": "function", "function": {"name": only}}
        response = await self.client.chat.completions.create(**kwargs)
        usage.record(self.source, self.model, getattr(response, "usage", None))
        if not response.choices:
            raise ModelProtocolError("Endpoint returned no choice")
        choice = response.choices[0]
        if choice.finish_reason not in ("stop", "tool_calls") or choice.message.refusal:
            raise ModelProtocolError("Endpoint response did not complete")
        native = choice.message.model_dump(exclude_none=True)
        calls = []
        for call in choice.message.tool_calls or []:
            if call.type != "function" or not call.id:
                raise ModelProtocolError("Invalid function call")
            arguments = json.loads(call.function.arguments)
            if not isinstance(arguments, dict):
                raise ModelProtocolError("Tool arguments must be an object")
            calls.append(Call(call.id, call.function.name, arguments))
        return Turn(calls, choice.message.content or "", native)

    def add_results(self, turn: Turn, results: list[dict]) -> None:
        # Preserve extra reasoning fields returned by compatible servers.
        self.messages.append(turn.native)
        for call, result in zip(turn.calls, results):
            self.messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
            )
