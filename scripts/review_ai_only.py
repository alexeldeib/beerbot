#!/usr/bin/env python3
"""Review classifier performance on AI-only group messages."""

import json
import asyncio
from pathlib import Path

from beerbot.vision import message_classifier


async def main():
    # Load scraped messages
    with open("data/text_messages.json") as f:
        messages = json.load(f)

    # Get latest 20 messages (sorted by created_at descending)
    latest = sorted(messages, key=lambda m: m.get("created_at", 0), reverse=True)[:20]

    print("=" * 80)
    print("Last 20 messages from AI-only group 112180555")
    print("=" * 80)
    print()

    for i, msg in enumerate(latest, 1):
        text = msg["text"] if msg["text"] else ""
        text_display = text[:55] + "..." if len(text) > 55 else text
        user = msg.get("user_name", "Unknown")

        # Classify with ai_only=True to test the AI-only prompt
        result = await message_classifier.classify(
            msg["text"],
            user,
            group_id="112180555",
            skip_cooldown=True,
            ai_only=True
        )

        cls_type = result["type"]
        reason = result.get("reason", "")[:40]
        drink_info = ""
        if cls_type == "drink":
            drink_info = f" [{result.get('drink_count', 1)} {result.get('drink_type', 'beer')}]"

        print(f"{i:2}. {user[:12]:<12} | {cls_type:<8}{drink_info:<15} | {text_display}")
        if reason and cls_type not in ("ignore", "default"):
            print(f"    reason: {reason}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
