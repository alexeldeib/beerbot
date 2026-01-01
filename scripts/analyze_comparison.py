#!/usr/bin/env python3
"""Analyze comparison results and generate a report.

Usage:
    # Generate markdown report
    uv run scripts/analyze_comparison.py

    # Output to specific file
    uv run scripts/analyze_comparison.py --output report.md

No API keys required - just analyzes existing comparison_results.json
"""

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
RESULTS_DIR = DATA_DIR / "results"
COMPARISON_FILE = RESULTS_DIR / "comparison_results.json"
REPORT_FILE = RESULTS_DIR / "comparison_report.md"


def load_results() -> dict:
    """Load comparison results from JSON."""
    if not COMPARISON_FILE.exists():
        print(f"Error: {COMPARISON_FILE} not found")
        print("Run 'uv run scripts/compare_parsers.py' first")
        return None

    with open(COMPARISON_FILE) as f:
        return json.load(f)


def generate_report(data: dict) -> str:
    """Generate markdown report from comparison results."""
    results = data["results"]
    total = len(results)

    # Category counts
    categories = Counter(r["category"] for r in results)

    # AI detections breakdown
    ai_detections = [r for r in results if r["category"] == "ai_detection"]
    ai_null = [r for r in results if r["category"] == "ai_null"]
    ai_triggered = ai_detections + ai_null

    # Drink type breakdown for AI detections
    ai_type_counts = Counter(
        r["ai_result"]["type"] for r in ai_detections if r["ai_result"]
    )

    # Regex match breakdown
    regex_matches = [r for r in results if r["category"] == "regex_match"]
    regex_type_counts = Counter(r["old_result"]["type"] for r in regex_matches)

    # Build report
    lines = [
        "# Text Parser Comparison Report",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Comparison run: {data.get('run_at', 'unknown')}",
        "",
        "## Summary",
        "",
        f"- **Total messages analyzed**: {total:,}",
        f"- **Regex matches** (no AI needed): {categories.get('regex_match', 0):,} ({categories.get('regex_match', 0)/total*100:.1f}%)",
        f"- **AI triggered**: {len(ai_triggered):,} ({len(ai_triggered)/total*100:.1f}%)",
        f"  - AI detections: {len(ai_detections):,} ({len(ai_detections)/len(ai_triggered)*100:.1f}% of triggered)" if ai_triggered else "  - AI detections: 0",
        f"  - AI null: {len(ai_null):,} ({len(ai_null)/len(ai_triggered)*100:.1f}% of triggered)" if ai_triggered else "  - AI null: 0",
        f"- **No signal words** (AI not tried): {categories.get('no_signal', 0):,} ({categories.get('no_signal', 0)/total*100:.1f}%)",
        "",
        "## AI Improvement Rate",
        "",
    ]

    # Calculate improvement
    if regex_matches or ai_detections:
        total_drinks = len(regex_matches) + len(ai_detections)
        improvement = len(ai_detections) / total_drinks * 100 if total_drinks > 0 else 0
        lines.extend([
            f"- Drinks detected by regex: {len(regex_matches):,}",
            f"- Additional drinks detected by AI: {len(ai_detections):,}",
            f"- **Improvement rate**: {improvement:.1f}% more drinks detected with AI fallback",
            "",
        ])

    # Drink type breakdown
    lines.extend([
        "## Drink Detection by Type",
        "",
        "### Regex Detections",
        "",
        "| Type | Count | % of Regex |",
        "|------|-------|------------|",
    ])
    for dt in ["beer", "wine", "cocktail", "claw"]:
        count = regex_type_counts.get(dt, 0)
        pct = count / len(regex_matches) * 100 if regex_matches else 0
        lines.append(f"| {dt} | {count:,} | {pct:.1f}% |")

    if ai_detections:
        lines.extend([
            "",
            "### AI Detections",
            "",
            "| Type | Count | % of AI Detections |",
            "|------|-------|-------------------|",
        ])
        for dt in ["beer", "wine", "cocktail", "claw"]:
            count = ai_type_counts.get(dt, 0)
            pct = count / len(ai_detections) * 100
            lines.append(f"| {dt} | {count:,} | {pct:.1f}% |")

    # Sample AI detections for manual review
    if ai_detections:
        lines.extend([
            "",
            "## Sample AI Detections (review for false positives)",
            "",
        ])
        for i, r in enumerate(ai_detections[:30], 1):
            text = r["text"]
            if len(text) > 80:
                text = text[:77] + "..."
            # Escape markdown
            text = text.replace("|", "\\|").replace("`", "\\`")
            ai = r["ai_result"]
            user = r.get("user_name", "unknown")
            lines.append(f"{i}. **{user}**: \"{text}\"")
            lines.append(f"   - Detected: {ai['type']} (count: {ai['count']})")
            lines.append("")

    # Messages where AI was triggered but returned null
    if ai_null:
        lines.extend([
            "",
            "## Sample AI Null Results (AI tried but found nothing)",
            "",
            "These messages had signal words but AI correctly determined no drink:",
            "",
        ])
        # Show first 20 examples
        for i, r in enumerate(ai_null[:20], 1):
            text = r["text"]
            if len(text) > 80:
                text = text[:77] + "..."
            text = text.replace("|", "\\|").replace("`", "\\`")
            lines.append(f"{i}. \"{text}\"")
        if len(ai_null) > 20:
            lines.append(f"\n... and {len(ai_null) - 20} more")

    # Patterns that could be added to regex
    if ai_detections:
        lines.extend([
            "",
            "## Potential Regex Patterns",
            "",
            "Based on AI detections, consider adding these patterns to `MessageParser`:",
            "",
        ])

        # Look for common patterns in AI detections
        patterns = Counter()
        keywords = Counter()
        for r in ai_detections:
            text = r["text"].lower()
            # Count words that appear frequently
            for word in ["finished", "had", "enjoying", "drinking", "sipping"]:
                if word in text:
                    patterns[word] = patterns.get(word, 0) + 1
            # Count drink names
            for dt in ["ipa", "lager", "stout", "margarita", "martini", "whiskey", "bourbon"]:
                if dt in text:
                    keywords[dt] = keywords.get(dt, 0) + 1

        if patterns:
            lines.append("### Common verbs in AI detections:")
            for word, count in patterns.most_common(10):
                lines.append(f"- \"{word}\": {count} occurrences")

        if keywords:
            lines.append("")
            lines.append("### Common drink names in AI detections:")
            for word, count in keywords.most_common(10):
                lines.append(f"- \"{word}\": {count} occurrences")

    # API cost estimate
    if ai_triggered:
        lines.extend([
            "",
            "## Cost Analysis",
            "",
            f"- AI calls made: {len(ai_triggered):,}",
            f"- Estimated Gemini Flash cost: ~${len(ai_triggered) * 0.00001:.4f} (at $0.01/1K requests)",
            "",
            "Note: This is a one-time analysis cost. In production, only ~17% of messages trigger AI.",
        ])

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Analyze comparison results")
    parser.add_argument("--output", "-o", type=str, help="Output file path")
    args = parser.parse_args()

    # Load results
    data = load_results()
    if not data:
        return

    print(f"Loaded {len(data['results']):,} comparison results")

    # Generate report
    report = generate_report(data)

    # Save or print
    output_path = Path(args.output) if args.output else REPORT_FILE
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report)
    print(f"Report saved to {output_path}")

    # Also print summary to console
    results = data["results"]
    categories = Counter(r["category"] for r in results)
    print("\nQuick Summary:")
    print(f"  Total messages: {len(results):,}")
    print(f"  Regex matches: {categories.get('regex_match', 0):,}")
    print(f"  AI detections: {categories.get('ai_detection', 0):,}")
    print(f"  AI null: {categories.get('ai_null', 0):,}")
    print(f"  No signal: {categories.get('no_signal', 0):,}")


if __name__ == "__main__":
    main()
