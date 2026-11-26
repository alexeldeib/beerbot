"""Durable GroupMe inbox execution and independently claimed outbound delivery."""

import asyncio
import copy
import json
import logging
import time
import traceback
from collections import Counter
from datetime import datetime, UTC
from pathlib import Path

from .database import bind_execution, get_pool
from .config import settings
from .dispatch import dispatch
from . import usage
from .models import GroupMeMessage

logger = logging.getLogger(__name__)
EXECUTION_TIMEOUT = 90
MAX_EXECUTION_ATTEMPTS = 3
MAX_DELIVERY_ATTEMPTS = 5
# Terminal failures since boot, by error code. Memory-only, so ops can poll it without waking
# Postgres; the durable record is the failed rows themselves.
TERMINAL_FAILURES: Counter[str] = Counter()
# Accepted-but-unfinished messages (inbox id) and enqueued-but-undelivered replies (outbox id), with
# when each started waiting. Memory-only, so ops can see a stuck queue without waking Postgres.
UNPROCESSED: dict[int, float] = {}
UNDELIVERED: dict[int, float] = {}
STARTED_AT = datetime.now(UTC).isoformat()


def failure_detail(exc: BaseException) -> str:
    """Exception type and this repo's stack frames, never the message: it can hold chat data."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if "/src/beerbot/" in f.filename]
    where = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames))
    return f"{type(exc).__name__} at {where or 'unknown'}"[:500]


def record_terminal(code: str) -> None:
    TERMINAL_FAILURES[code] += 1
    dispatch("ops.yml", every=600, reason=code)  # No-op without a dispatch token.


class QueueRuntime:
    """Process-local wake hints. Postgres remains the durable source of truth.

    Both recovery timers use the same wall-clock boundary so empty scans share
    one Neon wake window. Actual work/retry deadlines always take precedence.
    """

    def __init__(self, recovery_seconds=None):
        self.recovery_seconds = recovery_seconds or settings.queue_recovery_seconds
        self.events = {name: asyncio.Event() for name in ("execution", "delivery")}
        self.scans = {name: 0 for name in self.events}
        self.failed = {name: False for name in self.events}
        # Last scan failure per worker, kept after recovery: what ops reports when /ready fails.
        self.last_error = {name: None for name in self.events}
        self.last_scan = {name: None for name in self.events}
        self.next_scan = {name: None for name in self.events}
        self.probe_failed = False

    def wake(self, name=None):
        for key in [name] if name else self.events:
            self.events[key].set()

    def recovery_delay(self):
        return self.recovery_seconds - time.time() % self.recovery_seconds

    async def wait(self, name, delay):
        self.next_scan[name] = datetime.fromtimestamp(time.time() + delay, UTC).isoformat()
        try:
            await asyncio.wait_for(self.events[name].wait(), timeout=delay)
        except TimeoutError:
            pass

    def snapshot(self):
        return {
            "recovery_seconds": self.recovery_seconds,
            "scans": self.scans.copy(),
            "failed": self.failed.copy(),
            "probe_failed": self.probe_failed,
            "last_scan": self.last_scan.copy(),
            "next_scan": self.next_scan.copy(),
            "terminal_failures": dict(TERMINAL_FAILURES),
            "last_error": self.last_error.copy(),
            "started_at": STARTED_AT,
            "git_sha": settings.git_sha,
            "oldest_unprocessed_seconds": _oldest(UNPROCESSED),
            "oldest_undelivered_seconds": _oldest(UNDELIVERED),
        }


def _oldest(waiting: dict[int, float]) -> int | None:
    return round(time.time() - min(waiting.values())) if waiting else None


async def next_work_delay(name: str, recovery_delay: float) -> float:
    """Arm a timer for known work, not a poll for hypothetical future work.

    The same scan lists what is still waiting, so the queue ages forget rows another machine
    finished (blue-green overlap), maintenance expired, or a crash left behind."""
    scanned = time.time()
    pool = await get_pool()
    async with pool.acquire() as conn:
        if name == "execution":
            tracked = UNPROCESSED
            row = await conn.fetchrow("""SELECT EXTRACT(EPOCH FROM MIN(i.available_at) FILTER (
                    WHERE NOT EXISTS(SELECT 1 FROM message_inbox earlier WHERE
                        earlier.group_id=i.group_id AND earlier.state='pending' AND earlier.id<i.id)
                    )-NOW()) AS due, array_agg(i.id) AS waiting
                FROM message_inbox i WHERE i.state='pending'""")
        else:
            tracked = UNDELIVERED
            row = await conn.fetchrow("""SELECT EXTRACT(EPOCH FROM MIN(
                    CASE WHEN o.state='sending' THEN o.updated_at+INTERVAL '121 seconds'
                         ELSE o.available_at END) FILTER (
                    WHERE NOT EXISTS(SELECT 1 FROM message_outbox earlier
                        WHERE earlier.group_id=o.group_id AND earlier.state IN ('pending','sending')
                        AND earlier.id<o.id))-NOW()) AS due, array_agg(o.id) AS waiting
                FROM message_outbox o WHERE o.state IN ('pending','sending')""")
    waiting = set(row["waiting"] or ())
    # Only entries committed before this scan began: a newer one may be missing from its snapshot.
    for key, since in list(tracked.items()):
        if key not in waiting and since < scanned:
            del tracked[key]
    due = row["due"]
    # Due rows may be locked by a peer during deployment. Keep checking while
    # real pending work exists; only an empty queue sleeps until recovery.
    return recovery_delay if due is None else min(recovery_delay, max(1, float(due)))


async def accept_message(message: GroupMeMessage, agent: str = "beerius") -> dict:
    """Acknowledge only after the payload is durably stored; first receipt wins. `agent` is the
    router's decision: which bot answers it."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO message_inbox(group_id,message_id,payload,agent)
               VALUES($1,$2,$3::jsonb,$4) ON CONFLICT(group_id,message_id) DO NOTHING
               RETURNING id""",
            message.group_id,
            message.id,
            message.model_dump_json(),
            agent,
        )
    if row:
        UNPROCESSED[row["id"]] = time.time()
    return {"status": "ok", "action": "queued" if row else "duplicate"}


async def accept_context(message: GroupMeMessage) -> None:
    """Keep beerdev's own posts as completed, never-processed rows in its thread: replies to them
    route to beerdev, and "veto" in reply to "💡 Queued: … /issues/31" carries its issue."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO message_inbox(group_id,message_id,payload,state,completed_at,agent)
               VALUES($1,$2,$3::jsonb,'completed',NOW(),'beerdev')
               ON CONFLICT(group_id,message_id) DO NOTHING""",
            message.group_id,
            message.id,
            message.model_dump_json(),
        )


async def execute_one(agent) -> bool:
    """Claim a group's oldest pending message and commit all DB effects together.

    The outer transaction holds the inbox row and group lock. A savepoint owns
    all model/tool effects, so execution failure can roll them back while the
    outer transaction records a bounded retry. Process death rolls everything
    back automatically, leaving the durable inbox message pending.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                -- Explicit columns: a pooled server-side plan for SELECT * breaks when the
                -- table gains a column (cached plan must not change result type).
                SELECT i.id, i.group_id, i.payload, i.attempts, i.agent FROM message_inbox i
                WHERE i.state='pending' AND i.available_at <= NOW()
                  AND NOT EXISTS (SELECT 1 FROM message_inbox earlier
                      WHERE earlier.group_id=i.group_id AND earlier.state='pending'
                        AND earlier.id<i.id)
                ORDER BY i.id LIMIT 1 FOR UPDATE OF i SKIP LOCKED
            """)
            if row is None:
                return False
            if not await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0))",
                "beerbot-message:" + row["group_id"],
            ):
                return False
            group_id = row["group_id"]
            # Preserve legacy in-memory behavior on rollback; durable history
            # takes over after the first completed run and survives restarts.
            history_before = copy.deepcopy(agent._get_history(group_id))
            bucket_before = copy.deepcopy(agent._get_bucket(group_id))
            finished, outbox_id = False, None
            try:
                async with conn.transaction():
                    await conn.execute("SET LOCAL statement_timeout = '10s'")
                    await conn.execute("SET LOCAL lock_timeout = '2s'")
                    async with asyncio.timeout(EXECUTION_TIMEOUT):
                        if settings.require_registered_groups and not await conn.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM groups WHERE group_id=$1)", group_id
                        ):
                            raise PermissionError("Group is no longer registered")
                        payload = row["payload"]
                        message = GroupMeMessage.model_validate_json(payload)
                        history = await conn.fetch(
                            """SELECT payload,reply,agent FROM message_inbox
                               WHERE group_id=$1 AND state='completed' AND payload IS NOT NULL
                               ORDER BY id DESC LIMIT 10""",
                            group_id,
                        )
                        if history:
                            agent._get_history(group_id).clear()
                            for previous in reversed(history):
                                msg = GroupMeMessage.model_validate_json(previous["payload"])
                                placeholder = (
                                    "(video)"
                                    if any(a.type == "video" for a in msg.attachments)
                                    else "(image)"
                                )
                                agent.record_message(
                                    group_id,
                                    msg.text or placeholder,
                                    msg.name,
                                    is_bot=msg.sender_type == "bot",
                                    message_id=msg.id,
                                    user_id=msg.user_id,
                                )
                                # beerdev's answers arrive as its own posts (context rows).
                                if previous["reply"] and previous["agent"] == "beerius":
                                    agent.record_message(
                                        group_id, previous["reply"], "Beerius", is_bot=True
                                    )
                        with bind_execution(conn) as scope:
                            if row["agent"] == "beerdev":
                                from . import beerdev  # beerdev reads status from this module

                                reply = await beerdev.process(message, agent)
                            else:
                                reply = await agent.process_message(message)
                            if scope.failed:
                                raise RuntimeError("Tool execution failed")
                            trace = json.dumps(scope.tools)
                        await conn.execute(
                            """UPDATE message_inbox SET state='completed', completed_at=NOW(),
                               attempts=attempts+1, reply=$2,tool_results=$3::jsonb,error_code=NULL
                               WHERE id=$1""",
                            row["id"],
                            reply,
                            trace,
                        )
                        finished = True
                        if reply:
                            outbox_id = await conn.fetchval(
                                """INSERT INTO message_outbox(inbox_id,group_id,body)
                                   VALUES($1,$2,$3) RETURNING id""",
                                row["id"],
                                group_id,
                                reply,
                            )
            except BaseException as exc:
                agent._message_history[group_id] = history_before
                agent._rate_limiters[group_id] = bucket_before
                if not isinstance(exc, Exception):
                    raise
                code = type(exc).__name__
                attempts = row["attempts"] + 1
                state = (
                    "failed"
                    if attempts >= MAX_EXECUTION_ATTEMPTS or isinstance(exc, PermissionError)
                    else "pending"
                )
                await conn.execute(
                    """UPDATE message_inbox SET attempts=$2,state=$3,error_code=$4,
                       available_at=NOW()+$5*INTERVAL '1 second',error_detail=$6 WHERE id=$1""",
                    row["id"],
                    attempts,
                    state,
                    code,
                    min(300, 5 * 2**attempts),
                    failure_detail(exc),
                )
                logger.error("Message execution %s: inbox=%s error=%s", state, row["id"], code)
                finished = state == "failed"
                if finished:
                    record_terminal(code)
        # Committed: the outside watchdog's queue ages follow what is now durable.
        if finished:
            UNPROCESSED.pop(row["id"], None)
        if outbox_id is not None:
            UNDELIVERED[outbox_id] = time.time()
        await usage.flush(conn)  # Awake anyway: this message's tokens, even if it failed.
    return True


async def deliver_one(client) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                SELECT o.id, o.group_id, o.body, o.attempts, i.agent, i.message_id
                FROM message_outbox o JOIN message_inbox i ON i.id = o.inbox_id
                WHERE o.state='pending' AND o.available_at <= NOW()
                  AND NOT EXISTS(SELECT 1 FROM message_outbox earlier
                      WHERE earlier.group_id=o.group_id AND earlier.id<o.id
                        AND earlier.state IN ('pending','sending'))
                ORDER BY o.id LIMIT 1 FOR UPDATE OF o SKIP LOCKED
            """)
            if row is None:
                return False
            unregistered = settings.require_registered_groups and not await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM groups WHERE group_id=$1)", row["group_id"]
            )
            if unregistered:
                await conn.execute(
                    """UPDATE message_outbox SET state='failed',error_code='unregistered_group',
                       updated_at=NOW() WHERE id=$1""",
                    row["id"],
                )
            else:
                await conn.execute(
                    """UPDATE message_outbox SET state='sending',attempts=attempts+1,
                       updated_at=NOW() WHERE id=$1""",
                    row["id"],
                )
        if unregistered:
            UNDELIVERED.pop(row["id"], None)
            return True
        # The committed sending marker makes a crash distinguishable from a
        # never-attempted send. Never automatically resend an uncertain outcome.
        try:
            async with asyncio.timeout(30):
                if row["agent"] == "beerdev":  # As beerdev's bot, threaded under the question.
                    result = await client.deliver_message(
                        row["body"],
                        group_id=row["group_id"],
                        bot_id=settings.beerdev_bot_id,
                        reply_to=row["message_id"],
                    )
                else:
                    result = await client.deliver_message(row["body"], group_id=row["group_id"])
            state, code = result.state, result.code
            delay = max(result.retry_after, min(300, 5 * 2 ** (row["attempts"] + 1)))
        except Exception as exc:
            state, code, delay = "uncertain", type(exc).__name__, 0
        if state == "retry":
            state = "pending" if row["attempts"] + 1 < MAX_DELIVERY_ATTEMPTS else "failed"
        await conn.execute(
            """UPDATE message_outbox SET state=$2,error_code=$3,updated_at=NOW(),
               available_at=NOW()+$4*INTERVAL '1 second' WHERE id=$1 AND state='sending'""",
            row["id"],
            state,
            code,
            delay,
        )
        logger.info("Reply delivery: outbox=%s state=%s code=%s", row["id"], state, code)
        if state != "pending":  # Sent, or a terminal state that ops tickets instead.
            UNDELIVERED.pop(row["id"], None)
        if state in ("failed", "uncertain"):
            record_terminal(f"delivery_{state}")
    return True


async def maintain_queue() -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            # Skip in-flight claims so maintenance cannot delay a live message.
            stale = await conn.execute("""WITH stale AS (
                SELECT id FROM message_outbox WHERE state='sending'
                  AND updated_at < NOW()-INTERVAL '2 minutes'
                LIMIT 100 FOR UPDATE SKIP LOCKED)
                UPDATE message_outbox SET state='uncertain',
                  error_code='interrupted_send',updated_at=NOW()
                WHERE id IN (SELECT id FROM stale)""")
            # Keep deduplication tombstones; discard private content after 3 days.
            inbox = await conn.execute("""WITH expired AS (
                SELECT id FROM message_inbox WHERE payload IS NOT NULL
                  AND created_at < NOW()-INTERVAL '3 days'
                LIMIT 100 FOR UPDATE SKIP LOCKED)
                UPDATE message_inbox SET payload=NULL,reply=NULL,tool_results=NULL,
                  state=CASE WHEN state='pending' THEN 'failed' ELSE state END,
                  error_code=CASE WHEN state='pending' THEN 'expired' ELSE error_code END
                WHERE id IN (SELECT id FROM expired)""")
            outbox = await conn.execute("""WITH expired AS (
                SELECT id FROM message_outbox WHERE body IS NOT NULL
                  AND created_at < NOW()-INTERVAL '3 days'
                LIMIT 100 FOR UPDATE SKIP LOCKED)
                UPDATE message_outbox SET body=NULL,
                  state=CASE WHEN state='pending' THEN 'failed' ELSE state END,
                  error_code=CASE WHEN state='pending' THEN 'expired' ELSE error_code END
                WHERE id IN (SELECT id FROM expired)""")
        await usage.flush(conn)  # Recaps and anything a failed flush left behind.
    # Continue bounded cleanup batches promptly instead of retaining a backlog
    # for multiple hourly sweeps. Locked live claims are still skipped.
    return any(int(result.split()[-1]) >= 100 for result in (stale, inbox, outbox))


async def queue_status() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        inbox = await conn.fetch("SELECT state,COUNT(*) AS count FROM message_inbox GROUP BY state")
        outbox = await conn.fetch(
            "SELECT state,COUNT(*) AS count FROM message_outbox GROUP BY state"
        )
        age = await conn.fetchval("""SELECT EXTRACT(EPOCH FROM NOW()-MIN(created_at))
            FROM message_inbox WHERE state='pending'""")
        attention = await conn.fetch("""SELECT id,inbox_id,state,attempts,error_code,created_at
            FROM message_outbox WHERE state IN ('failed','uncertain') ORDER BY id DESC LIMIT 50""")
        failed_runs = await conn.fetch("""SELECT id,attempts,error_code,error_detail,created_at
            FROM message_inbox WHERE state='failed' ORDER BY id DESC LIMIT 50""")
        return {
            "inbox": {r["state"]: r["count"] for r in inbox},
            "outbox": {r["state"]: r["count"] for r in outbox},
            "oldest_pending_seconds": float(age) if age is not None else None,
            "deliveries_needing_attention": [dict(r) for r in attention],
            "failed_executions": [dict(r) for r in failed_runs],
        }


async def retry_delivery(outbox_id: int, acknowledge_uncertain: bool = False) -> bool:
    """Requeue a stored reply; uncertain sends require acknowledging duplicate risk."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE message_outbox SET state='pending',attempts=0,error_code=NULL,
               available_at=NOW(),updated_at=NOW() WHERE id=$1 AND body IS NOT NULL
               AND created_at > NOW()-INTERVAL '3 days'
               AND (state='failed' OR (state='uncertain' AND $2)) RETURNING id""",
            outbox_id,
            acknowledge_uncertain,
        )
    return row is not None


async def _worker(name, runtime, work, after_work=None, maintenance=None):
    failures = 0
    while True:
        # Clear BEFORE scanning, never after: a wake racing with the empty scan
        # must survive and make the subsequent wait return immediately.
        runtime.events[name].clear()
        runtime.next_scan[name] = None
        runtime.scans[name] += 1
        try:
            more_maintenance = await maintenance() if maintenance else False
            while await work():
                if after_work:
                    after_work()
            delay = await next_work_delay(name, runtime.recovery_delay())
            if more_maintenance:
                delay = min(delay, 1)
            failures = 0
            runtime.failed[name] = False
            runtime.probe_failed = False
            runtime.last_scan[name] = datetime.now(UTC).isoformat()
        except Exception as exc:
            failures += 1
            runtime.failed[name] = True
            runtime.last_error[name] = {
                "at": datetime.now(UTC).isoformat(),
                "detail": failure_detail(exc),
            }
            delay = min(60, 5 * 2 ** min(failures - 1, 4))
            logger.exception("Queue worker scan failed: worker=%s", name)
        await runtime.wait(name, delay)


async def execution_worker(agent, runtime):
    await _worker(
        "execution",
        runtime,
        lambda: execute_one(agent),
        after_work=lambda: runtime.wake("delivery"),
    )


async def delivery_worker(client, runtime):
    await _worker("delivery", runtime, lambda: deliver_one(client), maintenance=maintain_queue)
