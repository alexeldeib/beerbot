"""beerdev scanner: file asks from the GroupMe chat as GitHub issues and acknowledge them.

`scan` (default) is run by .github/workflows/beerdev.yml; `next` picks the issue for
beerdev-build.yml. Actions logs are public: never print chat text.
"""

import ast
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from google import genai
from google.genai import types
from pydantic import BaseModel

MODEL = "gemini-3.6-flash"
# Only `beerdev` issues are built automatically; infra needs a person (credentials, services).
LABELS = {"feature": ["beerdev"], "bug": ["beerdev", "bug"], "infra": ["beerdev-infra", "inbox"]}
FILED = "💡"
MAX_LOOKBACK = timedelta(hours=24)
TOOLS = Path(__file__).resolve().parents[1] / "src/beerbot/tools.py"

PROMPT = """You watch a friends' group chat for beerdev, the developer behind Beerius, a
drink-tracking GroupMe bot. File a request when a message:
- asks for a new bot capability ("can the bot track zyns?"): kind "feature";
- reports the bot misbehaving ("Beerius never answers my questions"): kind "bug";
- asks beerdev for anything else: kind "infra" if it needs credentials, new services or bots,
  monitoring, or deploy changes; otherwise "feature" or "bug".
One message can hold asks of different kinds (say, a bug and an infra ask): file each separately.
Ignore drink logs, banter, stats questions, things the bot already does, and anything already
covered by an existing request. Merge duplicate asks. Return [] when nothing qualifies, which is
the usual case.

For each request give the message_id of the clearest ask, its kind, a short imperative title, and
a spec of one to three sentences. For "infra", end the spec with "Human setup:" and what a person
must provide. Never include people's names or IDs.

What the bot can do today:
{capabilities}

Existing requests:
{existing}

Chat messages, oldest first (JSON):
{messages}
"""


class Request(BaseModel):
    message_id: str
    kind: Literal["feature", "bug", "infra"]
    title: str
    spec: str


def capabilities(source: Path = TOOLS) -> str:
    """Summarize Beerius's tools straight from source, so shipped features count as existing."""
    factory = next(
        node
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "create_tools"
    )
    lines = []
    for node in factory.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name != "reply":
            summary = (ast.get_docstring(node) or "").partition("\n")[0]
            lines.append(f"- {node.name}: {summary}")
    return "\n".join(lines)


def scan_start(github: httpx.Client, repo: str, hours: str | None) -> datetime:
    """Resume at the last successful scan; overlap is deduplicated by message id."""
    now = datetime.now(UTC)
    if hours:
        return now - timedelta(hours=float(hours))
    runs = (
        github.get(
            f"/repos/{repo}/actions/workflows/beerdev.yml/runs",
            params={"status": "success", "per_page": 1},
        )
        .raise_for_status()
        .json()["workflow_runs"]
    )
    if not runs:
        return now - MAX_LOOKBACK
    return max(datetime.fromisoformat(runs[0]["run_started_at"]), now - MAX_LOOKBACK)


def chat_messages(groupme: httpx.Client, group_id: str, since: datetime) -> list[dict]:
    """Human text messages newer than `since`, oldest first."""
    found: list[dict] = []
    before = None
    while True:
        params = {"limit": 100} | ({"before_id": before} if before else {})
        response = groupme.get(f"/groups/{group_id}/messages", params=params)
        if response.status_code == 304:  # GroupMe's "no older messages"
            break
        page = response.raise_for_status().json()["response"]["messages"]
        for message in page:
            if message["created_at"] < since.timestamp():
                return found[::-1]
            if message["sender_type"] == "user" and message.get("text"):
                found.append({"id": message["id"], "text": message["text"]})
        if not page:
            break
        before = page[-1]["id"]
    return found[::-1]


def classify(messages: list[dict], issues: list[dict]) -> list[Request]:
    prompt = PROMPT.format(
        capabilities=capabilities(),
        existing="\n".join(f"- {issue['title']}" for issue in issues) or "(none)",
        messages=json.dumps(messages, ensure_ascii=False),
    )
    # Keep a reference: a garbage-collected genai.Client closes its HTTP client mid-request.
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=list[Request]
        ),
    )
    return response.parsed or []


def fresh(requests: list[Request], messages: list[dict], issues: list[dict]) -> list[Request]:
    """Drop invented message ids and asks that already have an issue (one per message and kind)."""
    seen = {
        (message_id, kind or "*")  # Markers without a kind predate kinds and cover them all.
        for issue in issues
        for message_id, kind in re.findall(
            r"beerdev:msg=([^\s>:]+)(?::(\w+))?", issue.get("body") or ""
        )
    }
    scanned = {message["id"] for message in messages}
    kept = []
    for request in requests:
        key = (request.message_id, request.kind)
        if request.message_id in scanned and key not in seen and (key[0], "*") not in seen:
            seen.add(key)
            kept.append(request)
    return kept


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


def react(groupme: httpx.Client, group_id: str, message_id: str, emoji: str) -> bool:
    """Mark the ask itself. Shows as the token's owner: GroupMe bots cannot react."""
    response = groupme.post(
        f"/messages/{group_id}/{message_id}/like",
        json={"like_icon": {"type": "unicode", "code": emoji}},
    )
    return response.is_success


def filed_requests(github: httpx.Client, repo: str) -> list[dict]:
    """Every beerdev issue, buildable or not, so nothing is filed twice."""
    return [
        issue
        for label in ("beerdev", "beerdev-infra")
        for issue in github.get(
            f"/repos/{repo}/issues", params={"labels": label, "state": "all", "per_page": 100}
        )
        .raise_for_status()
        .json()
    ]


def write_output(**values: object) -> None:
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as handle:
            handle.writelines(f"{key}={value}\n" for key, value in values.items())


def main(command: str = "scan") -> None:
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
        return

    groupme = httpx.Client(
        base_url="https://api.groupme.com/v3",
        headers={"X-Access-Token": env["GROUPME_ACCESS_TOKEN"]},
        timeout=30,
    )
    group = env["BEERDEV_GROUP_ID"]
    since = scan_start(github, repo, env.get("SINCE_HOURS"))
    messages = chat_messages(groupme, group, since)
    requests = fresh(classify(messages, issues), messages, issues) if messages else []
    for request in requests:
        body = f"{request.spec}\n\n<!-- beerdev:msg={request.message_id}:{request.kind} -->"
        created = github.post(
            f"/repos/{repo}/issues",
            json={"title": request.title, "body": body, "labels": LABELS[request.kind]},
        )
        issues.append(created.raise_for_status().json())
        if not react(groupme, group, request.message_id, FILED):
            print("::warning::Filed a request but could not react to it")
    build = next_build(issues) is not None
    kinds = ", ".join(request.kind for request in requests) or "none"
    print(f"Scanned {len(messages)} messages since {since:%Y-%m-%d %H:%M} UTC; filed: {kinds}")
    write_output(build=str(build).lower())


if __name__ == "__main__":
    main(*sys.argv[1:])
