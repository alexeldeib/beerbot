#!/usr/bin/env python3
"""Compare old (regex) vs new (regex + AI) text parsing.

Usage:
    # Run comparison on scraped text messages
    uv run scripts/compare_parsers.py

    # Limit to N messages (for testing)
    uv run scripts/compare_parsers.py --limit 100

    # Skip AI calls, only classify by heuristics
    uv run scripts/compare_parsers.py --dry-run

Environment variables:
    GEMINI_API_KEY: Google Gemini API key (for AI calls)
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beerbot.models import DrinkType
from beerbot.services import MessageParser
from beerbot.vision import AITextParser

DATA_DIR = Path(__file__).parent.parent / "data"
TEXT_MESSAGES_FILE = DATA_DIR / "text_messages.json"
RESULTS_DIR = DATA_DIR / "results"
COMPARISON_FILE = RESULTS_DIR / "comparison_results.json"


def classify_result(old_count: int, ai_triggered: bool, ai_count: int) -> str:
    """Classify the comparison result into a category."""
    if old_count > 0:
        return "regex_match"       # Regex found drink (no AI needed)
    elif ai_triggered and ai_count > 0:
        return "ai_detection"      # AI found drink that regex missed
    elif ai_triggered and ai_count == 0:
        return "ai_null"           # AI was tried but found nothing
    else:
        return "no_signal"         # No signal words, AI not tried


async def compare_message(
    msg: dict,
    parser: MessageParser,
    ai_parser: AITextParser,
    dry_run: bool = False
) -> dict:
    """Compare old and new parsing logic on a single message."""
    text = msg["text"]

    # Step 1: OLD regex-only parsing
    old_count, old_type = parser.parse_drink(text)

    # Step 2: Would AI be triggered?
    ai_triggered = (old_count == 0) and parser.should_try_ai_parsing(text)

    # Step 3: NEW AI parsing (if triggered and not dry run)
    ai_count, ai_type = 0, DrinkType.BEER
    if ai_triggered and not dry_run:
        try:
            ai_count, ai_type = await ai_parser.parse_drink_text(text)
        except Exception as e:
            print(f"  AI error for '{text[:50]}...': {e}")

    category = classify_result(old_count, ai_triggered, ai_count if not dry_run else 0)

    return {
        "message_id": msg["message_id"],
        "text": text,
        "user_name": msg.get("user_name"),
        "created_at": msg.get("created_at"),
        "old_result": {
            "count": old_count,
            "type": old_type.value
        },
        "ai_triggered": ai_triggered,
        "ai_result": {
            "count": ai_count,
            "type": ai_type.value
        } if ai_triggered else None,
        "category": category,
    }


async def run_comparison(messages: list[dict], dry_run: bool = False) -> list[dict]:
    """Run comparison on all messages."""
    parser = MessageParser()
    ai_parser = AITextParser()

    results = []
    ai_call_count = 0

    for i, msg in enumerate(messages):
        if (i + 1) % 100 == 0 or i == 0:
            print(f"Processing message {i + 1}/{len(messages)}...")

        result = await compare_message(msg, parser, ai_parser, dry_run)
        results.append(result)

        # Rate limit AI calls
        if result["ai_triggered"] and not dry_run:
            ai_call_count += 1
            if ai_call_count % 10 == 0:
                print(f"  Made {ai_call_count} AI calls...")
            await asyncio.sleep(0.3)  # Rate limit

    print(f"\nProcessed {len(messages)} messages, made {ai_call_count} AI calls")
    return results


def save_results(results: list[dict]):
    """Save comparison results to JSON."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Save full results
    with open(COMPARISON_FILE, "w") as f:
        json.dump({
            "run_at": datetime.now().isoformat(),
            "total_messages": len(results),
            "results": results,
        }, f, indent=2)
    print(f"\nSaved full results to {COMPARISON_FILE}")

    # Also save just AI detections for easy review
    ai_detections = [r for r in results if r["category"] == "ai_detection"]
    if ai_detections:
        ai_detections_file = RESULTS_DIR / "ai_detections.json"
        with open(ai_detections_file, "w") as f:
            json.dump(ai_detections, f, indent=2)
        print(f"Saved {len(ai_detections)} AI detections to {ai_detections_file}")


def print_summary(results: list[dict]):
    """Print a summary of comparison results."""
    total = len(results)

    # Count by category
    categories = {}
    for r in results:
        cat = r["category"]
        categories[cat] = categories.get(cat, 0) + 1

    print("\n" + "=" * 60)
    print("COMPARISON SUMMARY")
    print("=" * 60)
    print(f"Total messages analyzed: {total}")
    print()
    print("By category:")
    for cat in ["regex_match", "ai_detection", "ai_null", "no_signal"]:
        count = categories.get(cat, 0)
        pct = count / total * 100 if total > 0 else 0
        print(f"  {cat:20s}: {count:5d} ({pct:5.1f}%)")

    # Count AI detections by drink type
    ai_detections = [r for r in results if r["category"] == "ai_detection"]
    if ai_detections:
        print()
        print(f"AI detections by drink type (n={len(ai_detections)}):")
        type_counts = {}
        for r in ai_detections:
            dt = r["ai_result"]["type"]
            type_counts[dt] = type_counts.get(dt, 0) + 1
        for dt, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            pct = count / len(ai_detections) * 100
            print(f"  {dt:12s}: {count:3d} ({pct:5.1f}%)")

        print()
        print("Sample AI detections (first 10):")
        for r in ai_detections[:10]:
            text = r["text"]
            if len(text) > 60:
                text = text[:57] + "..."
            ai = r["ai_result"]
            print(f"  \"{text}\" -> {ai['type']} ({ai['count']})")


async def main():
    parser = argparse.ArgumentParser(description="Compare old vs new text parsing")
    parser.add_argument("--limit", type=int, help="Limit to N messages")
    parser.add_argument("--dry-run", action="store_true",
                       help="Skip AI calls, only classify by heuristics")
    args = parser.parse_args()

    # Load text messages
    if not TEXT_MESSAGES_FILE.exists():
        print(f"Error: {TEXT_MESSAGES_FILE} not found")
        print("Run 'uv run scripts/scrape_text_messages.py --scrape' first")
        return

    with open(TEXT_MESSAGES_FILE) as f:
        messages = json.load(f)

    print(f"Loaded {len(messages)} text messages")

    if args.limit:
        messages = messages[:args.limit]
        print(f"Limited to {len(messages)} messages")

    # Check for API key if not dry run
    if not args.dry_run:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Error: GEMINI_API_KEY not set")
            print("Use --dry-run to skip AI calls")
            return

    # Run comparison
    results = await run_comparison(messages, dry_run=args.dry_run)

    # Save and summarize
    save_results(results)
    print_summary(results)


if __name__ == "__main__":
    asyncio.run(main())
