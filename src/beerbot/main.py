"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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
from .vision import vision_service


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
            await groupme_client.send_message(response_text)
        return {"status": "ok", "action": "command", "command": command}

    # Check for drink logging (text triggers)
    text_drink_count, text_drink_type = message_parser.parse_drink(message.text)

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
        await groupme_client.send_message(quip)
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
            await groupme_client.send_message(response_text)
            return {"status": "ok", "action": "logged", "drinks": drink_count, "drink_type": drink_type.value, "split_the_g": split_the_g}
        else:
            return {"status": "ok", "action": "duplicate", "message_id": message.id}

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
            return await stats_service.get_leaderboard(message.group_id)
        case "today":
            return await stats_service.get_today_stats(message.group_id)
        case "week":
            return await stats_service.get_week_stats(message.group_id)
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
        case "debts":
            return await stats_service.get_debt_leaderboard(message.group_id)
        case "help":
            return stats_service.get_help()
        case "toast":
            return await vision_service.generate_toast()
        case _:
            return None
