"""Idle scheduling, wake races, durable retry deadlines, and health probe tests."""

import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from src.beerbot import delivery, main
from src.beerbot import web
from src.beerbot.agent import BeerAgent
from src.beerbot.groupme_client import DeliveryResult
from tests.test_durable_delivery import setup, message


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def stop(*tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_failure_detail_names_the_dependency_and_status_never_the_message():
    import openai
    from google.genai import errors

    def detail(exc: Exception) -> str:
        try:
            raise exc
        except Exception as raised:
            return delivery.failure_detail(raised)

    google = detail(errors.ServerError(503, {"error": {"message": "chat text"}}))
    assert google.startswith("google.genai.errors.ServerError 503 at ")
    request = httpx.Request("POST", "https://compatible.test")
    limited = openai.RateLimitError(
        "chat text", response=httpx.Response(429, request=request), body=None
    )
    assert detail(limited).startswith("openai.RateLimitError 429 at ")
    assert detail(ValueError("chat text")) == "ValueError at unknown"  # No frames of ours here.
    assert all("chat text" not in d for d in (google, detail(limited)))
    assert len(detail(RuntimeError("x" * 1000))) <= 500


async def test_empty_worker_sleeps_without_repeated_database_scans(monkeypatch):
    runtime = delivery.QueueRuntime()
    work = AsyncMock(return_value=False)
    schedule = AsyncMock(return_value=3600)
    monkeypatch.setattr(delivery, "next_work_delay", schedule)
    task = asyncio.create_task(delivery._worker("execution", runtime, work))
    try:
        await until(lambda: runtime.next_scan["execution"] is not None)
        await asyncio.sleep(0.1)
        assert work.await_count == 1 and schedule.await_count == 1
        runtime.wake("execution")
        await until(lambda: work.await_count == 2)
        assert not runtime.failed["execution"]
    finally:
        await stop(task)


async def test_wake_during_empty_scan_is_not_lost(monkeypatch):
    runtime = delivery.QueueRuntime()
    work = AsyncMock(return_value=False)
    calls = 0

    async def schedule(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            runtime.wake("execution")
        return 3600

    monkeypatch.setattr(delivery, "next_work_delay", schedule)
    task = asyncio.create_task(delivery._worker("execution", runtime, work))
    try:
        await until(lambda: work.await_count == 2)
        await asyncio.sleep(0.05)
        assert work.await_count == 2
    finally:
        await stop(task)


async def test_sparse_recovery_runs_without_incoming_event(monkeypatch):
    runtime = delivery.QueueRuntime(recovery_seconds=0.05)
    work = AsyncMock(return_value=False)

    async def schedule(name, delay):
        return delay

    monkeypatch.setattr(delivery, "next_work_delay", schedule)
    task = asyncio.create_task(delivery._worker("execution", runtime, work))
    try:
        await until(lambda: work.await_count >= 3)
    finally:
        await stop(task)


async def test_worker_error_is_visible_and_wake_can_recover(monkeypatch):
    runtime = delivery.QueueRuntime()
    work = AsyncMock(side_effect=[RuntimeError("test failure"), False])
    monkeypatch.setattr(delivery, "next_work_delay", AsyncMock(return_value=3600))
    monkeypatch.setattr(delivery, "LAST_ERROR", {"execution": None, "delivery": None})
    task = asyncio.create_task(delivery._worker("execution", runtime, work))
    try:
        await until(lambda: runtime.failed["execution"])
        await asyncio.sleep(0.05)
        assert work.await_count == 1  # no tight error loop
        error = runtime.snapshot()["last_error"]["execution"]
        assert (
            error["detail"].startswith("RuntimeError at") and "test failure" not in error["detail"]
        )
        runtime.wake("execution")
        await until(lambda: work.await_count == 2 and not runtime.failed["execution"])
        # Kept after recovery for ops to report.
        assert runtime.snapshot()["last_error"]["execution"] == error
    finally:
        await stop(task)


async def test_failed_attempts_and_sends_are_the_workers_last_error(pg, monkeypatch):
    # ops reads these to tell a failing dependency (model, GroupMe) from a bug in our code.
    from google.genai import errors

    await setup(pg)
    monkeypatch.setattr(delivery, "LAST_ERROR", {"execution": None, "delivery": None})
    runtime = delivery.QueueRuntime()
    agent = BeerAgent()
    outage = errors.ServerError(503, {"error": {"message": "chat text"}})
    monkeypatch.setattr(agent, "process_message", AsyncMock(side_effect=[outage, "Reply"]))
    await delivery.accept_message(message())
    assert await delivery.execute_one(agent)  # Failed once; it retries later.
    error = runtime.snapshot()["last_error"]["execution"]
    assert error["detail"].startswith("google.genai.errors.ServerError 503 at delivery.py:")
    assert error["at"] and "chat text" not in error["detail"]
    await pg.execute("UPDATE message_inbox SET available_at=now()")
    assert await delivery.execute_one(agent)
    for outcome, detail in [
        (DeliveryResult("uncertain", "http_502"), "GroupMe http_502 (uncertain)"),
        (RuntimeError("chat text"), "RuntimeError at delivery.py:"),
    ]:
        sender = MagicMock(deliver_message=AsyncMock(side_effect=[outcome]))
        await pg.execute("UPDATE message_outbox SET state='pending', available_at=now()")
        assert await delivery.deliver_one(sender)
        assert runtime.snapshot()["last_error"]["delivery"]["detail"].startswith(detail)


async def test_live_workers_process_callback_without_waiting_for_recovery(pg, monkeypatch):
    await setup(pg)
    runtime = delivery.QueueRuntime()
    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", AsyncMock(return_value="Test reply"))
    sender = MagicMock(deliver_message=AsyncMock(return_value=DeliveryResult("sent")))
    tasks = [
        asyncio.create_task(delivery.execution_worker(agent, runtime)),
        asyncio.create_task(delivery.delivery_worker(sender, runtime)),
    ]
    monkeypatch.setattr(main, "accept_message", delivery.accept_message)
    monkeypatch.setattr(main.settings, "groupme_webhook_secret", None)
    monkeypatch.setattr(main.app.state, "queue_runtime", runtime, raising=False)
    try:
        await until(lambda: all(runtime.next_scan.values()))
        baseline = runtime.scans.copy()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="https://test"
        ) as client:
            response = await client.post("/callback", json=message().model_dump(mode="json"))
        assert response.status_code == 200
        await until(lambda: sender.deliver_message.await_count == 1)
        await until(lambda: all(runtime.next_scan.values()))
        assert await pg.fetchval("SELECT state FROM message_outbox") == "sent"
        assert runtime.scans["execution"] > baseline["execution"]
        scans = runtime.scans.copy()
        await asyncio.sleep(0.1)
        assert runtime.scans == scans
    finally:
        await stop(*tasks)


async def test_retry_deadlines_and_locked_receipts_are_not_treated_as_empty(pg):
    await setup(pg)
    await delivery.accept_message(message())
    await pg.execute("UPDATE message_inbox SET available_at=now()+interval '45 seconds'")
    assert 40 < await delivery.next_work_delay("execution", 3600) <= 45
    await pg.execute("UPDATE message_inbox SET available_at=now()")
    async with pg.acquire() as conn, conn.transaction():
        await conn.fetchrow("SELECT id FROM message_inbox FOR UPDATE")
        assert not await delivery.execute_one(MagicMock())
        assert await delivery.next_work_delay("execution", 3600) == 1


async def test_sending_head_arms_recovery_timer_not_blocked_later_row(pg):
    await setup(pg)
    await delivery.accept_message(message("one"))
    await delivery.accept_message(message("two"))
    await pg.execute(
        "INSERT INTO message_outbox(inbox_id,group_id,body,state) SELECT id,group_id,'test',CASE WHEN message_id='one' THEN 'sending' ELSE 'pending' END FROM message_inbox ORDER BY id"
    )
    assert 118 < await delivery.next_work_delay("delivery", 3600) <= 121
    await pg.execute(
        "UPDATE message_outbox SET updated_at=now()-interval '3 minutes' WHERE state='sending'"
    )
    await delivery.maintain_queue()
    assert (
        await pg.fetchval("SELECT error_code FROM message_outbox WHERE state='uncertain'")
        == "interrupted_send"
    )
    assert await delivery.next_work_delay("delivery", 3600) == 1
    sender = MagicMock(deliver_message=AsyncMock(return_value=DeliveryResult("sent")))
    assert await delivery.deliver_one(sender)
    assert sender.deliver_message.await_count == 1  # uncertain first send is not repeated


async def test_delivery_retry_deadline_survives_a_new_runtime(pg):
    await setup(pg)
    await delivery.accept_message(message())
    await pg.execute(
        "INSERT INTO message_outbox(inbox_id,group_id,body,available_at) SELECT id,group_id,'test',now()+interval '30 seconds' FROM message_inbox"
    )
    runtime = delivery.QueueRuntime()
    delay = await delivery.next_work_delay("delivery", runtime.recovery_delay())
    assert 0 < delay <= 30
    assert await delivery.next_work_delay("execution", 3600) == 1


async def test_retention_backlog_requests_another_bounded_cleanup(pg):
    await setup(pg)
    await pg.execute("""INSERT INTO message_inbox(group_id,message_id,payload,state,created_at)
        SELECT 'g1','old-'||i,'{}'::jsonb,'completed',now()-interval '4 days'
        FROM generate_series(1,101) AS i""")
    assert await delivery.maintain_queue()
    assert not await delivery.maintain_queue()
    assert await pg.fetchval("SELECT count(*) FROM message_inbox WHERE payload IS NOT NULL") == 0
    assert await pg.fetchval("SELECT count(*) FROM message_inbox") == 101


def test_health_and_worker_status_are_database_free(monkeypatch):
    pool = AsyncMock(side_effect=AssertionError("Unexpected DB probe"))
    monkeypatch.setattr(main, "get_pool", pool)
    monkeypatch.setattr(main.beer_agent, "client", MagicMock())
    monkeypatch.setattr(main.settings, "admin_token", "test-admin")
    with TestClient(main.app) as client:
        for _ in range(10):
            assert client.get("/ready").status_code == 200
            assert client.get("/health").status_code == 200
        assert client.get("/admin/workers/status").status_code == 401
        assert client.get(
            "/admin/workers/status", headers={"Authorization": "Bearer test-admin"}
        ).json()["scans"] == {"execution": 0, "delivery": 0}
        assert client.post("/admin/messages/wake").status_code == 401
        assert (
            client.post(
                "/admin/messages/wake", headers={"Authorization": "Bearer test-admin"}
            ).status_code
            == 200
        )
        assert all(event.is_set() for event in main.app.state.queue_runtime.events.values())
    pool.assert_not_awaited()


def test_manual_retry_wakes_delivery_worker(monkeypatch):
    monkeypatch.setattr(main.settings, "admin_token", "test-admin")
    monkeypatch.setattr(main, "retry_delivery", AsyncMock(return_value=True))
    with TestClient(main.app) as client:
        result = client.post(
            "/admin/messages/outbox/123/retry", headers={"Authorization": "Bearer test-admin"}
        )
        assert result.status_code == 200
        assert main.app.state.queue_runtime.events["delivery"].is_set()


def test_anonymous_app_load_does_not_wake_database(monkeypatch):
    pool = AsyncMock(side_effect=AssertionError("Anonymous request touched DB"))
    monkeypatch.setattr(web, "get_pool", pool)
    with TestClient(main.app) as client:
        assert client.get("/app").status_code == 200
        assert client.get("/app/api/config").status_code == 200
        assert client.get("/app/api/me").status_code == 401
    pool.assert_not_awaited()


async def test_recap_completion_is_cached_without_repeated_database_checks(monkeypatch):
    from src.beerbot.main import _recap_scheduler

    sunday = datetime(2026, 9, 20, 21, 5, tzinfo=ZoneInfo("America/New_York"))
    fake_datetime = MagicMock()
    fake_datetime.now.return_value = sunday
    monkeypatch.setattr(main, "datetime", fake_datetime)
    monkeypatch.setattr(main.settings, "weekly_recap_enabled", True)
    monkeypatch.setattr(main.settings, "weekly_recap_hour", 21)
    groups = AsyncMock(return_value=[MagicMock(group_id="g1")])
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(main.group_repo, "list_all", groups)
    monkeypatch.setattr(main.recap_repo, "has_sent", sent)
    ticks = 0

    async def sleep(seconds):
        nonlocal ticks
        ticks += 1
        if ticks == 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await _recap_scheduler()
    groups.assert_awaited_once()
    sent.assert_awaited_once()


async def test_queue_ages_show_a_stuck_message_without_touching_the_database(pg, monkeypatch):
    # ops pages on these ages from outside, so they live in memory and follow committed state.
    await setup(pg)
    monkeypatch.setattr(delivery, "UNPROCESSED", {})
    monkeypatch.setattr(delivery, "UNDELIVERED", {})
    runtime = delivery.QueueRuntime()
    assert runtime.snapshot()["oldest_unprocessed_seconds"] is None
    await delivery.accept_message(message())
    monkeypatch.setattr(delivery.time, "time", lambda: 10_000.0)  # Pretend 3 minutes pass:
    delivery.UNPROCESSED.update({key: 10_000.0 - 180 for key in delivery.UNPROCESSED})
    assert runtime.snapshot()["oldest_unprocessed_seconds"] == 180
    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", AsyncMock(return_value="Test reply"))
    assert await delivery.execute_one(agent)
    snapshot = runtime.snapshot()
    assert snapshot["oldest_unprocessed_seconds"] is None  # Answered,
    assert snapshot["oldest_undelivered_seconds"] == 0  # and its reply waits to be posted.
    sender = MagicMock(deliver_message=AsyncMock(return_value=DeliveryResult("sent")))
    assert await delivery.deliver_one(sender)
    assert runtime.snapshot()["oldest_undelivered_seconds"] is None


async def test_queue_ages_forget_rows_no_longer_waiting(pg, monkeypatch):
    # A peer machine (blue-green overlap) finished these, or the group went away: an empty scan
    # must drop them, or ops pages on a queue that is actually empty.
    await setup(pg)
    monkeypatch.setattr(delivery, "UNPROCESSED", {})
    monkeypatch.setattr(delivery, "UNDELIVERED", {})
    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", AsyncMock(return_value="Reply"))
    await delivery.accept_message(message("answered"))
    assert await delivery.execute_one(agent)
    await pg.execute("DELETE FROM groups WHERE group_id='g1'")
    sender = MagicMock(deliver_message=AsyncMock(side_effect=AssertionError("unregistered")))
    assert await delivery.deliver_one(sender)
    assert delivery.UNDELIVERED == {}  # Failed as unregistered: no longer waiting.

    await pg.execute("INSERT INTO groups(group_id,bot_id) VALUES('g1','fake')")
    for id in ("peer", "waiting"):
        await delivery.accept_message(message(id))
    await pg.execute("UPDATE message_inbox SET state='completed' WHERE message_id='peer'")
    outbox = await pg.fetchval(
        """INSERT INTO message_outbox(inbox_id,group_id,body,state)
           SELECT id,group_id,'sent elsewhere','sent' FROM message_inbox
           WHERE message_id='peer' RETURNING id"""
    )
    delivery.UNDELIVERED[outbox] = 0.0
    delivery.UNPROCESSED[-1] = time.time() + 60  # Accepted after the scan began: kept.
    await delivery.next_work_delay("execution", 3600)
    await delivery.next_work_delay("delivery", 3600)
    waiting = await pg.fetchval("SELECT id FROM message_inbox WHERE message_id='waiting'")
    assert set(delivery.UNPROCESSED) == {waiting, -1}
    assert delivery.UNDELIVERED == {}
