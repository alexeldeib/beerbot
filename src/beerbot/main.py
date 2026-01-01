"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Set DEBUG for vision module to see image fetch details
logging.getLogger("src.beerbot.vision").setLevel(logging.DEBUG)
from .database import close_pool, init_db
from .groupme_client import groupme_client
from .models import DrinkType, GroupMeMessage, VisionResult
from .services import (
    extract_mentioned_user_ids,
    extract_mentioned_users,
    message_parser,
    stats_service,
)
from .vision import ai_text_parser, sassy_responder, vision_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    await init_db()
    yield
    # Shutdown
    await close_pool()


app = FastAPI(
    title="Beerbot",
    description="GroupMe bot for tracking beer consumption",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """Health check endpoint for deployment platforms."""
    return {"status": "healthy"}


@app.post("/callback")
async def groupme_callback(request: Request):
    """Handle incoming GroupMe webhook callbacks."""
    try:
        data = await request.json()
        message = GroupMeMessage(**data)
    except Exception:
        return JSONResponse({"error": "Invalid message format"}, status_code=400)

    # Ignore messages from bots to prevent loops
    if message.sender_type == "bot":
        return {"status": "ignored", "reason": "bot message"}

    # Check for commands first
    command = message_parser.parse_command(message.text)
    if command:
        response_text = await handle_command(command, message)
        if response_text:
            await groupme_client.send_message(response_text, group_id=message.group_id)
        return {"status": "ok", "action": "command", "command": command}

    # Check for drink removal (-N drinks syntax)
    removal = message_parser.parse_drink_removal(message.text)
    if removal:
        removal_count, removal_type = removal
        # Check for mentioned user in attachments (remove from them, not sender)
        target_user_id = None
        for attachment in message.attachments:
            if attachment.type == "mentions" and attachment.user_ids:
                target_user_id = attachment.user_ids[0]
                break
        response_text = await stats_service.remove_drinks_by_type(
            message, removal_count, removal_type, target_user_id
        )
        await groupme_client.send_message(response_text, group_id=message.group_id)
        return {"status": "ok", "action": "removed", "drinks": removal_count, "drink_type": removal_type.value}

    # Check for drink logging (text triggers)
    text_drink_count, text_drink_type = message_parser.parse_drink(message.text)

    # AI fallback: if no drinks detected by regex but message has signal words
    if text_drink_count == 0 and message_parser.should_try_ai_parsing(message.text):
        try:
            ai_count, ai_type = await ai_text_parser.parse_drink_text(message.text)
            if ai_count > 0:
                text_drink_count = ai_count
                text_drink_type = ai_type
                logging.getLogger(__name__).info(
                    "AI parser detected drink: text=%r count=%d type=%s",
                    message.text[:50], ai_count, ai_type.value
                )
        except Exception:
            logging.getLogger(__name__).exception("AI text parsing failed")

    # Check for drink logging (image analysis)
    vision_result = VisionResult()
    if settings.image_analysis_enabled:
        vision_result = await vision_service.analyze_attachments(message.attachments)

    # Use the higher of text or image count (they represent the same drinks)
    # Prefer image drink type if image detected a drink, otherwise use text
    if vision_result.drink_count > 0:
        drink_count = max(text_drink_count, vision_result.drink_count)
        drink_type = vision_result.drink_type
    else:
        drink_count = text_drink_count
        drink_type = text_drink_type

    split_the_g = vision_result.split_the_g_count  # Only from images

    # Check if images were analyzed but no drinks found
    has_images = any(a.type == "image" for a in message.attachments)
    if has_images and drink_count == 0 and vision_result.analyzed:
        quip = await vision_service.generate_no_beer_quip()
        await groupme_client.send_message(quip, group_id=message.group_id)
        return {"status": "ok", "action": "quip", "reason": "no_drinks_in_image"}

    if drink_count > 0:
        # Extract mentioned users with their names
        mentioned_users = extract_mentioned_users(message.text, message.attachments)

        # Determine if sender should be included:
        # - If explicit assignment (+N beers) with mentions, only log for mentioned users
        # - Otherwise, include the sender (emoji, "cheers", etc.)
        is_assignment = message_parser.is_explicit_assignment(message.text)
        include_sender = not (is_assignment and mentioned_users)

        # Log drinks (returns None if duplicate message - idempotency)
        response_text = await stats_service.log_beers_for_users(
            message, drink_count, mentioned_users, include_sender, split_the_g, drink_type
        )
        if response_text:
            await groupme_client.send_message(response_text, group_id=message.group_id)

            # Occasionally add a sassy quip after logging drinks
            if settings.sassy_responses_enabled:
                import random
                if random.random() < settings.sassy_response_rate:
                    quip = await sassy_responder.generate_drink_quip(
                        message.text, message.name, drink_count, drink_type.value
                    )
                    if quip:
                        await groupme_client.send_message(quip, group_id=message.group_id)

            return {"status": "ok", "action": "logged", "drinks": drink_count, "drink_type": drink_type.value, "split_the_g": split_the_g}
        else:
            return {"status": "ok", "action": "duplicate", "message_id": message.id}

    # Check for sassy response opportunity (for "something else" messages)
    # Let AI decide what's interesting - no probability filter here
    # Pass group_id so AI can reference actual leaderboard data
    if settings.sassy_responses_enabled and message.text:
        sassy_reply = await sassy_responder.maybe_respond(
            message.text, message.name, group_id=message.group_id
        )
        if sassy_reply:
            await groupme_client.send_message(sassy_reply, group_id=message.group_id)
            return {"status": "ok", "action": "sassy_reply"}

    # No action needed
    return {"status": "ok", "action": "none"}


async def handle_command(command: str, message: GroupMeMessage) -> str | None:
    """Handle a command and return response text."""
    import logging
    logger = logging.getLogger(__name__)

    try:
        return await _handle_command_inner(command, message)
    except Exception:
        logger.exception("Error handling command: %s", command)
        return "Oops! Something went wrong processing that command."


async def _handle_command_inner(command: str, message: GroupMeMessage) -> str | None:
    """Inner command handler (separated for error handling)."""
    match command:
        case "stats":
            return await stats_service.get_group_stats(message.group_id)
        case "mystats":
            return await stats_service.get_user_stats(message)
        case "leaderboard":
            # Parse optional drink type filter
            drink_filter = message_parser.parse_stats_filter(message.text, "leaderboard")
            return await stats_service.get_leaderboard(message.group_id, drink_filter)
        case "today":
            # Parse optional drink type filter
            drink_filter = message_parser.parse_stats_filter(message.text, "today")
            return await stats_service.get_today_stats(message.group_id, drink_filter)
        case "week":
            # Parse optional drink type filter
            drink_filter = message_parser.parse_stats_filter(message.text, "week")
            return await stats_service.get_week_stats(message.group_id, drink_filter)
        case "undo":
            # Check for mentioned user in attachments
            target_user_id = None
            for attachment in message.attachments:
                if attachment.type == "mentions" and attachment.user_ids:
                    target_user_id = attachment.user_ids[0]
                    break
            return await stats_service.undo_beer(message, target_user_id)
        case "unbeer":
            # Parse the quantity to remove (defaults to 1)
            quantity = message_parser.parse_unbeer_count(message.text)
            # Check for mentioned user in attachments
            target_user_id = None
            for attachment in message.attachments:
                if attachment.type == "mentions" and attachment.user_ids:
                    target_user_id = attachment.user_ids[0]
                    break
            return await stats_service.unbeer(message, quantity, target_user_id)
        case "million":
            # Parse optional drink type filter
            drink_filter = message_parser.parse_million_filter(message.text)
            return await stats_service.get_million_countdown(message.group_id, drink_filter)
        case "splitg":
            return await stats_service.get_split_g_leaderboard(message.group_id)
        case "split":
            # Check for mentioned user
            mentioned = extract_mentioned_users(message.text, message.attachments)
            if mentioned:
                target_user_id, target_name = mentioned[0]
                return await stats_service.add_split(message, target_user_id, target_name)
            return await stats_service.add_split(message)
        case "unsplit":
            # Parse the quantity to remove (defaults to 1)
            quantity = message_parser.parse_unsplit_count(message.text)
            # Check for mentioned user in attachments
            target_user_id = None
            for attachment in message.attachments:
                if attachment.type == "mentions" and attachment.user_ids:
                    target_user_id = attachment.user_ids[0]
                    break
            return await stats_service.unsplit(message, quantity, target_user_id)
        case "owe":
            # Parse the amount (defaults to 1) and get mentioned user with name
            amount = message_parser.parse_debt_amount(message.text)
            mentioned = extract_mentioned_users(message.text, message.attachments)
            if not mentioned:
                return "Please mention who owes: !owe @user or !owe N @user"
            debtor_user_id, debtor_name = mentioned[0]
            return await stats_service.add_debt(message, amount, debtor_user_id, debtor_name)
        case "forgive":
            # Parse the amount (defaults to 1) and get mentioned user (defaults to sender)
            amount = message_parser.parse_debt_amount(message.text)
            mentioned = extract_mentioned_users(message.text, message.attachments)
            if mentioned:
                debtor_user_id, debtor_name = mentioned[0]
            else:
                debtor_user_id, debtor_name = message.user_id, message.name
            return await stats_service.forgive_debt(message, amount, debtor_user_id, debtor_name)
        case "debts":
            return await stats_service.get_debt_leaderboard(message.group_id)
        case "help":
            return stats_service.get_help()
        case "toast":
            return await vision_service.generate_toast()
        case _:
            return None


# --- Admin Endpoints ---

class GroupRegistration(BaseModel):
    """Request body for registering a group."""

    group_id: str
    bot_id: str
    name: str | None = None


async def verify_admin_token(authorization: str | None = Header(None)) -> None:
    """Verify the admin token from Authorization header."""
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="Admin endpoints not configured")

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    # Expect "Bearer <token>" format
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization format")

    if parts[1] != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid admin token")


@app.post("/admin/groups", dependencies=[Depends(verify_admin_token)])
async def register_group(registration: GroupRegistration):
    """Register a new group with its bot_id mapping."""
    from .repositories import group_repo

    group = await group_repo.register(
        group_id=registration.group_id,
        bot_id=registration.bot_id,
        name=registration.name,
    )

    # Clear cache so new bot_id is used immediately
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
    """List all registered groups."""
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
    """Delete a group registration."""
    from .repositories import group_repo

    deleted = await group_repo.delete(group_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Group not found")

    # Clear cache
    groupme_client.clear_cache(group_id)

    return {"status": "ok", "deleted": group_id}
