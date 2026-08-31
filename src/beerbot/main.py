"""FastAPI application entry point."""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .agent import beer_agent
from .config import settings
from .database import close_pool, init_db
from .groupme_client import groupme_client
from .llm import model_profile
from .models import GroupMeMessage
from .repositories import group_repo, recap_repo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")


async def _recap_scheduler() -> None:
    """Background loop: fires recap on Sunday after WEEKLY_RECAP_HOUR ET."""
    while True:
        await asyncio.sleep(300)  # 5 min tick
        now = datetime.now(EASTERN)
        if now.weekday() != 6 or now.hour < settings.weekly_recap_hour:
            continue
        if not settings.weekly_recap_enabled:
            continue

        week_start = (now - timedelta(days=now.weekday())).date()
        groups = await group_repo.list_all()
        for group in groups:
            if await recap_repo.has_sent(group.group_id, week_start):
                continue
            recap = await beer_agent.generate_weekly_recap(group.group_id)
            if recap and await recap_repo.try_claim(group.group_id, week_start):
                sent = await groupme_client.send_message(recap, group_id=group.group_id)
                if sent:
                    logger.info("Sent weekly recap for group %s", group.group_id)
                else:
                    await recap_repo.release_claim(group.group_id, week_start)
                    logger.error("Weekly recap delivery failed for group %s", group.group_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if settings.is_development is False and not settings.groupme_webhook_secret:
        logger.warning("GROUPME_WEBHOOK_SECRET is not configured")
    task = asyncio.create_task(_recap_scheduler())
    yield
    task.cancel()
    await close_pool()


app = FastAPI(
    title="Beerbot",
    description="GroupMe bot for tracking beer consumption",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


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
        return {"status": "ignored", "reason": "bot message"}

    if settings.require_registered_groups:
        group = await group_repo.get_by_group_id(message.group_id)
        if not group:
            logger.warning("Rejected callback for unregistered group %s", message.group_id)
            return JSONResponse({"error": "Unknown group"}, status_code=403)

    reply = await beer_agent.process_message(message)

    if reply:
        sent = await groupme_client.send_message(reply, group_id=message.group_id)
        if not sent:
            logger.error("Reply delivery failed for group %s", message.group_id)
            return JSONResponse(
                {"status": "error", "action": "delivery_failed"},
                status_code=502,
            )
        return {"status": "ok", "action": "replied"}

    return {"status": "ok", "action": "none"}


# --- Admin Endpoints ---


class GroupRegistration(BaseModel):
    group_id: str
    bot_id: str
    name: str | None = None


async def verify_admin_token(authorization: str | None = Header(None)) -> None:
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="Admin endpoints not configured")
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization format")
    if not hmac.compare_digest(parts[1], settings.admin_token):
        raise HTTPException(status_code=403, detail="Invalid admin token")


@app.post("/admin/groups", dependencies=[Depends(verify_admin_token)])
async def register_group(registration: GroupRegistration):
    group = await group_repo.register(
        group_id=registration.group_id,
        bot_id=registration.bot_id,
        name=registration.name,
    )
    groupme_client.clear_cache(registration.group_id)

    return {
        "status": "ok",
        "group": {
            "group_id": group.group_id,
            "workspace_id": group.workspace_id,
            "credential_configured": bool(group.bot_id),
            "name": group.name,
            "created_at": group.created_at.isoformat(),
        },
    }


@app.get("/admin/groups", dependencies=[Depends(verify_admin_token)])
async def list_groups():
    groups = await group_repo.list_all()
    return {
        "status": "ok",
        "groups": [
            {
                "group_id": g.group_id,
                "workspace_id": g.workspace_id,
                "credential_configured": bool(g.bot_id),
                "name": g.name,
                "created_at": g.created_at.isoformat(),
            }
            for g in groups
        ],
    }


@app.delete("/admin/groups/{group_id}", dependencies=[Depends(verify_admin_token)])
async def delete_group(group_id: str):
    deleted = await group_repo.delete(group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Group not found")
    groupme_client.clear_cache(group_id)
    return {"status": "ok", "deleted": group_id}


@app.post("/admin/weekly-recap", dependencies=[Depends(verify_admin_token)])
async def trigger_weekly_recap():
    """Generate and send weekly recaps for all active groups."""
    if not settings.weekly_recap_enabled:
        return {"status": "disabled"}

    groups = await group_repo.list_all()
    sent = []
    failed = []

    for group in groups:
        recap = await beer_agent.generate_weekly_recap(group.group_id)
        if recap:
            delivered = await groupme_client.send_message(recap, group_id=group.group_id)
            if delivered:
                sent.append(group.group_id)
            else:
                failed.append(group.group_id)

    return {
        "status": "ok" if not failed else "partial",
        "recaps_sent": len(sent),
        "groups": sent,
        "failed_groups": failed,
    }
