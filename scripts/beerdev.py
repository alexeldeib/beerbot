"""beerdev's GitHub-side jobs: `next` picks the issue beerdev-build.yml builds, `digest` posts the
Monday history line, and `propose` files the weekly lead's plan. Asks and answers happen in the app
(src/beerbot/beerdev.py). beerdev talks only as its own GroupMe bot. Actions logs are public: never
print chat text.
"""

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field, TypeAdapter


class Proposal(BaseModel):
    """One item of the weekly lead's plan (beerdev-lead.yml)."""

    title: str = Field(max_length=80)
    body: str = Field(max_length=4000)
    kind: Literal["feature", "bug", "refactor"]
    needs_owner: bool


def next_build(issues: list[dict]) -> int | None:
    """Oldest open `beerdev` issue that has not failed, incidents (`ops`) first."""
    buildable = [
        (names.isdisjoint({"ops"}), issue["number"])
        for issue in issues
        if issue["state"] == "open"
        and "pull_request" not in issue
        and "beerdev" in (names := {label["name"] for label in issue["labels"]})
        and "beerdev-failed" not in names
    ]
    return min(buildable, default=(None, None))[1]


def say(text: str) -> bool:
    """Post to the group as the beerdev bot. A bot id can post and do nothing else."""
    bot_id = os.environ.get("BEERDEV_BOT_ID")
    if not bot_id or not text.strip():
        return False
    response = httpx.post(
        "https://api.groupme.com/v3/bots/post",
        json={"bot_id": bot_id, "text": text.strip()[:1000]},
        timeout=30,
    )
    if not response.is_success:
        print(f"::warning::beerdev could not post to the chat (HTTP {response.status_code})")
    return response.is_success


def digest(issues: list[dict], now: datetime) -> str:
    """Monday's history line for the chat; empty on a quiet week."""
    week = now - timedelta(days=7)

    def closed(reason: str) -> list[str]:
        return [
            i["title"]
            for i in issues
            if i["state"] == "closed"
            and i.get("state_reason") == reason
            and datetime.fromisoformat(i["closed_at"]) > week
            and "ops" not in {label["name"] for label in i["labels"]}
        ]

    shipped, declined = closed("completed"), closed("not_planned")
    waiting = [
        i for i in issues if i["state"] == "open" and "inbox" in {lb["name"] for lb in i["labels"]}
    ]
    if not (shipped or declined):
        return ""
    lines = ["📜 beerdev this week"]
    lines += [f"✅ Shipped: {'; '.join(shipped)}"] if shipped else []
    lines += [f"🤷 Declined or vetoed: {'; '.join(declined)}"] if declined else []
    lines += [f"👀 {len(waiting)} waiting on a human"] if waiting else []
    return "\n".join(lines)


def filed_requests(github: httpx.Client, repo: str) -> list[dict]:
    """Every beerdev issue, buildable or not, so nothing is filed twice, and the owner's whole
    inbox (held work loses `beerdev`). Each issue once, however many of these labels it has."""
    found = {
        issue["number"]: issue
        for label in ("beerdev", "beerdev-infra", "proposal", "inbox")
        for issue in github.get(
            f"/repos/{repo}/issues", params={"labels": label, "state": "all", "per_page": 100}
        )
        .raise_for_status()
        .json()
    }
    return list(found.values())


def propose(github: httpx.Client, repo: str, issues: list[dict], plan: Path) -> bool:
    """File the lead's plan: at most three items, skipping open duplicates. Work that needs a
    person (dependencies, infra, product calls) waits in the inbox until approved."""
    text = plan.read_text()  # The agent's final answer: tolerate a fence or a stray sentence.
    start, end = text.find("["), text.rfind("]")
    found = text[start : end + 1] if 0 <= start < end else "[]"
    proposals = TypeAdapter(list[Proposal]).validate_json(found)[:3]
    open_titles = {i["title"].casefold() for i in issues if i["state"] == "open"}
    build = False
    for item in proposals:
        if item.title.casefold() in open_titles:
            continue
        extra = {"bug": ["bug"], "refactor": ["refactor"]}.get(item.kind, [])
        labels = ["proposal", "inbox"] if item.needs_owner else ["beerdev", *extra]
        body = f"{item.body}\n\n_Proposed by the weekly beerdev lead._\n<!-- beerdev:lead -->"
        github.post(
            f"/repos/{repo}/issues", json={"title": item.title, "labels": labels, "body": body}
        ).raise_for_status()
        build |= not item.needs_owner
    print(f"Lead proposed {len(proposals)} item(s)")
    return build


def write_output(**values: object) -> None:
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as handle:
            handle.writelines(f"{key}={value}\n" for key, value in values.items())


def main(command: str, *args: str) -> None:
    env = os.environ
    repo = env["GITHUB_REPOSITORY"]
    github = httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {env['GH_TOKEN']}"},
        timeout=30,
    )
    issues = filed_requests(github, repo)
    if command == "next":
        issue = next_build(issues)
        print(f"Next build: {issue}")
        if issue:
            write_output(issue=issue)
    elif command == "digest":
        line = digest(issues, datetime.now(UTC))
        print("Posted the weekly digest" if say(line) else "Quiet week: no digest")
    elif command == "propose":
        write_output(build=str(propose(github, repo, issues, Path(args[0]))).lower())
    else:
        raise SystemExit(f"Unknown command {command!r}: use next, digest, or propose")


if __name__ == "__main__":
    main(*sys.argv[1:])
