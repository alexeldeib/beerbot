"""FastAPI application entry point."""

import asyncio
import hmac
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .agent import beer_agent
from . import beerdev, usage
from .config import EASTERN, settings
from .database import close_pool, init_db, get_pool
from .groupme_client import groupme_client
from .llm import model_profile
from .models import GroupMeMessage
from .repositories import group_repo, recap_repo
from .delivery import (
    accept_context,
    accept_message,
    execution_worker,
    delivery_worker,
    queue_status,
    retry_delivery,
    QueueRuntime,
)
from .web import router as web_router, InviteInput, disable_account, invite_account
from .activity import AccessGrant, grant_access

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _redact_tokens(record: logging.LogRecord) -> bool:
    """GroupMe can only send the webhook secret in the query string, which access logs print."""
    if isinstance(record.args, tuple):
        record.args = tuple(
            re.sub(r"token=[^&\s]+", "token=***", arg) if isinstance(arg, str) else arg
            for arg in record.args
        )
    return True


logging.getLogger("uvicorn.access").addFilter(_redact_tokens)

_recap_revision = 0


async def _send_recap(group_id: str, week_start: date) -> bool:
    """One group's weekly recap. True once nothing is left to do this week: sent, possibly sent
    (an uncertain send is never repeated), claimed by another machine, or a week without drinks.
    False retries at the next tick."""
    try:
        if await recap_repo.has_sent(group_id, week_start):
            return True
        recap = await beer_agent.generate_weekly_recap(group_id)
        if recap is None or not await recap_repo.try_claim(group_id, week_start):
            return True
        result = await groupme_client.deliver_message(recap, group_id=group_id)
        if result.state in ("sent", "uncertain"):  # Uncertain may have posted: keep the claim.
            logger.info("Weekly recap %s for group %s", result.state, group_id)
            return True
        await recap_repo.release_claim(group_id, week_start)  # Never posted: safe to retry.
        logger.error("Weekly recap delivery failed for group %s: %s", group_id, result.code)
    except Exception:
        logger.exception("Weekly recap failed for group %s", group_id)
    return False


async def _recap_scheduler() -> None:
    """Background loop: fires recap on Sunday after WEEKLY_RECAP_HOUR ET."""
    completed_week = None
    completed_revision = None
    while True:
        await asyncio.sleep(300)  # 5 min tick
        now = datetime.now(EASTERN)
        if now.weekday() != 6 or now.hour < settings.weekly_recap_hour:
            continue
        if not settings.weekly_recap_enabled:
            continue

        week_start = (now - timedelta(days=now.weekday())).date()
        if completed_week == week_start and completed_revision == _recap_revision:
            continue
        revision = _recap_revision
        try:
            groups = await group_repo.list_all()
        except Exception:
            logger.exception("Weekly recap iteration failed; retrying at the next scheduled tick")
            continue
        if all([await _send_recap(group.group_id, week_start) for group in groups]):
            completed_week, completed_revision = week_start, revision


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    runtime = QueueRuntime()
    app.state.queue_runtime = runtime
    if not settings.is_development and not settings.groupme_webhook_secret:
        logger.warning("GROUPME_WEBHOOK_SECRET is not configured")
    tasks = [
        asyncio.create_task(_recap_scheduler()),
        asyncio.create_task(execution_worker(beer_agent, runtime)),
        asyncio.create_task(delivery_worker(groupme_client, runtime)),
    ]
    app.state.message_workers = tasks[1:]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_pool()


app = FastAPI(
    title="Beerbot",
    description="GroupMe bot for tracking beer consumption",
    version="0.2.0",
    lifespan=lifespan,
)
app.include_router(web_router)


@app.middleware("http")
async def web_security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/app" or request.url.path.startswith("/app/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
    return response


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


NOT_READY = {"status": "not_ready"}


def _workers_running(request: Request) -> bool:
    """Both queue workers alive and a model configured. Memory only."""
    workers = getattr(request.app.state, "message_workers", [])
    alive = len(workers) == 2 and not any(task.done() for task in workers)
    return alive and beer_agent.client is not None


@app.get("/ready")
async def readiness(request: Request):
    """Frequent Fly probe: no database I/O, so a sleeping Neon compute stays asleep."""
    runtime = getattr(request.app.state, "queue_runtime", None)
    if not _workers_running(request) or (
        runtime and (runtime.probe_failed or any(runtime.failed.values()))
    ):
        return JSONResponse(NOT_READY, status_code=503)
    return {"status": "ready"}


@app.get("/ready/db")
async def database_readiness(request: Request):
    """Explicit deploy/diagnostic check, never a recurring health probe.

    Waking after the old blue-green fleet has exited closes the handoff race:
    a late committed receipt cannot be stranded after the new worker's boot scan.
    """
    runtime = getattr(request.app.state, "queue_runtime", None)
    if not _workers_running(request):
        return JSONResponse(NOT_READY, status_code=503)
    try:
        async with asyncio.timeout(10):
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
    except Exception:
        if runtime:
            runtime.probe_failed = True
            runtime.wake()
        return JSONResponse(NOT_READY, status_code=503)
    if runtime:
        runtime.probe_failed = False
        runtime.wake()
    return {"status": "ready"}


@app.get("/version")
async def version_info():
    profile = model_profile(settings)
    return {
        "version": settings.app_version,
        "git_sha": settings.git_sha,
        "llm": {
            "provider": profile.provider,
            "model": profile.model,
            "capabilities": {
                "images": profile.capabilities.images,
                "video": profile.capabilities.video,
                "tools": profile.capabilities.tools,
            },
        },
    }


@app.post("/callback")
async def groupme_callback(request: Request):
    """Handle incoming GroupMe webhook callbacks."""
    expected_secret = settings.groupme_webhook_secret
    if expected_secret:
        provided_secret = request.query_params.get("token", "")
        if not hmac.compare_digest(provided_secret, expected_secret):
            return JSONResponse({"error": "Unauthorized webhook"}, status_code=401)

    try:
        data = await request.json()
        message = GroupMeMessage(**data)
    except Exception:
        return JSONResponse({"error": "Invalid message format"}, status_code=400)

    if message.sender_type == "bot":
        # Our bots' own posts, echoed right after we deliver them (the database is awake), become
        # history and reply targets. No bot message is processed, and other bots' are ignored.
        # ponytail: matched by GroupMe bot name; a group whose Beerius bot has another name keeps
        # no history of its replies.
        agent = message.name.lower()
        if agent in ("beerius", "beerdev") and (
            not settings.require_registered_groups
            or await group_repo.get_by_group_id(message.group_id)
        ):
            await accept_context(message, agent)
        return {"status": "ignored", "reason": "bot message"}

    if settings.require_registered_groups:
        group = await group_repo.get_by_group_id(message.group_id)
        if not group:
            logger.warning("Rejected callback for unregistered group %s", message.group_id)
            return JSONResponse({"error": "Unknown group"}, status_code=403)

    # The router: beerdev answers messages that start with "beerdev" or reply to its posts;
    # Beerius answers the rest.
    agent = "beerdev" if await beerdev.addressed(message) else "beerius"
    result = await accept_message(message, agent)
    if runtime := getattr(request.app.state, "queue_runtime", None):
        runtime.wake()
    return result


# --- Admin Endpoints ---


class GroupRegistration(BaseModel):
    group_id: str
    bot_id: str
    name: str | None = None


def _group_json(group) -> dict:
    """A registered group for the admin API, without its bot credential."""
    return {
        "group_id": group.group_id,
        "workspace_id": group.workspace_id,
        "credential_configured": bool(group.bot_id),
        "name": group.name,
        "created_at": group.created_at.isoformat(),
    }


class WorkspaceEnvironment(BaseModel):
    environment: Literal["production", "test"]


def _bearer(authorization: str | None) -> str | None:
    """The token of an "Authorization: Bearer <token>" header; None for any other shape."""
    parts = (authorization or "").split()
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None


async def verify_admin_token(authorization: str | None = Header(None)) -> None:
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="Admin endpoints not configured")
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    if (token := _bearer(authorization)) is None:
        raise HTTPException(status_code=401, detail="Invalid authorization format")
    if not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(status_code=403, detail="Invalid admin token")


async def verify_ops_token(authorization: str | None = Header(None)) -> bool:
    """ops (ops.yml) holds OPS_TOKEN: it may only read worker and queue status and usage. Every
    other admin route, waking the queue included, needs ADMIN_TOKEN. True when it was ops."""
    token = _bearer(authorization)
    if settings.ops_token and token and hmac.compare_digest(token, settings.ops_token):
        return True
    await verify_admin_token(authorization)
    return False


@app.post("/admin/accounts/invite", dependencies=[Depends(verify_admin_token)])
async def invite_web_account(invitation: InviteInput):
    return await invite_account(invitation)


@app.post("/admin/accounts/{account_id}/disable", dependencies=[Depends(verify_admin_token)])
async def disable_web_account(account_id: str):
    return await disable_account(account_id)


@app.patch(
    "/admin/workspaces/{workspace_id}/environment", dependencies=[Depends(verify_admin_token)]
)
async def set_workspace_environment(workspace_id: str, classification: WorkspaceEnvironment):
    """Classify personal-history visibility without moving identities or activity."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE workspaces SET settings=jsonb_set(settings,'{environment}',
                   to_jsonb($2::text)),updated_at=now() WHERE id=$1 RETURNING id""",
            workspace_id,
            classification.environment,
        )
    if row is None:
        raise HTTPException(404, "Workspace not found")
    return {"workspace_id": row["id"], "environment": classification.environment}


@app.put("/admin/workspaces/{workspace_id}/app-access", dependencies=[Depends(verify_admin_token)])
async def set_app_access(workspace_id: str, grant: AccessGrant):
    return await grant_access(workspace_id, grant)


@app.get("/admin/messages/status", dependencies=[Depends(verify_ops_token)])
async def message_status():
    return await queue_status()


@app.get("/admin/workers/status")
async def worker_status(request: Request, from_ops: bool = Depends(verify_ops_token)):
    """In-memory diagnostics do not wake Postgres. ops' own reads are its heartbeat: the status
    shows how long ago the previous one was, and a silent ops gets restarted (watch_ops)."""
    runtime = getattr(request.app.state, "queue_runtime", None)
    status = runtime.snapshot() if runtime else {"status": "not_started"}
    if runtime and from_ops:
        runtime.ops_seen = time.time()
    return status | {"model_usage_since_boot": usage.since_boot()}


@app.get("/admin/usage", dependencies=[Depends(verify_ops_token)])
async def model_usage():
    """Model tokens and list-price spend this month, by source, against the budget."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await usage.flush(conn)
        return await usage.report(conn)


@app.post("/admin/messages/wake", dependencies=[Depends(verify_admin_token)])
async def wake_messages(request: Request):
    runtime = getattr(request.app.state, "queue_runtime", None)
    if runtime is None:
        raise HTTPException(503, "Workers not started")
    runtime.wake()
    return {"status": "woken"}


@app.post("/admin/messages/outbox/{outbox_id}/retry", dependencies=[Depends(verify_admin_token)])
async def retry_outbound(request: Request, outbox_id: int, acknowledge_uncertain: bool = False):
    if not await retry_delivery(outbox_id, acknowledge_uncertain):
        raise HTTPException(
            status_code=409, detail="Delivery not retryable or uncertainty not acknowledged"
        )
    if runtime := getattr(request.app.state, "queue_runtime", None):
        runtime.wake("delivery")
    return {"status": "queued"}


@app.post("/admin/groups", dependencies=[Depends(verify_admin_token)])
async def register_group(registration: GroupRegistration):
    global _recap_revision
    group = await group_repo.register(
        group_id=registration.group_id,
        bot_id=registration.bot_id,
        name=registration.name,
    )
    groupme_client.clear_cache(registration.group_id)
    _recap_revision += 1

    return {
        "status": "ok",
        "group": _group_json(group),
    }


@app.get("/admin/groups", dependencies=[Depends(verify_admin_token)])
async def list_groups():
    groups = await group_repo.list_all()
    return {
        "status": "ok",
        "groups": [_group_json(g) for g in groups],
    }


@app.delete("/admin/groups/{group_id}", dependencies=[Depends(verify_admin_token)])
async def delete_group(group_id: str):
    global _recap_revision
    deleted = await group_repo.delete(group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Group not found")
    groupme_client.clear_cache(group_id)
    _recap_revision += 1
    return {"status": "ok", "deleted": group_id}
