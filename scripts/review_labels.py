#!/usr/bin/env python3
"""Interactive helper for reviewing auto-labeled images.

Usage:
    # Review all unreviewed labels interactively
    uv run scripts/review_labels.py

    # Show summary of unreviewed labels
    uv run scripts/review_labels.py --summary

    # Open images in default viewer while reviewing
    uv run scripts/review_labels.py --open

    # Review specific image
    uv run scripts/review_labels.py --image msg_123456_0.jpg
"""

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
IMAGES_DIR = DATA_DIR / "images"
LABELS_FILE = DATA_DIR / "labels.json"

DRINK_TYPES = ["beer", "wine", "cocktail", "claw", None]


def load_labels() -> dict:
    """Load labels from JSON file."""
    with open(LABELS_FILE) as f:
        return json.load(f)


def save_labels(labels: dict):
    """Save labels to JSON file."""
    with open(LABELS_FILE, "w") as f:
        json.dump(labels, f, indent=2)


def open_image(image_path: Path):
    """Open image in default viewer."""
    system = platform.system()
    if system == "Darwin":  # macOS
        subprocess.run(["open", str(image_path)], check=False)
    elif system == "Linux":
        subprocess.run(["xdg-open", str(image_path)], check=False)
    elif system == "Windows":
        subprocess.run(["start", str(image_path)], shell=True, check=False)


def print_label_info(filename: str, label: dict):
    """Print label information."""
    drink_type = label.get("drink_type") or "null"
    split_g = "yes" if label.get("split_the_g") else "no"
    user = label.get("user_name", "unknown")
    text = label.get("text") or ""
    notes = label.get("notes") or ""

    print(f"\n{'='*60}")
    print(f"File: {filename}")
    print(f"Path: {IMAGES_DIR / filename}")
    print(f"{'='*60}")
    print(f"Current label:")
    print(f"  drink_type: {drink_type}")
    print(f"  split_the_g: {split_g}")
    print(f"  user: {user}")
    if text:
        print(f"  text: {text}")
    if notes:
        print(f"  notes: {notes}")
    print()


def prompt_drink_type(current: str | None) -> str | None:
    """Prompt user for drink type."""
    options = ["beer", "wine", "cocktail", "claw", "null"]
    current_display = current or "null"

    print(f"Drink type ({current_display}):")
    for i, opt in enumerate(options, 1):
        marker = " <-- current" if opt == current_display else ""
        print(f"  {i}. {opt}{marker}")
    print("  Enter: keep current")
    print("  q: quit review")

    while True:
        choice = input("> ").strip().lower()
        if choice == "":
            return current
        if choice == "q":
            return "QUIT"
        if choice in ["1", "2", "3", "4", "5"]:
            result = options[int(choice) - 1]
            return None if result == "null" else result
        if choice in options:
            return None if choice == "null" else choice
        print("Invalid choice. Try again.")


def prompt_split_g(current: bool) -> bool | str:
    """Prompt user for split_the_g."""
    current_display = "yes" if current else "no"
    print(f"Split the G? ({current_display}) [y/n/Enter=keep/q=quit]:")

    while True:
        choice = input("> ").strip().lower()
        if choice == "":
            return current
        if choice == "q":
            return "QUIT"
        if choice in ["y", "yes", "1"]:
            return True
        if choice in ["n", "no", "0"]:
            return False
        print("Invalid choice. Try again.")


def prompt_notes(current: str | None) -> str | None:
    """Prompt user for notes."""
    print(f"Notes ({current or 'none'}):")
    print("  Enter: keep current")
    print("  Type new note, or 'clear' to remove")

    choice = input("> ").strip()
    if choice == "":
        return current
    if choice.lower() == "clear":
        return None
    return choice


def review_label(filename: str, label: dict, auto_open: bool = False) -> tuple[dict, bool]:
    """Review a single label interactively.

    Returns (updated_label, should_continue).
    """
    image_path = IMAGES_DIR / filename

    if not image_path.exists():
        print(f"Warning: Image not found: {image_path}")
        return label, True

    if auto_open:
        open_image(image_path)

    print_label_info(filename, label)

    # Drink type
    drink_type = prompt_drink_type(label.get("drink_type"))
    if drink_type == "QUIT":
        return label, False

    # Split the G
    split_g = prompt_split_g(label.get("split_the_g", False))
    if split_g == "QUIT":
        return label, False

    # Notes
    notes = prompt_notes(label.get("notes"))

    # Update label
    label["drink_type"] = drink_type
    label["split_the_g"] = split_g
    label["notes"] = notes
    label["reviewed"] = True
    label["has_drink"] = drink_type is not None
    label["drink_count"] = 1 if drink_type else 0

    print(f"\nUpdated: drink_type={drink_type or 'null'}, split_g={split_g}, reviewed=True")

    return label, True


def show_summary(labels: dict):
    """Show summary of labels."""
    total = len(labels)
    reviewed = sum(1 for l in labels.values() if l.get("reviewed", False))
    unreviewed = total - reviewed

    by_type = {}
    for label in labels.values():
        dtype = label.get("drink_type") or "null"
        by_type[dtype] = by_type.get(dtype, 0) + 1

    splits = sum(1 for l in labels.values() if l.get("split_the_g"))

    print(f"\nLabel Summary")
    print(f"{'='*40}")
    print(f"Total images: {total}")
    print(f"Reviewed: {reviewed}")
    print(f"Unreviewed: {unreviewed}")
    print()
    print(f"By drink type:")
    for dtype in ["beer", "wine", "cocktail", "claw", "null"]:
        count = by_type.get(dtype, 0)
        print(f"  {dtype}: {count}")
    print()
    print(f"Split the G: {splits}")

    if unreviewed > 0:
        print(f"\nUnreviewed images:")
        for filename, label in labels.items():
            if not label.get("reviewed", False):
                dtype = label.get("drink_type") or "null"
                print(f"  {filename}: {dtype}")


def main():
    parser = argparse.ArgumentParser(description="Review auto-labeled images")
    parser.add_argument("--summary", action="store_true", help="Show summary only")
    parser.add_argument("--open", action="store_true", help="Open images in viewer")
    parser.add_argument("--image", type=str, help="Review specific image")
    parser.add_argument("--all", action="store_true", help="Review all images (not just unreviewed)")
    args = parser.parse_args()

    labels = load_labels()

    if args.summary:
        show_summary(labels)
        return

    # Determine which images to review
    if args.image:
        if args.image not in labels:
            print(f"Error: {args.image} not found in labels.json")
            return
        to_review = [(args.image, labels[args.image])]
    elif args.all:
        to_review = list(labels.items())
    else:
        to_review = [(f, l) for f, l in labels.items() if not l.get("reviewed", False)]

    if not to_review:
        print("No unreviewed images found!")
        show_summary(labels)
        return

    print(f"Found {len(to_review)} images to review")
    print("Press Enter to keep current value, or enter new value")
    print("Enter 'q' to quit at any prompt")

    reviewed_count = 0
    for i, (filename, label) in enumerate(to_review):
        print(f"\n[{i+1}/{len(to_review)}]")
        updated_label, should_continue = review_label(filename, label, auto_open=args.open)
        labels[filename] = updated_label

        if updated_label.get("reviewed"):
            reviewed_count += 1

        # Save after each review
        save_labels(labels)

        if not should_continue:
            print(f"\nQuitting. Reviewed {reviewed_count} images.")
            break
    else:
        print(f"\nDone! Reviewed {reviewed_count} images.")

    show_summary(labels)


if __name__ == "__main__":
    main()
