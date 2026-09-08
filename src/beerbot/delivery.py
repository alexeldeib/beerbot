"""Durable GroupMe inbox execution and independently claimed outbound delivery."""

import asyncio
import copy
import json
import logging

from .database import bind_execution, get_pool
from .config import settings
from .models import GroupMeMessage

logger = logging.getLogger(__name__)
EXECUTION_TIMEOUT = 90
MAX_EXECUTION_ATTEMPTS = 3
MAX_DELIVERY_ATTEMPTS = 5


async def accept_message(message: GroupMeMessage) -> dict:
    """Acknowledge only after the payload is durably stored; first receipt wins."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO message_inbox(group_id,message_id,payload)
               VALUES($1,$2,$3::jsonb) ON CONFLICT(group_id,message_id) DO NOTHING
               RETURNING id""",
            message.group_id,
            message.id,
            message.model_dump_json(),
        )
    return {"status": "ok", "action": "queued" if row else "duplicate"}


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
                SELECT i.* FROM message_inbox i
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
                            """SELECT payload,reply FROM message_inbox
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
                                    message_id=msg.id,
                                    user_id=msg.user_id,
                                )
                                if previous["reply"]:
                                    agent.record_message(
                                        group_id, previous["reply"], "Beerius", is_bot=True
                                    )
                        with bind_execution(conn) as scope:
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
                        if reply:
                            await conn.execute(
                                """INSERT INTO message_outbox(inbox_id,group_id,body)
                                   VALUES($1,$2,$3)""",
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
                       available_at=NOW()+$5*INTERVAL '1 second' WHERE id=$1""",
                    row["id"],
                    attempts,
                    state,
                    code,
                    min(300, 5 * 2**attempts),
                )
                logger.error("Message execution %s: inbox=%s error=%s", state, row["id"], code)
    return True


async def deliver_one(client) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                SELECT o.* FROM message_outbox o
                WHERE o.state='pending' AND o.available_at <= NOW()
                  AND NOT EXISTS(SELECT 1 FROM message_outbox earlier
                      WHERE earlier.group_id=o.group_id AND earlier.id<o.id
                        AND earlier.state IN ('pending','sending'))
                ORDER BY o.id LIMIT 1 FOR UPDATE OF o SKIP LOCKED
            """)
            if row is None:
                return False
            if settings.require_registered_groups and not await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM groups WHERE group_id=$1)", row["group_id"]
            ):
                await conn.execute(
                    "UPDATE message_outbox SET state='failed',error_code='unregistered_group',updated_at=NOW() WHERE id=$1",
                    row["id"],
                )
                return True
            await conn.execute(
                """UPDATE message_outbox SET state='sending',attempts=attempts+1,
                   updated_at=NOW() WHERE id=$1""",
                row["id"],
            )
        # The committed sending marker makes a crash distinguishable from a
        # never-attempted send. Never automatically resend an uncertain outcome.
        try:
            async with asyncio.timeout(30):
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
    return True


async def maintain_queue() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            # Skip in-flight claims so maintenance cannot delay a live message.
            await conn.execute("""WITH stale AS (
                SELECT id FROM message_outbox WHERE state='sending'
                  AND updated_at < NOW()-INTERVAL '2 minutes'
                LIMIT 100 FOR UPDATE SKIP LOCKED)
                UPDATE message_outbox SET state='uncertain',
                  error_code='interrupted_send',updated_at=NOW()
                WHERE id IN (SELECT id FROM stale)""")
            # Keep deduplication tombstones; discard private content after 3 days.
            await conn.execute("""WITH expired AS (
                SELECT id FROM message_inbox WHERE payload IS NOT NULL
                  AND created_at < NOW()-INTERVAL '3 days'
                LIMIT 100 FOR UPDATE SKIP LOCKED)
                UPDATE message_inbox SET payload=NULL,reply=NULL,tool_results=NULL,
                  state=CASE WHEN state='pending' THEN 'failed' ELSE state END,
                  error_code=CASE WHEN state='pending' THEN 'expired' ELSE error_code END
                WHERE id IN (SELECT id FROM expired)""")
            await conn.execute("""WITH expired AS (
                SELECT id FROM message_outbox WHERE body IS NOT NULL
                  AND created_at < NOW()-INTERVAL '3 days'
                LIMIT 100 FOR UPDATE SKIP LOCKED)
                UPDATE message_outbox SET body=NULL,
                  state=CASE WHEN state='pending' THEN 'failed' ELSE state END,
                  error_code=CASE WHEN state='pending' THEN 'expired' ELSE error_code END
                WHERE id IN (SELECT id FROM expired)""")


async def queue_status() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        inbox = await conn.fetch("SELECT state,COUNT(*) AS count FROM message_inbox GROUP BY state")
        outbox = await conn.fetch(
            "SELECT state,COUNT(*) AS count FROM message_outbox GROUP BY state"
        )
        age = await conn.fetchval("""SELECT EXTRACT(EPOCH FROM NOW()-MIN(created_at))
            FROM message_inbox WHERE state='pending'""")
        attention = await conn.fetch("""SELECT id,inbox_id,state,attempts,error_code
            FROM message_outbox WHERE state IN ('failed','uncertain') ORDER BY id DESC LIMIT 50""")
        failed_runs = await conn.fetch("""SELECT id,attempts,error_code
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


async def execution_worker(agent):
    while True:
        try:
            if await execute_one(agent):
                continue
        except Exception:
            logger.exception("Message worker iteration failed")
        await asyncio.sleep(1)


async def delivery_worker(client):
    ticks = 0
    while True:
        try:
            maintenance_due = ticks % 60 == 0
            ticks += 1
            if maintenance_due:
                await maintain_queue()
            if await deliver_one(client):
                continue
        except Exception:
            logger.exception("Delivery worker iteration failed")
        await asyncio.sleep(1)
