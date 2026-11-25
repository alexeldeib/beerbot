"""Best-effort GitHub dispatches: off without a token, debounced, shaped for the API."""

import asyncio
import json

import httpx

from src.beerbot import dispatch as module


async def test_dispatch_is_off_without_a_token(monkeypatch):
    monkeypatch.setattr(module.settings, "github_dispatch_token", None)
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
    monkeypatch.setattr(module.settings, "github_dispatch_token", "t")
    monkeypatch.setattr(module, "_last", {})
    assert module.dispatch("ops.yml", every=60, reason="ValueError")
    assert not module.dispatch("ops.yml", every=60, reason="ValueError")
    assert module.dispatch("ops.yml", every=60, reason="TypeError")
    await asyncio.gather(*list(module._tasks))
    path = "/repos/alexeldeib/beerbot/actions/workflows/ops.yml/dispatches"
    assert sent[0] == (path, "Bearer t", {"ref": "main", "inputs": {"reason": "ValueError"}})
    assert len(sent) == 2
