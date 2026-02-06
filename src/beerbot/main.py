"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .agent import beer_agent
from .config import settings
from .database import close_pool, init_db
from .groupme_client import groupme_client
from .models import GroupMeMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
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


@app.post("/callback")
async def groupme_callback(request: Request):
    """Handle incoming GroupMe webhook callbacks."""
    try:
        data = await request.json()
        message = GroupMeMessage(**data)
    except Exception:
        return JSONResponse({"error": "Invalid message format"}, status_code=400)

    if message.sender_type == "bot":
        return {"status": "ignored", "reason": "bot message"}

    reply = await beer_agent.process_message(message)

    if reply:
        await groupme_client.send_message(reply, group_id=message.group_id)
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
    if parts[1] != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid admin token")


@app.post("/admin/groups", dependencies=[Depends(verify_admin_token)])
async def register_group(registration: GroupRegistration):
    from .repositories import group_repo

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
            "bot_id": group.bot_id,
            "name": group.name,
            "created_at": group.created_at.isoformat(),
        },
    }


@app.get("/admin/groups", dependencies=[Depends(verify_admin_token)])
async def list_groups():
    from .repositories import group_repo

    groups = await group_repo.list_all()
    return {
        "status": "ok",
        "groups": [
            {
                "group_id": g.group_id,
                "bot_id": g.bot_id,
                "name": g.name,
                "created_at": g.created_at.isoformat(),
            }
            for g in groups
        ],
    }


@app.delete("/admin/groups/{group_id}", dependencies=[Depends(verify_admin_token)])
async def delete_group(group_id: str):
    from .repositories import group_repo

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

    from .repositories import group_repo

    groups = await group_repo.list_all()
    sent = []

    for group in groups:
        recap = await beer_agent.generate_weekly_recap(group.group_id)
        if recap:
            await groupme_client.send_message(recap, group_id=group.group_id)
            sent.append(group.group_id)

    return {"status": "ok", "recaps_sent": len(sent), "groups": sent}
