"""Best-effort GitHub dispatches: off without a token, debounced, shaped for the API."""

import asyncio
import json

import httpx
import pytest

from src.beerbot import dispatch as module


def test_only_beerdevs_token_is_read(monkeypatch):
    from src.beerbot.config import Settings

    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "retired alias")
    monkeypatch.setenv("GITHUB_TOKEN", "a local shell's")
    monkeypatch.delenv("BEERDEV_GITHUB_TOKEN", raising=False)
    assert Settings(_env_file=None).github_token is None
    monkeypatch.setenv("BEERDEV_GITHUB_TOKEN", "t")
    assert Settings(_env_file=None).github_token == "t"


async def test_dispatch_is_off_without_a_token(monkeypatch):
    monkeypatch.setattr(module.settings, "github_token", None)
    assert not module.dispatch("ops.yml", every=60, reason="ValueError")


async def test_dispatch_debounces_per_workflow_and_inputs(monkeypatch):
    sent = []

    def handler(request):
        auth = request.headers["authorization"]
        sent.append((request.url.path, auth, json.loads(request.content)))
        return httpx.Response(204)

    real = httpx.AsyncClient
    fake = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)  # noqa: E731
    monkeypatch.setattr(module.httpx, "AsyncClient", fake)
    monkeypatch.setattr(module.settings, "github_token", "t")
    monkeypatch.setattr(module, "_last", {})
    assert module.dispatch("ops.yml", every=60, reason="ValueError")
    assert not module.dispatch("ops.yml", every=60, reason="ValueError")
    assert module.dispatch("ops.yml", every=60, reason="TypeError")
    await asyncio.gather(*list(module._tasks))
    path = "/repos/alexeldeib/beerbot/actions/workflows/ops.yml/dispatches"
    assert sent[0] == (path, "Bearer t", {"ref": "main", "inputs": {"reason": "ValueError"}})
    assert len(sent) == 2


@pytest.mark.parametrize(
    ("state", "calls"),
    [
        ("disabled_inactivity", ["GET", "PUT enable", "POST dispatches"]),  # GitHub turned it off
        ("active", ["GET", "POST dispatches"]),
        ("disabled_manually", ["GET"]),  # The owner paused it: never turned back on
    ],
)
async def test_dispatch_turns_on_only_what_github_disabled(monkeypatch, state, calls):
    sent = []

    def handler(request):
        sent.append(f"{request.method} {request.url.path.rsplit('/', 1)[-1]}".strip())
        if request.method == "GET":
            return httpx.Response(200, json={"state": state})
        return httpx.Response(204)

    real = httpx.AsyncClient
    fake = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)  # noqa: E731
    monkeypatch.setattr(module.httpx, "AsyncClient", fake)
    monkeypatch.setattr(module.settings, "github_token", "t")
    monkeypatch.setattr(module, "_last", {})
    assert module.dispatch("ops.yml", every=3600, enable=True, reason="ops_silent")
    await asyncio.gather(*list(module._tasks))
    assert [call.replace("GET ops.yml", "GET") for call in sent] == calls
