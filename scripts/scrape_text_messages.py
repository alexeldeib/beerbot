#!/usr/bin/env python3
"""Scrape text messages from GroupMe for parser comparison testing.

Usage:
    # Scrape all text messages
    uv run scripts/scrape_text_messages.py --scrape

    # Incremental scrape (only new since last scrape)
    uv run scripts/scrape_text_messages.py --scrape --incremental

Environment variables:
    GROUPME_ACCESS_TOKEN: GroupMe API token
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).parent.parent / "data"
TEXT_MESSAGES_FILE = DATA_DIR / "text_messages.json"
TEXT_METADATA_FILE = DATA_DIR / "text_metadata.json"

GROUPME_API = "https://api.groupme.com/v3"
GROUP_ID = "112180555"  # AI-only mode test group


def load_last_message_id() -> str | None:
    """Load the last processed message ID from text metadata."""
    if not TEXT_METADATA_FILE.exists():
        return None
    try:
        with open(TEXT_METADATA_FILE) as f:
            metadata = json.load(f)
            return metadata.get("last_message_id")
    except (json.JSONDecodeError, KeyError):
        return None


async def fetch_all_messages(token: str, incremental: bool = False) -> list[dict]:
    """Fetch messages from the group.

    Args:
        token: GroupMe API token
        incremental: If True, only fetch messages newer than last scrape
    """
    messages = []
    before_id = None
    stop_at_id = load_last_message_id() if incremental else None

    if incremental and stop_at_id:
        print(f"Incremental mode: fetching messages since {stop_at_id}")
    elif incremental:
        print("Incremental mode: no previous scrape found, fetching all")

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            params = {"token": token, "limit": 100}
            if before_id:
                params["before_id"] = before_id

            url = f"{GROUPME_API}/groups/{GROUP_ID}/messages"
            print(f"Fetching messages... (before_id={before_id}, total so far: {len(messages)})")

            resp = await client.get(url, params=params)

            if resp.status_code == 304:
                # No more messages
                break

            resp.raise_for_status()
            data = resp.json()

            batch = data.get("response", {}).get("messages", [])
            if not batch:
                break

            # In incremental mode, stop when we reach the last processed message
            if stop_at_id:
                new_batch = []
                for msg in batch:
                    if msg["id"] == stop_at_id:
                        # Found our stopping point
                        messages.extend(new_batch)
                        print("Reached last scraped message, stopping")
                        print(f"Fetched {len(messages)} new messages")
                        return messages
                    new_batch.append(msg)
                batch = new_batch

            messages.extend(batch)
            before_id = batch[-1]["id"]

            # Small delay to be nice to the API
            await asyncio.sleep(0.1)

    print(f"Fetched {len(messages)} total messages")
    return messages


def extract_text_messages(messages: list[dict]) -> list[dict]:
    """Extract messages with non-empty text for parser testing.

    Filters out:
    - Bot messages (to avoid testing on our own output)
    - System messages
    - Empty text messages
    """
    text_messages = []

    for msg in messages:
        # Skip bot and system messages
        sender_type = msg.get("sender_type", "")
        if sender_type in ("bot", "system"):
            continue

        # Skip empty text
        text = msg.get("text")
        if not text:
            continue

        text_messages.append({
            "message_id": msg["id"],
            "text": text,
            "user_id": msg.get("user_id"),
            "user_name": msg.get("name"),
            "created_at": msg.get("created_at"),
            "sender_type": sender_type,
            # Include attachments for context (mentions affect parsing)
            "attachments": msg.get("attachments", []),
        })

    print(f"Found {len(text_messages)} text messages from users")
    return text_messages


def save_text_messages(text_messages: list[dict], all_messages: list[dict]):
    """Save text messages and metadata."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing messages for merging in incremental mode
    existing_messages = []
    if TEXT_MESSAGES_FILE.exists():
        try:
            with open(TEXT_MESSAGES_FILE) as f:
                existing_messages = json.load(f)
        except (json.JSONDecodeError, KeyError):
            pass

    # Merge (new first, then existing without duplicates)
    existing_ids = {msg.get("message_id") for msg in text_messages}
    merged = text_messages + [msg for msg in existing_messages if msg.get("message_id") not in existing_ids]

    # Save messages
    with open(TEXT_MESSAGES_FILE, "w") as f:
        json.dump(merged, f, indent=2)
    print(f"Saved {len(merged)} text messages to {TEXT_MESSAGES_FILE}")

    # Save metadata
    last_message_id = None
    if all_messages:
        last_message_id = all_messages[0]["id"]  # Newest first
    elif merged:
        last_message_id = merged[0].get("message_id")

    metadata = {
        "scraped_at": datetime.now().isoformat(),
        "group_id": GROUP_ID,
        "total_messages": len(merged),
        "last_message_id": last_message_id,
    }

    with open(TEXT_METADATA_FILE, "w") as f:
        json.dump(metadata, f, indent=2)
    print(f"Saved metadata to {TEXT_METADATA_FILE}")
    if last_message_id:
        print(f"Last message ID: {last_message_id} (for incremental scraping)")


async def main():
    parser = argparse.ArgumentParser(description="Scrape GroupMe text messages for parser testing")
    parser.add_argument("--scrape", action="store_true", help="Scrape text messages")
    parser.add_argument("--incremental", action="store_true",
                       help="Only fetch new messages since last scrape")
    args = parser.parse_args()

    if not args.scrape:
        parser.print_help()
        return

    token = os.environ.get("GROUPME_ACCESS_TOKEN")
    if not token:
        print("Error: GROUPME_ACCESS_TOKEN not set")
        print("Add it to .env.local or export it")
        return

    messages = await fetch_all_messages(token, incremental=args.incremental)
    text_messages = extract_text_messages(messages)
    save_text_messages(text_messages, messages)


if __name__ == "__main__":
    asyncio.run(main())
