#!/usr/bin/env python3
"""Evaluate UnifiedBeerBot against regression tests and scraped messages.

Usage:
    # Run regression tests
    uv run scripts/eval_unified.py --regression

    # Evaluate on latest scraped messages
    uv run scripts/eval_unified.py --messages N
"""

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
load_dotenv(".env.local", override=True)

from beerbot.vision import unified_beer_bot

DATA_DIR = Path(__file__).parent.parent / "data"
REGRESSION_FILE = DATA_DIR / "regression_tests.json"
MESSAGES_FILE = DATA_DIR / "text_messages.json"


async def run_regression_tests() -> dict:
    """Run regression tests and report results."""
    with open(REGRESSION_FILE) as f:
        tests = json.load(f)

    results = {
        "total": len(tests),
        "passed": 0,
        "failed": 0,
        "failures": [],
    }

    print("=" * 70)
    print("REGRESSION TESTS - UnifiedBeerBot")
    print("=" * 70)
    print()

    for test in tests:
        result = await unified_beer_bot.respond(
            test["text"],
            test["user_name"],
            group_id=None,
            skip_cooldown=True,
        )

        # Check action match (answer/respond are equivalent - both send a reply)
        expected = test["expected_action"]
        got = result["action"]
        if expected in ("answer", "respond") and got in ("answer", "respond"):
            action_match = True  # Both result in reply being sent
        else:
            action_match = got == expected

        # Check drink match (if applicable)
        drink_match = True
        if test["expected_drink"]:
            if result["drink"]:
                drink_match = (
                    result["drink"]["count"] == test["expected_drink"]["count"]
                    and result["drink"]["type"] == test["expected_drink"]["type"]
                )
            else:
                drink_match = False
        elif result["drink"]:
            drink_match = False

        passed = action_match and drink_match

        if passed:
            results["passed"] += 1
            status = "✓"
        else:
            results["failed"] += 1
            status = "✗"
            results["failures"].append({
                "id": test["id"],
                "text": test["text"],
                "expected_action": test["expected_action"],
                "expected_drink": test["expected_drink"],
                "got_action": result["action"],
                "got_drink": result["drink"],
                "reasoning": result["reasoning"],
            })

        # Print result
        drink_str = ""
        if result["drink"]:
            drink_str = f" [{result['drink']['count']} {result['drink']['type']}]"
        expected_drink_str = ""
        if test["expected_drink"]:
            expected_drink_str = f" [{test['expected_drink']['count']} {test['expected_drink']['type']}]"

        print(f"{status} {test['id']}: {test['text'][:40]:<40}")
        if not passed:
            print(f"   Expected: {test['expected_action']}{expected_drink_str}")
            print(f"   Got:      {result['action']}{drink_str}")
            print(f"   Reason:   {result['reasoning']}")
        print()

    # Summary
    print("=" * 70)
    print(f"RESULTS: {results['passed']}/{results['total']} passed ({100*results['passed']/results['total']:.1f}%)")
    print("=" * 70)

    if results["failures"]:
        print(f"\nFailed tests: {len(results['failures'])}")
        for f in results["failures"]:
            print(f"  - {f['id']}: {f['text'][:30]}...")

    return results


async def evaluate_messages(n: int) -> dict:
    """Evaluate unified bot on latest N scraped messages."""
    with open(MESSAGES_FILE) as f:
        messages = json.load(f)

    # Get latest N messages
    latest = sorted(messages, key=lambda m: m.get("created_at", 0), reverse=True)[:n]

    results = {
        "total": len(latest),
        "actions": {"log_drink": 0, "answer": 0, "respond": 0, "ignore": 0},
        "with_reply": 0,
    }

    print("=" * 70)
    print(f"UNIFIED BOT EVALUATION - Last {n} messages")
    print("=" * 70)
    print()

    for i, msg in enumerate(latest, 1):
        result = await unified_beer_bot.respond(
            msg["text"],
            msg.get("user_name", "Unknown"),
            group_id="112180555",
            skip_cooldown=True,
        )

        results["actions"][result["action"]] += 1
        if result["reply"]:
            results["with_reply"] += 1

        # Print result
        text_display = (msg["text"] or "")[:45]
        drink_str = ""
        if result["drink"]:
            drink_str = f" [{result['drink']['count']} {result['drink']['type']}]"
        reply_str = ""
        if result["reply"]:
            reply_str = f"\n      Reply: {result['reply'][:50]}..."

        print(f"{i:2}. {result['action']:<10}{drink_str:<15} | {text_display}{reply_str}")

    # Summary
    print()
    print("=" * 70)
    print("DISTRIBUTION:")
    for action, count in results["actions"].items():
        pct = 100 * count / results["total"] if results["total"] > 0 else 0
        print(f"  {action:<12}: {count:3} ({pct:5.1f}%)")
    print(f"  with_reply  : {results['with_reply']:3} ({100*results['with_reply']/results['total']:.1f}%)")
    print("=" * 70)

    return results


async def main():
    parser = argparse.ArgumentParser(description="Evaluate UnifiedBeerBot")
    parser.add_argument("--regression", action="store_true", help="Run regression tests")
    parser.add_argument("--messages", type=int, metavar="N", help="Evaluate on N latest messages")
    args = parser.parse_args()

    if args.regression:
        await run_regression_tests()
    elif args.messages:
        await evaluate_messages(args.messages)
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
