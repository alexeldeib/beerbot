#!/usr/bin/env python3
"""Scrape images from GroupMe and auto-label with Gemini for testing.

Usage:
    # Scrape all images
    uv run scripts/scrape_images.py --scrape

    # Incremental scrape (only new since last scrape)
    uv run scripts/scrape_images.py --scrape --incremental

    # Auto-label with Gemini
    uv run scripts/scrape_images.py --label

    # Full pipeline: incremental scrape + label new images
    uv run scripts/scrape_images.py --scrape --label --incremental

Environment variables:
    GROUPME_ACCESS_TOKEN: GroupMe API token
    GEMINI_API_KEY: Google Gemini API key (for labeling)
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

DATA_DIR = Path(__file__).parent.parent / "data"
IMAGES_DIR = DATA_DIR / "images"
METADATA_FILE = DATA_DIR / "metadata.json"
LABELS_FILE = DATA_DIR / "labels.json"

GROUPME_API = "https://api.groupme.com/v3"
GROUP_ID = "112172155"


def load_last_message_id() -> str | None:
    """Load the last processed message ID from metadata."""
    if not METADATA_FILE.exists():
        return None
    try:
        with open(METADATA_FILE) as f:
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
                        print(f"Reached last scraped message, stopping")
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


def extract_image_attachments(messages: list[dict]) -> list[dict]:
    """Extract messages with image attachments."""
    images = []

    for msg in messages:
        attachments = msg.get("attachments", [])
        for i, att in enumerate(attachments):
            if att.get("type") == "image" and att.get("url"):
                images.append({
                    "message_id": msg["id"],
                    "attachment_index": i,
                    "url": att["url"],
                    "user_id": msg.get("user_id"),
                    "user_name": msg.get("name"),
                    "text": msg.get("text"),
                    "created_at": msg.get("created_at"),
                    "favorited_by": msg.get("favorited_by", []),
                })

    print(f"Found {len(images)} images in messages")
    return images


async def download_images(images: list[dict]) -> list[dict]:
    """Download images to local directory."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    downloaded = []

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for i, img in enumerate(images):
            filename = f"msg_{img['message_id']}_{img['attachment_index']}.jpg"
            filepath = IMAGES_DIR / filename

            if filepath.exists():
                print(f"[{i+1}/{len(images)}] Skipping {filename} (exists)")
                img["local_path"] = str(filepath)
                downloaded.append(img)
                continue

            try:
                print(f"[{i+1}/{len(images)}] Downloading {filename}...")
                resp = await client.get(img["url"])
                resp.raise_for_status()

                filepath.write_bytes(resp.content)
                img["local_path"] = str(filepath)
                downloaded.append(img)

                await asyncio.sleep(0.05)  # Rate limiting

            except Exception as e:
                print(f"  Error downloading {img['url']}: {e}")

    print(f"Downloaded {len(downloaded)} images to {IMAGES_DIR}")
    return downloaded


def save_metadata(images: list[dict], messages: list[dict] | None = None):
    """Save image metadata to JSON.

    Args:
        images: List of image metadata dicts
        messages: Original messages (used to extract last_message_id)
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing metadata for merging in incremental mode
    existing_images = []
    if METADATA_FILE.exists():
        try:
            with open(METADATA_FILE) as f:
                existing = json.load(f)
                existing_images = existing.get("images", [])
        except (json.JSONDecodeError, KeyError):
            pass

    # Merge images (new first, then existing without duplicates)
    existing_ids = {img.get("message_id") for img in images}
    merged_images = images + [img for img in existing_images if img.get("message_id") not in existing_ids]

    # Get the most recent message ID for incremental scraping
    last_message_id = None
    if messages and len(messages) > 0:
        # Messages are typically newest first
        last_message_id = messages[0]["id"]
    elif merged_images:
        # Fall back to most recent image message
        last_message_id = merged_images[0].get("message_id")

    metadata = {
        "scraped_at": datetime.now().isoformat(),
        "group_id": GROUP_ID,
        "total_images": len(merged_images),
        "last_message_id": last_message_id,
        "images": merged_images,
    }

    with open(METADATA_FILE, "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"Saved metadata to {METADATA_FILE}")
    if last_message_id:
        print(f"Last message ID: {last_message_id} (for incremental scraping)")


async def label_images_with_gemini(api_key: str):
    """Auto-label images using Gemini."""
    from beerbot.vision import VisionService

    # Load metadata
    if not METADATA_FILE.exists():
        print("No metadata.json found. Run --scrape first.")
        return

    with open(METADATA_FILE) as f:
        metadata = json.load(f)

    images = metadata["images"]

    # Load existing labels (for resuming)
    if LABELS_FILE.exists():
        with open(LABELS_FILE) as f:
            labels = json.load(f)
    else:
        labels = {}

    # Create vision service
    vision = VisionService()
    if not vision.client:
        print("No Gemini API key configured")
        return

    for i, img in enumerate(images):
        local_path = img.get("local_path")
        if not local_path or not Path(local_path).exists():
            continue

        filename = Path(local_path).name

        # Skip if already labeled
        if filename in labels:
            print(f"[{i+1}/{len(images)}] Skipping {filename} (already labeled)")
            continue

        try:
            print(f"[{i+1}/{len(images)}] Labeling {filename}...")

            # Read image and analyze
            image_data = Path(local_path).read_bytes()
            result = vision.analyze_image_sync(image_data)

            labels[filename] = {
                "drink_type": result.drink_type.value if result.drink_count > 0 else None,
                "drink_count": result.drink_count,
                "split_the_g": result.split_the_g_count > 0,
                "auto_labeled": True,
                "reviewed": False,
                "message_id": img["message_id"],
                "user_name": img.get("user_name"),
                "text": img.get("text"),
            }

            # Save after each (resumable)
            with open(LABELS_FILE, "w") as f:
                json.dump(labels, f, indent=2)

            await asyncio.sleep(0.5)  # Rate limit Gemini

        except Exception as e:
            print(f"  Error labeling {filename}: {e}")

    # Summary
    total = len(labels)
    with_drinks = sum(1 for l in labels.values() if l.get("drink_type"))
    splits = sum(1 for l in labels.values() if l.get("split_the_g"))

    print(f"\nLabeling complete!")
    print(f"  Total images: {total}")
    print(f"  With drinks: {with_drinks}")
    print(f"  Split the G: {splits}")
    print(f"\nLabels saved to {LABELS_FILE}")
    print("Edit labels.json to correct any errors, then set 'reviewed': true")


async def main():
    parser = argparse.ArgumentParser(description="Scrape GroupMe images for testing")
    parser.add_argument("--scrape", action="store_true", help="Scrape and download images")
    parser.add_argument("--label", action="store_true", help="Auto-label with Gemini")
    parser.add_argument("--incremental", action="store_true",
                       help="Only fetch new messages since last scrape")
    args = parser.parse_args()

    if not args.scrape and not args.label:
        parser.print_help()
        return

    if args.scrape:
        token = os.environ.get("GROUPME_ACCESS_TOKEN")
        if not token:
            print("Error: GROUPME_ACCESS_TOKEN not set")
            print("Add it to .env.local or export it")
            return

        messages = await fetch_all_messages(token, incremental=args.incremental)
        images = extract_image_attachments(messages)
        images = await download_images(images)
        save_metadata(images, messages)

    if args.label:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Error: GEMINI_API_KEY not set")
            return

        await label_images_with_gemini(api_key)


if __name__ == "__main__":
    asyncio.run(main())
