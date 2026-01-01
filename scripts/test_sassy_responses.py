#!/usr/bin/env python3
"""Test the sassy responder on 'something else' messages.

Usage:
    # Test on a sample of messages
    uv run scripts/test_sassy_responses.py --sample 20

    # Test on specific message text
    uv run scripts/test_sassy_responses.py --text "So what I'm asking is halftime counts right?"

Environment variables:
    GEMINI_API_KEY: Google Gemini API key
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

DATA_DIR = Path(__file__).parent.parent / "data"
SOMETHING_ELSE_FILE = DATA_DIR / "results" / "something_else.json"


async def test_single_message(text: str, user_name: str = "TestUser"):
    """Test sassy responder on a single message."""
    from beerbot.vision import SassyResponder

    responder = SassyResponder()
    if not responder.client:
        print("Error: GEMINI_API_KEY not set")
        return

    print(f"\nMessage: \"{text}\"")
    print(f"Sender: {user_name}")
    print("-" * 60)

    response = await responder.maybe_respond(text, user_name)
    if response:
        print(f"Response: {response}")
    else:
        print("Response: (no response - not interesting)")


async def test_sample_messages(sample_size: int):
    """Test sassy responder on a sample of 'something else' messages."""
    from beerbot.vision import SassyResponder

    responder = SassyResponder()
    if not responder.client:
        print("Error: GEMINI_API_KEY not set")
        return

    # Load messages
    if not SOMETHING_ELSE_FILE.exists():
        print(f"Error: {SOMETHING_ELSE_FILE} not found")
        print("Run 'uv run scripts/categorize_messages.py --scrape' first")
        return

    with open(SOMETHING_ELSE_FILE) as f:
        messages = json.load(f)

    print(f"Loaded {len(messages)} 'something else' messages")
    print(f"Testing sample of {sample_size}")
    print("=" * 70)

    # Sample random messages
    import random
    sample = random.sample(messages, min(sample_size, len(messages)))

    interesting_count = 0
    results = []

    for i, msg in enumerate(sample, 1):
        text = msg.get("text", "")
        user_name = msg.get("user_name", "Unknown")

        print(f"\n[{i}/{sample_size}] {user_name}: \"{text[:80]}{'...' if len(text) > 80 else ''}\"")

        response = await responder.maybe_respond(text, user_name)
        if response:
            print(f"  -> {response}")
            interesting_count += 1
            results.append({
                "text": text,
                "user_name": user_name,
                "response": response,
            })
        else:
            print("  -> (not interesting)")

        # Rate limit
        await asyncio.sleep(0.5)

    print("\n" + "=" * 70)
    print(f"SUMMARY: {interesting_count}/{sample_size} messages got sassy responses ({interesting_count/sample_size*100:.0f}%)")

    # Save results
    results_file = DATA_DIR / "results" / "sassy_test_results.json"
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Saved {len(results)} responses to {results_file}")


async def main():
    parser = argparse.ArgumentParser(description="Test sassy responder")
    parser.add_argument("--sample", type=int, help="Test on N random messages")
    parser.add_argument("--text", type=str, help="Test on specific message text")
    parser.add_argument("--user", type=str, default="TestUser", help="User name for --text")
    args = parser.parse_args()

    # Check for API key
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not set")
        return

    if args.text:
        await test_single_message(args.text, args.user)
    elif args.sample:
        await test_sample_messages(args.sample)
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
