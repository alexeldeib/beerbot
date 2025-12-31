#!/usr/bin/env python3
"""Evaluate vision prompt against labeled dataset.

Usage:
    # Evaluate current production prompt
    uv run scripts/eval_prompt.py

    # Evaluate with verbose output
    uv run scripts/eval_prompt.py --verbose

    # Evaluate a custom prompt file
    uv run scripts/eval_prompt.py --prompt data/prompts/v1.txt

    # Save results to file
    uv run scripts/eval_prompt.py --output data/results/baseline.json

    # Initialize/migrate labels schema
    uv run scripts/eval_prompt.py --init

    # Evaluate only reviewed labels (for CI)
    uv run scripts/eval_prompt.py --reviewed-only

    # CI output mode (JSON summary to stdout)
    uv run scripts/eval_prompt.py --ci

    # Compare against baseline
    uv run scripts/eval_prompt.py --ci --baseline data/results/baseline_latest.json
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

DATA_DIR = Path(__file__).parent.parent / "data"
IMAGES_DIR = DATA_DIR / "images"
LABELS_FILE = DATA_DIR / "labels.json"
PROMPTS_DIR = DATA_DIR / "prompts"
RESULTS_DIR = DATA_DIR / "results"


@dataclass
class EvalResult:
    """Result for a single image evaluation."""

    filename: str
    ground_truth_type: Optional[str]
    ground_truth_split_g: bool
    predicted_type: Optional[str]
    predicted_split_g: bool
    type_correct: bool
    split_g_correct: bool
    raw_response: str = ""


@dataclass
class TypeMetrics:
    """Precision, recall, F1 for a drink type."""

    precision: float
    recall: float
    f1: float
    support: int  # Number of ground truth instances


@dataclass
class EvalMetrics:
    """Aggregate metrics from evaluation run."""

    total_images: int
    type_accuracy: float
    split_g_accuracy: float
    overall_accuracy: float  # Both correct

    per_type: dict  # {type: TypeMetrics}
    confusion_matrix: dict  # {actual: {predicted: count}}

    false_negatives: list  # Missed drinks
    false_positives: list  # Detected non-drinks
    wrong_type: list  # Correct detection, wrong type


@dataclass
class EvalRecord:
    """Full evaluation record for saving."""

    name: str
    timestamp: str
    prompt_source: str
    prompt_hash: str
    prompt_preview: str
    model: str
    metrics: dict
    results: list


def load_labels(reviewed_only: bool = False) -> dict:
    """Load ground truth labels.

    Args:
        reviewed_only: If True, only return labels with reviewed=True
    """
    with open(LABELS_FILE) as f:
        labels = json.load(f)

    if reviewed_only:
        labels = {k: v for k, v in labels.items() if v.get("reviewed", False)}

    return labels


def migrate_labels():
    """Ensure labels.json has consistent schema."""
    labels = load_labels()
    migrated = 0

    for filename, label in labels.items():
        # Add has_drink field if missing
        if "has_drink" not in label:
            has_drink = label.get("drink_type") is not None
            label["has_drink"] = has_drink
            migrated += 1

        # Normalize drink_count
        if label.get("has_drink") and label.get("drink_count", 0) == 0:
            label["drink_count"] = 1
            migrated += 1

        # Ensure all fields exist with defaults
        label.setdefault("reviewed", False)
        label.setdefault("notes", None)
        label.setdefault("auto_labeled", True)

    # Save migrated labels
    with open(LABELS_FILE, "w") as f:
        json.dump(labels, f, indent=2)

    print(f"Migrated {migrated} fields in labels.json")
    return labels


@dataclass
class SimpleVisionResult:
    """Simplified vision result for standalone evaluation."""
    drink_count: int = 0
    drink_type: str = "beer"  # Just store as string
    split_the_g_count: int = 0
    analyzed: bool = False


# Default production prompt (v12)
DEFAULT_PROMPT = (PROMPTS_DIR / "v12.txt").read_text() if (PROMPTS_DIR / "v12.txt").exists() else """
Analyze this image for alcoholic drinks. Return JSON with drink_type and split_the_g.
{"drink_type": <"beer"|"wine"|"cocktail"|"claw"|null>, "split_the_g": <bool>}
"""


class TestVisionService:
    """Vision service that allows prompt override for testing (standalone, no beerbot imports)."""

    def __init__(self, custom_prompt: Optional[str] = None):
        import os
        from google import genai

        api_key = os.environ.get("GEMINI_API_KEY")
        self.client = genai.Client(api_key=api_key) if api_key else None
        self.prompt = custom_prompt or DEFAULT_PROMPT
        self.model = "gemini-2.0-flash"

    def analyze_image_sync(self, image_data: bytes) -> tuple:
        """Analyze image and return (SimpleVisionResult, raw_response)."""
        from google.genai import types
        import re

        if not self.client:
            return SimpleVisionResult(), ""

        try:
            image_part = types.Part.from_bytes(data=image_data, mime_type="image/jpeg")
            response = self.client.models.generate_content(
                model=self.model,
                contents=[self.prompt, image_part],
            )

            raw_text = response.text.strip()

            # Try to extract JSON from response
            json_text = raw_text
            fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
            if fence_match:
                json_text = fence_match.group(1)

            try:
                data = json.loads(json_text)
                drink_type_str = data.get("drink_type")
                split_the_g = bool(data.get("split_the_g", False))

                if drink_type_str:
                    drink_type = drink_type_str.lower()
                    drink_count = 1
                else:
                    drink_type = "beer"
                    drink_count = 0

                result = SimpleVisionResult(
                    drink_count=drink_count,
                    drink_type=drink_type,
                    split_the_g_count=1 if split_the_g else 0,
                    analyzed=True,
                )
            except (json.JSONDecodeError, KeyError, TypeError):
                # Fallback parsing
                raw_lower = raw_text.lower()
                drink_type = "beer"
                drink_count = 0
                for dt in ["beer", "wine", "cocktail", "claw"]:
                    if dt in raw_lower:
                        drink_type = dt
                        drink_count = 1
                        break
                result = SimpleVisionResult(
                    drink_count=drink_count,
                    drink_type=drink_type,
                    split_the_g_count=0,
                    analyzed=True,
                )

            return result, raw_text

        except Exception as e:
            print(f"  API error: {e}")
            return SimpleVisionResult(), f"ERROR: {e}"


def evaluate_image(
    service: TestVisionService, image_path: Path, label: dict
) -> EvalResult:
    """Evaluate single image against ground truth."""
    image_data = image_path.read_bytes()
    result, raw_response = service.analyze_image_sync(image_data)

    # Ground truth
    gt_type = label.get("drink_type")
    gt_split_g = label.get("split_the_g", False)

    # Prediction (drink_type is now a string, not an enum)
    pred_type = result.drink_type if result.drink_count > 0 else None
    pred_split_g = result.split_the_g_count > 0

    # Correctness
    type_correct = gt_type == pred_type
    split_g_correct = gt_split_g == pred_split_g

    return EvalResult(
        filename=image_path.name,
        ground_truth_type=gt_type,
        ground_truth_split_g=gt_split_g,
        predicted_type=pred_type,
        predicted_split_g=pred_split_g,
        type_correct=type_correct,
        split_g_correct=split_g_correct,
        raw_response=raw_response,
    )


def calculate_metrics(results: list[EvalResult]) -> EvalMetrics:
    """Calculate precision, recall, F1 per drink type."""
    total = len(results)

    # Count correct
    type_correct = sum(1 for r in results if r.type_correct)
    split_g_correct = sum(1 for r in results if r.split_g_correct)
    overall_correct = sum(1 for r in results if r.type_correct and r.split_g_correct)

    # Confusion matrix: {actual: {predicted: count}}
    confusion = defaultdict(lambda: defaultdict(int))
    for r in results:
        actual = r.ground_truth_type or "null"
        predicted = r.predicted_type or "null"
        confusion[actual][predicted] += 1

    # Per-type metrics
    types = ["beer", "wine", "cocktail", "claw", "null"]
    per_type = {}

    for dtype in types:
        # True positives: predicted dtype, actual dtype
        tp = confusion[dtype][dtype]

        # False positives: predicted dtype, actual != dtype
        fp = sum(confusion[actual][dtype] for actual in types if actual != dtype)

        # False negatives: predicted != dtype, actual dtype
        fn = sum(confusion[dtype][pred] for pred in types if pred != dtype)

        # Support: total actual instances
        support = sum(confusion[dtype].values())

        # Calculate metrics
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        per_type[dtype] = TypeMetrics(
            precision=round(precision, 3),
            recall=round(recall, 3),
            f1=round(f1, 3),
            support=support,
        )

    # Error lists
    false_negatives = [
        r.filename for r in results
        if r.ground_truth_type is not None and r.predicted_type is None
    ]
    false_positives = [
        r.filename for r in results
        if r.ground_truth_type is None and r.predicted_type is not None
    ]
    wrong_type = [
        {"filename": r.filename, "expected": r.ground_truth_type, "predicted": r.predicted_type}
        for r in results
        if r.ground_truth_type is not None
        and r.predicted_type is not None
        and r.ground_truth_type != r.predicted_type
    ]

    return EvalMetrics(
        total_images=total,
        type_accuracy=round(type_correct / total, 3) if total > 0 else 0,
        split_g_accuracy=round(split_g_correct / total, 3) if total > 0 else 0,
        overall_accuracy=round(overall_correct / total, 3) if total > 0 else 0,
        per_type={k: asdict(v) for k, v in per_type.items()},
        confusion_matrix={k: dict(v) for k, v in confusion.items()},
        false_negatives=false_negatives,
        false_positives=false_positives,
        wrong_type=wrong_type,
    )


def print_report(metrics: EvalMetrics, prompt_source: str, verbose_results: list = None):
    """Print evaluation report to console."""
    print("=" * 80)
    print("VISION PROMPT EVALUATION REPORT")
    print("=" * 80)
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Prompt: {prompt_source}")
    print(f"Images evaluated: {metrics.total_images}")
    print()

    print("OVERALL METRICS")
    print("-" * 40)
    correct_type = int(metrics.type_accuracy * metrics.total_images)
    correct_split = int(metrics.split_g_accuracy * metrics.total_images)
    correct_all = int(metrics.overall_accuracy * metrics.total_images)
    print(f"Type Accuracy:     {metrics.type_accuracy:.1%} ({correct_type}/{metrics.total_images})")
    print(f"Split-G Accuracy:  {metrics.split_g_accuracy:.1%} ({correct_split}/{metrics.total_images})")
    print(f"Overall Accuracy:  {metrics.overall_accuracy:.1%} ({correct_all}/{metrics.total_images}) [all correct]")
    print()

    print("PER-TYPE METRICS")
    print("-" * 40)
    print(f"{'Type':<12} {'Precision':>10} {'Recall':>10} {'F1':>10} {'Support':>10}")
    for dtype in ["beer", "cocktail", "wine", "claw", "null"]:
        if dtype in metrics.per_type:
            m = metrics.per_type[dtype]
            print(f"{dtype:<12} {m['precision']:>10.2f} {m['recall']:>10.2f} {m['f1']:>10.2f} {m['support']:>10}")
    print()

    print("CONFUSION MATRIX")
    print("-" * 40)
    types = ["beer", "cocktail", "wine", "claw", "null"]
    print(f"{'Actual \\ Pred':<14}", end="")
    for t in types:
        print(f"{t:>8}", end="")
    print()
    for actual in types:
        print(f"{actual:<14}", end="")
        for pred in types:
            count = metrics.confusion_matrix.get(actual, {}).get(pred, 0)
            print(f"{count:>8}", end="")
        print()
    print()

    print("ERROR ANALYSIS")
    print("-" * 40)
    print(f"False Negatives ({len(metrics.false_negatives)} - missed drinks):")
    for fn in metrics.false_negatives[:5]:
        print(f"  - {fn}")
    if len(metrics.false_negatives) > 5:
        print(f"  ... and {len(metrics.false_negatives) - 5} more")
    print()

    print(f"False Positives ({len(metrics.false_positives)} - detected non-drinks):")
    for fp in metrics.false_positives[:5]:
        print(f"  - {fp}")
    if len(metrics.false_positives) > 5:
        print(f"  ... and {len(metrics.false_positives) - 5} more")
    print()

    print(f"Wrong Type ({len(metrics.wrong_type)}):")
    for wt in metrics.wrong_type[:5]:
        print(f"  - {wt['filename']}: expected {wt['expected']}, got {wt['predicted']}")
    if len(metrics.wrong_type) > 5:
        print(f"  ... and {len(metrics.wrong_type) - 5} more")

    if verbose_results:
        print()
        print("DETAILED RESULTS")
        print("-" * 40)
        for r in verbose_results:
            status = "OK" if r.type_correct and r.split_g_correct else "ERR"
            gt = r.ground_truth_type or "null"
            pred = r.predicted_type or "null"
            split_status = "split" if r.predicted_split_g else ""
            print(f"[{status}] {r.filename}: {gt} -> {pred} {split_status}")


def save_results(
    record: EvalRecord, output_path: Optional[Path] = None, quiet: bool = False
):
    """Save evaluation results to JSON."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if output_path is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        output_path = RESULTS_DIR / f"{record.name}_{timestamp}.json"

    with open(output_path, "w") as f:
        json.dump(asdict(record), f, indent=2)

    if not quiet:
        print(f"\nResults saved to {output_path}")


def load_baseline(baseline_path: Path) -> Optional[dict]:
    """Load baseline results for comparison."""
    if not baseline_path.exists():
        return None
    with open(baseline_path) as f:
        return json.load(f)


def get_reviewed_labels_hash() -> str:
    """Get hash of reviewed labels for change detection."""
    if not LABELS_FILE.exists():
        return ""
    with open(LABELS_FILE) as f:
        labels = json.load(f)
    # Only hash reviewed labels
    reviewed = {k: v for k, v in sorted(labels.items()) if v.get("reviewed", False)}
    return hashlib.sha256(json.dumps(reviewed, sort_keys=True).encode()).hexdigest()[:12]


def print_ci_output(
    metrics: EvalMetrics,
    prompt_source: str,
    baseline: Optional[dict] = None,
    threshold: float = 0.95,
    improvement_threshold: float = 0.01,
) -> bool:
    """Print CI-friendly JSON output and return success status.

    Returns True if evaluation passes, False if it fails.
    """
    labels_hash = get_reviewed_labels_hash()

    result = {
        "status": "pass",
        "accuracy": metrics.overall_accuracy,
        "type_accuracy": metrics.type_accuracy,
        "split_g_accuracy": metrics.split_g_accuracy,
        "total_images": metrics.total_images,
        "prompt_source": prompt_source,
        "reviewed_labels_hash": labels_hash,
        "improvement_detected": False,
        "errors": {
            "false_negatives": len(metrics.false_negatives),
            "false_positives": len(metrics.false_positives),
            "wrong_type": len(metrics.wrong_type),
        },
    }

    passed = True

    # Check against threshold
    if metrics.overall_accuracy < threshold:
        result["status"] = "fail"
        result["failure_reason"] = f"Accuracy {metrics.overall_accuracy:.1%} below threshold {threshold:.1%}"
        passed = False

    # Compare against baseline if provided
    if baseline:
        baseline_metrics = baseline.get("metrics", {})
        baseline_accuracy = baseline_metrics.get("overall_accuracy", 0)
        result["baseline_accuracy"] = baseline_accuracy
        delta = round(metrics.overall_accuracy - baseline_accuracy, 3)
        result["accuracy_delta"] = delta

        # Detect definitive improvement (>= threshold, e.g., 1%)
        if delta >= improvement_threshold:
            result["improvement_detected"] = True

        if metrics.overall_accuracy < baseline_accuracy:
            result["status"] = "fail"
            result["failure_reason"] = (
                f"Accuracy {metrics.overall_accuracy:.1%} regressed from baseline {baseline_accuracy:.1%}"
            )
            passed = False

    # Add error details if failed
    if not passed:
        result["error_details"] = {
            "false_negatives": metrics.false_negatives[:5],
            "false_positives": metrics.false_positives[:5],
            "wrong_type": metrics.wrong_type[:5],
        }

    print(json.dumps(result, indent=2))
    return passed


async def run_evaluation(
    prompt_source: str,
    custom_prompt: Optional[str],
    verbose: bool,
    output: Optional[str],
    name: str,
    reviewed_only: bool = False,
    ci_mode: bool = False,
    baseline_path: Optional[str] = None,
    threshold: float = 0.95,
) -> bool:
    """Run evaluation against dataset.

    Returns True if evaluation passes (for CI mode), False otherwise.
    """
    labels = load_labels(reviewed_only=reviewed_only)

    # Create vision service
    service = TestVisionService(custom_prompt)
    prompt_text = service.prompt

    if not ci_mode:
        print(f"Evaluating {len(labels)} images...")
        print(f"Using prompt: {prompt_source}")
        if reviewed_only:
            print("(--reviewed-only: filtering to reviewed labels only)")
        print()

    results = []
    for i, (filename, label) in enumerate(labels.items()):
        image_path = IMAGES_DIR / filename
        if not image_path.exists():
            if not ci_mode:
                print(f"[{i+1}/{len(labels)}] Skipping {filename} (not found)")
            continue

        if not ci_mode:
            print(f"[{i+1}/{len(labels)}] Evaluating {filename}...", end=" ")

        result = evaluate_image(service, image_path, label)
        results.append(result)

        if not ci_mode:
            status = "OK" if result.type_correct else "WRONG"
            gt = result.ground_truth_type or "null"
            pred = result.predicted_type or "null"
            print(f"{status} (expected={gt}, got={pred})")

        await asyncio.sleep(0.5)  # Rate limiting

    # Calculate metrics
    metrics = calculate_metrics(results)

    # CI mode: print JSON and exit
    if ci_mode:
        baseline = None
        if baseline_path:
            baseline = load_baseline(Path(baseline_path))
        passed = print_ci_output(metrics, prompt_source, baseline, threshold)
        return passed

    # Print report
    print()
    print_report(metrics, prompt_source, results if verbose else None)

    # Save results
    prompt_hash = hashlib.sha256(prompt_text.encode()).hexdigest()[:12]
    record = EvalRecord(
        name=name,
        timestamp=datetime.now().isoformat(),
        prompt_source=prompt_source,
        prompt_hash=prompt_hash,
        prompt_preview=prompt_text[:200] + "..." if len(prompt_text) > 200 else prompt_text,
        model=service.model,
        metrics=asdict(metrics),
        results=[asdict(r) for r in results],
    )

    if output:
        save_results(record, Path(output))
    else:
        save_results(record)

    return True


async def main():
    parser = argparse.ArgumentParser(description="Evaluate vision prompt against labeled dataset")
    parser.add_argument("--prompt", type=str, help="Path to custom prompt file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed results")
    parser.add_argument("--output", "-o", type=str, help="Output path for results JSON")
    parser.add_argument("--name", type=str, default="eval", help="Name for this evaluation run")
    parser.add_argument("--init", action="store_true", help="Initialize/migrate labels schema")
    parser.add_argument("--reviewed-only", action="store_true", help="Only evaluate reviewed labels")
    parser.add_argument("--ci", action="store_true", help="CI output mode (JSON to stdout)")
    parser.add_argument("--baseline", type=str, help="Path to baseline JSON for comparison")
    parser.add_argument("--threshold", type=float, default=0.95, help="Minimum accuracy threshold (default: 0.95)")
    parser.add_argument("--labels-hash", action="store_true", help="Print current reviewed labels hash and exit")
    args = parser.parse_args()

    if args.labels_hash:
        print(get_reviewed_labels_hash())
        return

    if args.init:
        migrate_labels()
        return

    # Determine prompt source
    custom_prompt = None
    if args.prompt:
        prompt_source = args.prompt
        custom_prompt = Path(args.prompt).read_text()
    else:
        prompt_source = "production (src/beerbot/vision.py)"

    passed = await run_evaluation(
        prompt_source=prompt_source,
        custom_prompt=custom_prompt,
        verbose=args.verbose,
        output=args.output,
        name=args.name,
        reviewed_only=args.reviewed_only,
        ci_mode=args.ci,
        baseline_path=args.baseline,
        threshold=args.threshold,
    )

    # Exit with non-zero status if CI mode failed
    if args.ci and not passed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
