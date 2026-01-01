#!/usr/bin/env python3
"""Categorize all GroupMe messages by their callback path.

Usage:
    # Scrape and categorize all messages
    uv run scripts/categorize_messages.py --scrape

    # Just categorize from existing data
    uv run scripts/categorize_messages.py --categorize

    # Show breakdown
    uv run scripts/categorize_messages.py --report

Environment variables:
    GROUPME_ACCESS_TOKEN: GroupMe API token (for --scrape)
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beerbot.models import DrinkType
from beerbot.services import MessageParser

DATA_DIR = Path(__file__).parent.parent / "data"
ALL_MESSAGES_FILE = DATA_DIR / "all_messages.json"
CATEGORIES_FILE = DATA_DIR / "results" / "message_categories.json"

GROUPME_API = "https://api.groupme.com/v3"
GROUP_ID = "112172155"


async def fetch_all_messages(token: str) -> list[dict]:
    """Fetch all messages from the group."""
    messages = []
    before_id = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            params = {"token": token, "limit": 100}
            if before_id:
                params["before_id"] = before_id

            url = f"{GROUPME_API}/groups/{GROUP_ID}/messages"
            print(f"Fetching messages... (before_id={before_id}, total: {len(messages)})")

            resp = await client.get(url, params=params)

            if resp.status_code == 304:
                break

            resp.raise_for_status()
            data = resp.json()

            batch = data.get("response", {}).get("messages", [])
            if not batch:
                break

            messages.extend(batch)
            before_id = batch[-1]["id"]
            await asyncio.sleep(0.1)

    print(f"Fetched {len(messages)} total messages")
    return messages


def categorize_message(msg: dict, parser: MessageParser) -> dict:
    """Categorize a single message by callback path.

    Categories:
    - bot: Message from bot (ignored)
    - system: System message (ignored)
    - command: Matches a !command pattern
    - drink_removal: Matches -N drinks pattern
    - drink_text: Text triggers drink logging (emoji, +N, cheers, etc.)
    - drink_ai_candidate: Would trigger AI parsing (signal words, regex missed)
    - image_only: Has image but no/minimal text
    - image_with_text: Has image AND text
    - link_share: Just a URL/link
    - something_else: None of the above (conversation)
    """
    text = msg.get("text") or ""
    sender_type = msg.get("sender_type", "user")
    attachments = msg.get("attachments", [])

    has_image = any(a.get("type") == "image" for a in attachments)
    has_mention = any(a.get("type") == "mentions" for a in attachments)

    # Check sender type first
    if sender_type == "bot":
        return {"category": "bot", "subcategory": None}
    if sender_type == "system":
        return {"category": "system", "subcategory": None}

    # Check for commands
    command = parser.parse_command(text)
    if command:
        return {"category": "command", "subcategory": command}

    # Check for drink removal (-N drinks)
    removal = parser.parse_drink_removal(text)
    if removal:
        count, drink_type = removal
        return {"category": "drink_removal", "subcategory": drink_type.value, "count": count}

    # Check for drink logging (text triggers)
    drink_count, drink_type = parser.parse_drink(text)
    if drink_count > 0:
        return {"category": "drink_text", "subcategory": drink_type.value, "count": drink_count}

    # Check if would trigger AI parsing
    if parser.should_try_ai_parsing(text):
        return {"category": "drink_ai_candidate", "subcategory": None}

    # Image handling
    if has_image:
        if len(text) < 20:
            return {"category": "image_only", "subcategory": None}
        else:
            return {"category": "image_with_text", "subcategory": None}

    # Link share (just a URL)
    if text.startswith(("http://", "https://")) and " " not in text.strip():
        return {"category": "link_share", "subcategory": None}

    # Everything else is conversation
    return {"category": "something_else", "subcategory": None}


def categorize_all(messages: list[dict]) -> list[dict]:
    """Categorize all messages."""
    parser = MessageParser()
    results = []

    for msg in messages:
        cat_info = categorize_message(msg, parser)
        results.append({
            "message_id": msg["id"],
            "text": msg.get("text"),
            "user_id": msg.get("user_id"),
            "user_name": msg.get("name"),
            "sender_type": msg.get("sender_type"),
            "created_at": msg.get("created_at"),
            "has_image": any(a.get("type") == "image" for a in msg.get("attachments", [])),
            "category": cat_info["category"],
            "subcategory": cat_info.get("subcategory"),
            "count": cat_info.get("count"),
        })

    return results


def print_report(categories: list[dict]):
    """Print a detailed breakdown report."""
    total = len(categories)

    # Count by category
    cat_counts = Counter(c["category"] for c in categories)

    print("\n" + "=" * 70)
    print("MESSAGE CATEGORY BREAKDOWN")
    print("=" * 70)
    print(f"Total messages: {total}")
    print()

    # Order for display
    category_order = [
        "bot", "system", "command", "drink_removal", "drink_text",
        "drink_ai_candidate", "image_only", "image_with_text",
        "link_share", "something_else"
    ]

    category_labels = {
        "bot": "Bot messages (ignored)",
        "system": "System messages (ignored)",
        "command": "Commands (!stats, !help, etc.)",
        "drink_removal": "Drink removal (-N drinks)",
        "drink_text": "Drink logging (text triggers)",
        "drink_ai_candidate": "AI parsing candidates (signal words)",
        "image_only": "Image only (analyzed for drinks)",
        "image_with_text": "Image with text",
        "link_share": "Link shares (URLs)",
        "something_else": "Conversation (potential for sassy replies)",
    }

    print("BY CATEGORY:")
    print("-" * 70)
    for cat in category_order:
        count = cat_counts.get(cat, 0)
        pct = count / total * 100 if total > 0 else 0
        label = category_labels.get(cat, cat)
        print(f"  {label:50s} {count:5d} ({pct:5.1f}%)")

    # Subcategory breakdown for commands
    commands = [c for c in categories if c["category"] == "command"]
    if commands:
        print()
        print("COMMAND BREAKDOWN:")
        print("-" * 70)
        cmd_counts = Counter(c["subcategory"] for c in commands)
        for cmd, count in cmd_counts.most_common():
            print(f"  !{cmd:20s} {count:5d}")

    # Subcategory breakdown for drink_text
    drink_text = [c for c in categories if c["category"] == "drink_text"]
    if drink_text:
        print()
        print("DRINK TEXT TRIGGERS BY TYPE:")
        print("-" * 70)
        type_counts = Counter(c["subcategory"] for c in drink_text)
        for dt, count in type_counts.most_common():
            print(f"  {dt:20s} {count:5d}")

    # Sample "something_else" messages
    something_else = [c for c in categories if c["category"] == "something_else"]
    if something_else:
        print()
        print(f"SAMPLE 'SOMETHING ELSE' MESSAGES ({len(something_else)} total):")
        print("-" * 70)
        for msg in something_else[:20]:
            text = msg["text"] or ""
            if len(text) > 60:
                text = text[:57] + "..."
            user = msg.get("user_name", "unknown")
            print(f"  [{user:15s}] {text}")


def save_results(categories: list[dict]):
    """Save categorization results."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "results").mkdir(exist_ok=True)

    with open(CATEGORIES_FILE, "w") as f:
        json.dump({
            "categorized_at": datetime.now().isoformat(),
            "total_messages": len(categories),
            "results": categories,
        }, f, indent=2)
    print(f"\nSaved categorization to {CATEGORIES_FILE}")

    # Also save just "something_else" for AI analysis
    something_else = [c for c in categories if c["category"] == "something_else"]
    se_file = DATA_DIR / "results" / "something_else.json"
    with open(se_file, "w") as f:
        json.dump(something_else, f, indent=2)
    print(f"Saved {len(something_else)} 'something else' messages to {se_file}")


async def main():
    parser = argparse.ArgumentParser(description="Categorize GroupMe messages")
    parser.add_argument("--scrape", action="store_true", help="Scrape all messages first")
    parser.add_argument("--categorize", action="store_true", help="Categorize from saved data")
    parser.add_argument("--report", action="store_true", help="Show breakdown report")
    args = parser.parse_args()

    if not any([args.scrape, args.categorize, args.report]):
        parser.print_help()
        return

    # Scrape if requested
    if args.scrape:
        token = os.environ.get("GROUPME_ACCESS_TOKEN")
        if not token:
            print("Error: GROUPME_ACCESS_TOKEN not set")
            return

        messages = await fetch_all_messages(token)

        # Save raw messages
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(ALL_MESSAGES_FILE, "w") as f:
            json.dump({
                "scraped_at": datetime.now().isoformat(),
                "group_id": GROUP_ID,
                "total_messages": len(messages),
                "messages": messages,
            }, f, indent=2)
        print(f"Saved {len(messages)} messages to {ALL_MESSAGES_FILE}")

    # Categorize if requested (or if we just scraped)
    if args.categorize or args.scrape:
        if not ALL_MESSAGES_FILE.exists():
            print(f"Error: {ALL_MESSAGES_FILE} not found. Run with --scrape first.")
            return

        with open(ALL_MESSAGES_FILE) as f:
            data = json.load(f)
        messages = data["messages"]
        print(f"Loaded {len(messages)} messages")

        categories = categorize_all(messages)
        save_results(categories)
        print_report(categories)

    # Report only
    elif args.report:
        if not CATEGORIES_FILE.exists():
            print(f"Error: {CATEGORIES_FILE} not found. Run with --categorize first.")
            return

        with open(CATEGORIES_FILE) as f:
            data = json.load(f)
        print_report(data["results"])


if __name__ == "__main__":
    asyncio.run(main())
