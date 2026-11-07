"""beerdev scanner: file feature requests from the GroupMe chat as GitHub issues.

Run by .github/workflows/beerdev.yml. Actions logs are public: never print chat text.
"""

import ast
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from google import genai
from google.genai import types
from pydantic import BaseModel

MODEL = "gemini-3.6-flash"
LABEL = "beerdev"
MAX_LOOKBACK = timedelta(hours=24)
TOOLS = Path(__file__).resolve().parents[1] / "src/beerbot/tools.py"

PROMPT = """You watch a friends' group chat that uses Beerius, a drink-tracking GroupMe bot.
Find messages that ask for a NEW bot capability (for example "can the bot track zyns?").
Ignore drink logs, banter, stats questions, bug complaints, things the bot already does, and
anything already covered by an existing request. Merge duplicate asks into one request.
Return [] when nothing qualifies, which is the usual case.

For each request give the message_id of the clearest ask, a short imperative title, and a spec
of one to three sentences describing the behavior. Never include people's names or IDs.

What the bot can do today:
{capabilities}

Existing requests:
{existing}

Chat messages, oldest first (JSON):
{messages}
"""


class Request(BaseModel):
    message_id: str
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
    """Resume at the last successful scheduled scan; overlap is deduplicated by message id."""
    now = datetime.now(UTC)
    if hours:
        return now - timedelta(hours=float(hours))
    runs = (
        github.get(
            f"/repos/{repo}/actions/workflows/beerdev.yml/runs",
            params={"event": "schedule", "status": "success", "per_page": 1},
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
    """Drop invented message ids and asks that already have an issue."""
    seen = {
        marker
        for issue in issues
        for marker in re.findall(r"beerdev:msg=([^\s>]+)", issue.get("body") or "")
    }
    scanned = {message["id"] for message in messages}
    kept = []
    for request in requests:
        if request.message_id in scanned and request.message_id not in seen:
            seen.add(request.message_id)
            kept.append(request)
    return kept


def next_build(issues: list[dict]) -> int | None:
    """Oldest open request that has not failed; builds run one at a time."""
    buildable = [
        issue["number"]
        for issue in issues
        if issue["state"] == "open"
        and "pull_request" not in issue
        and all(label["name"] != "beerdev-failed" for label in issue["labels"])
    ]
    return min(buildable, default=None)


def main() -> None:
    env = os.environ
    repo = env["GITHUB_REPOSITORY"]
    github = httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {env['GH_TOKEN']}"},
        timeout=30,
    )
    groupme = httpx.Client(
        base_url="https://api.groupme.com/v3",
        headers={"X-Access-Token": env["GROUPME_ACCESS_TOKEN"]},
        timeout=30,
    )
    issues_url = f"/repos/{repo}/issues"
    since = scan_start(github, repo, env.get("SINCE_HOURS"))
    messages = chat_messages(groupme, env["BEERDEV_GROUP_ID"], since)
    issues = (
        github.get(issues_url, params={"labels": LABEL, "state": "all", "per_page": 100})
        .raise_for_status()
        .json()
    )
    for request in fresh(classify(messages, issues), messages, issues) if messages else []:
        body = f"{request.spec}\n\n<!-- beerdev:msg={request.message_id} -->"
        created = github.post(
            issues_url, json={"title": request.title, "body": body, "labels": [LABEL]}
        )
        issues.append(created.raise_for_status().json())
    issue = next_build(issues)
    print(f"Scanned {len(messages)} messages since {since:%Y-%m-%d %H:%M} UTC; next: {issue}")
    if issue and (output := env.get("GITHUB_OUTPUT")):
        with open(output, "a") as handle:
            handle.write(f"issue={issue}\n")


if __name__ == "__main__":
    main()
