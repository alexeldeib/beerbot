"""Model usage: counting responses, list-price costs, flushing while awake, and budget reports."""

from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from src.beerbot import delivery, main, usage
from src.beerbot.agent import BeerAgent
from tests.test_durable_delivery import message, setup


@pytest.fixture(autouse=True)
def fresh_counts(monkeypatch):
    monkeypatch.setattr(usage, "_pending", {})
    monkeypatch.setattr(usage, "SINCE_BOOT", {})


def test_record_counts_google_and_openai_usage_with_thinking_as_output():
    google = SimpleNamespace(
        prompt_token_count=4200,
        candidates_token_count=30,
        thoughts_token_count=300,
        cached_content_token_count=1000,
    )
    usage.record("beerius", "gemini-3.6-flash", google)
    usage.record("beerius", "gemini-3.6-flash", google)
    openai = SimpleNamespace(
        prompt_tokens=500,
        completion_tokens=40,
        prompt_tokens_details=SimpleNamespace(cached_tokens=100),
    )
    usage.record("recap", "local-model", openai)
    usage.record("beerius", "gemini-3.6-flash", None)  # Some endpoints report nothing.
    assert usage.since_boot() == {
        "gemini-3.6-flash": {
            "calls": 2,
            "input_tokens": 8400,
            "output_tokens": 660,
            "cached_tokens": 2000,
        },
        "local-model": {"calls": 1, "input_tokens": 500, "output_tokens": 40, "cached_tokens": 100},
    }


def test_costs_follow_list_prices_including_the_2027_change():
    december, january = date(2026, 12, 31), date(2027, 1, 1)
    million = 1_000_000
    assert usage.cost("gemini-3.6-flash", december, million, million, 0) == pytest.approx(4.50)
    assert usage.cost("gemini-3.6-flash", january, million, million, 0) == pytest.approx(9.00)
    # Cached input is billed at the cached rate, not on top of the input rate.
    assert usage.cost("gemini-3.6-flash", december, million, 0, million) == pytest.approx(0.075)
    assert usage.cost("self-hosted", december, million, million, 0) is None


async def test_message_tokens_are_written_even_when_the_message_fails(pg, monkeypatch):
    await setup(pg)
    agent = BeerAgent()
    meta = SimpleNamespace(
        prompt_token_count=4000,
        candidates_token_count=20,
        thoughts_token_count=200,
        cached_content_token_count=0,
    )

    async def process(msg, history):
        usage.record("beerius", "gemini-3.6-flash", meta)
        if msg.text == "boom":
            raise RuntimeError("model failed after a billed call")
        return "ok"

    monkeypatch.setattr(agent, "process_message", process)
    for text in ("+1 beer", "boom"):
        await delivery.accept_message(message(text, text=text))
        await delivery.execute_one(agent)
    row = await pg.fetchrow("SELECT calls, input_tokens, output_tokens FROM model_usage")
    assert tuple(row) == (2, 8000, 440)
    assert usage._pending == {}  # Written while the database was awake, nothing left over.


async def test_failed_writes_wait_for_the_next_flush():
    usage.add("beerius", "gemini-3.6-flash", [1, 100, 10, 0])

    async def asleep(*args):
        raise OSError("database unreachable")

    await usage.flush(SimpleNamespace(execute=asleep))  # Never raises into message handling.
    assert list(usage._pending.values()) == [[1, 100, 10, 0]]


async def test_admin_usage_reports_the_month_against_budget(pg, monkeypatch):
    await setup(pg)
    today = date.today()
    await pg.execute(
        """INSERT INTO model_usage VALUES ($1, 'beerius', 'gemini-3.6-flash', 300, 2600000, 180000, 0),
               ($1, 'unknown', 'mystery-model', 1, 10, 10, 0),
               ($2, 'beerius', 'gemini-3.6-flash', 999, 99000000, 0, 0)""",
        today,
        today.replace(day=1) - timedelta(days=40),  # Older than last month: not counted.
    )
    monkeypatch.setattr(main.settings, "admin_token", "admin")
    monkeypatch.setattr(main.settings, "model_budget_usd", 1.0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://test",
        headers={"Authorization": "Bearer admin"},
    ) as client:
        report = (await client.get("/admin/usage")).json()
        assert (await client.get("/admin/usage", headers={"Authorization": ""})).status_code == 401
        # ops reads it with its read-only token, and that token opens nothing else.
        monkeypatch.setattr(main.settings, "ops_token", "ops")
        ops = {"Authorization": "Bearer ops"}
        assert (await client.get("/admin/usage", headers=ops)).status_code == 200
        assert (await client.get("/admin/groups", headers=ops)).status_code == 403
    beerius = 2.6 * 0.75 + 0.18 * 3.75  # $2.625 at 2026 list prices
    assert report["by_source"]["beerius"]["usd"] == pytest.approx(beerius, abs=0.01)
    assert report["usd"] == pytest.approx(beerius, abs=0.01)
    assert report["over_budget"] and report["unpriced_models"] == ["mystery-model"]
