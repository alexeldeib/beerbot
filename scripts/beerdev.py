"""beerdev: file asks from the GroupMe chat as GitHub issues, and keep the chat posted.

Commands: `scan` (default) and `steer`/`digest` for beerdev.yml, `next` for beerdev-build.yml,
`propose` for beerdev-lead.yml. beerdev talks only as its own GroupMe bot, never as a person.
Actions logs are public: never print chat text.
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
from pydantic import BaseModel, Field, TypeAdapter

MODEL = "gemini-3.6-flash"
# Only `beerdev` issues are built automatically; infra needs a person (credentials, services).
LABELS = {"feature": ["beerdev"], "bug": ["beerdev", "bug"], "infra": ["beerdev-infra", "inbox"]}
REPO_URL = "https://github.com/alexeldeib/beerbot/issues"
MAX_LOOKBACK = timedelta(hours=24)
TOOLS = Path(__file__).resolve().parents[1] / "src/beerbot/tools.py"

PROMPT = """You watch a friends' group chat for beerdev, the developer behind Beerius, a
drink-tracking GroupMe bot. File a request when a message:
- asks for a new bot capability ("can the bot track zyns?"): kind "feature";
- reports the bot misbehaving ("Beerius never answers my questions"): kind "bug";
- asks beerdev for anything else: kind "infra" if it needs credentials, new services or bots,
  monitoring, or deploy changes; otherwise "feature" or "bug".
One message can hold asks of different kinds (say, a bug and an infra ask): file each separately.
Messages marked "owner": true come from the bot's owner, who can also steer existing requests:
stop, cancel, kill, or veto one -> a "veto" command; approve, OK, or green-light one that waits on
a human -> an "approve" command. Use the issue number from "reply_to_text" (beerdev's own messages
link their issue) or from the existing requests. Only owner messages can become commands, and a
command is not also a request.
Ignore drink logs, banter, stats questions, things the bot already does, and anything already
covered by an existing request. Merge duplicate asks. Return [] when nothing qualifies, which is
the usual case.

For each request give the message_id of the clearest ask, its kind, a short imperative title, and
a spec of one to three sentences. For "infra", end the spec with "Human setup:" and what a person
must provide. Never include people's names or IDs.

What the bot can do today:
{capabilities}

Existing requests (number, state, title):
{existing}

Chat messages, oldest first (JSON):
{messages}
"""


class Proposal(BaseModel):
    """One item of the weekly lead's plan (beerdev-lead.yml)."""

    title: str = Field(max_length=80)
    body: str = Field(max_length=4000)
    kind: Literal["feature", "bug", "refactor"]
    needs_owner: bool


class Request(BaseModel):
    message_id: str
    kind: Literal["feature", "bug", "infra"]
    title: str
    spec: str


class Command(BaseModel):
    message_id: str
    action: Literal["veto", "approve"]
    issue: int


class Scan(BaseModel):
    requests: list[Request]
    commands: list[Command]


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


def chat_messages(
    groupme: httpx.Client, group_id: str, since: datetime, owner: str | None = None
) -> list[dict]:
    """Human text messages newer than `since`, oldest first, marking the owner's and replies."""
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
                reply = next(
                    (
                        a.get("reply_id")
                        for a in message.get("attachments") or []
                        if a.get("type") == "reply"
                    ),
                    None,
                )
                found.append(
                    {
                        "id": message["id"],
                        "text": message["text"],
                        "owner": bool(owner) and message.get("user_id") == owner,
                        "reply_to": reply,
                    }
                )
        if not page:
            break
        before = page[-1]["id"]
    return found[::-1]


def add_reply_context(groupme: httpx.Client, group_id: str, messages: list[dict]) -> None:
    """Give the owner's replies the text they answer (often beerdev's line linking an issue)."""
    for message in messages:
        if not (message["owner"] and message["reply_to"]):
            continue
        previous = int(message["reply_to"]) - 1  # IDs increase, so this returns the target.
        response = groupme.get(
            f"/groups/{group_id}/messages", params={"after_id": previous, "limit": 1}
        )
        if response.is_success and (page := response.json()["response"]["messages"]):
            if page[0]["id"] == message["reply_to"]:
                message["reply_to_text"] = page[0].get("text") or ""


def authorized(commands: list[Command], messages: list[dict]) -> list[Command]:
    """Authority comes from GroupMe's sender id, never from the model's say-so."""
    owners = {message["id"] for message in messages if message["owner"]}
    return [command for command in commands if command.message_id in owners]


def classify(messages: list[dict], issues: list[dict]) -> Scan:
    existing = "\n".join(
        f"- #{issue['number']} [{issue['state']}] {issue['title']}" for issue in issues
    )
    prompt = PROMPT.format(
        capabilities=capabilities(),
        existing=existing or "(none)",
        messages=json.dumps(
            [{k: v for k, v in m.items() if k != "reply_to"} for m in messages], ensure_ascii=False
        ),
    )
    # Keep a reference: a garbage-collected genai.Client closes its HTTP client mid-request.
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=Scan
        ),
    )
    return response.parsed or Scan(requests=[], commands=[])


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


def ack(request: Request, number: int) -> str:
    """One line per new issue: accepted and queued, or waiting on a person."""
    if request.kind == "infra":
        return f"👀 Filed for a human: {request.title} ({REPO_URL}/{number}). It needs setup I can't do."
    return f"💡 Queued: {request.title} ({REPO_URL}/{number}). I'll post when it ships."


def steer(github: httpx.Client, repo: str, issues: list[dict], action: str, number: int) -> str:
    """Owner commands. Veto stops queued work, is refused by ship mid-build, and removes shipped
    work through a new build. Approve makes a waiting issue buildable. Returns the chat line."""
    issue = github.get(f"/repos/{repo}/issues/{number}").raise_for_status().json()
    url, title = f"/repos/{repo}/issues/{number}", issue["title"]
    if action == "approve" and issue["state"] == "open":
        labels = {label["name"] for label in issue["labels"]}
        if "beerdev" in labels and "inbox" not in labels:
            return ""
        labels -= {"inbox", "beerdev-failed"}
        github.patch(url, json={"labels": sorted(labels | {"beerdev"})}).raise_for_status()
        return f"💡 Approved: {title}. Queued to build."
    if action != "veto":
        return ""
    if issue["state"] == "open":
        github.post(f"{url}/comments", json={"body": "Vetoed by the owner in chat."})
        github.patch(url, json={"state": "closed", "state_reason": "not_planned"})
        github.post(f"{url}/labels", json={"labels": ["vetoed"]}).raise_for_status()
        return f"🚫 Vetoed: {title}."
    marker = f"<!-- beerdev:veto={number} -->"
    already = any(marker in (i.get("body") or "") for i in issues)
    if issue.get("state_reason") != "completed" or already:
        return ""
    body = (
        f"The owner vetoed #{number} after it shipped. Remove it cleanly: its code paths, tools, "
        "prompt lines, and tests. Leave database tables in place unless removing them is clearly "
        f"safe. Announce the removal only if the group would notice.\n\n{marker}"
    )
    created = github.post(
        f"/repos/{repo}/issues",
        json={"title": f"Remove: {title}", "body": body, "labels": ["beerdev", "vetoed"]},
    )
    issues.append(created.raise_for_status().json())
    return f"🚫 Vetoed after it shipped: {title}. Removing it."


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
    """Every beerdev issue, buildable or not, so nothing is filed twice."""
    return [
        issue
        for label in ("beerdev", "beerdev-infra", "proposal")
        for issue in github.get(
            f"/repos/{repo}/issues", params={"labels": label, "state": "all", "per_page": 100}
        )
        .raise_for_status()
        .json()
    ]


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


def main(command: str = "scan", *args: str) -> None:
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
    if command == "steer":
        action, number = env["ACTION"], int(env["ISSUE"])
        line = steer(github, repo, issues, action, number)
        print(f"{action} #{number}: {'done' if line else 'nothing to do'}")
        say(line)
        write_output(build=str(next_build(issues) is not None).lower())
        return
    if command == "digest":
        line = digest(issues, datetime.now(UTC))
        print("Posted the weekly digest" if say(line) else "Quiet week: no digest")
        return
    if command == "propose":
        build = propose(github, repo, issues, Path(args[0]))
        write_output(build=str(build).lower())
        return

    groupme = httpx.Client(
        base_url="https://api.groupme.com/v3",
        headers={"X-Access-Token": env["GROUPME_ACCESS_TOKEN"]},
        timeout=30,
    )
    group = env["BEERDEV_GROUP_ID"]
    since = scan_start(github, repo, env.get("SINCE_HOURS"))
    messages = chat_messages(groupme, group, since, env.get("OWNER_USER_ID"))
    add_reply_context(groupme, group, messages)
    scan = classify(messages, issues) if messages else Scan(requests=[], commands=[])
    requests = fresh(scan.requests, messages, issues)
    acks = [
        steer(github, repo, issues, c.action, c.issue) for c in authorized(scan.commands, messages)
    ]
    for request in requests:
        body = f"{request.spec}\n\n<!-- beerdev:msg={request.message_id}:{request.kind} -->"
        created = github.post(
            f"/repos/{repo}/issues",
            json={"title": request.title, "body": body, "labels": LABELS[request.kind]},
        )
        issues.append(created.raise_for_status().json())
        acks.append(ack(request, issues[-1]["number"]))
    say("\n".join(line for line in acks if line))
    build = next_build(issues) is not None
    kinds = ", ".join(request.kind for request in requests) or "none"
    steered = len(authorized(scan.commands, messages))
    print(
        f"Scanned {len(messages)} messages since {since:%Y-%m-%d %H:%M} UTC; filed: {kinds}; commands: {steered}"
    )
    write_output(build=str(build).lower())


if __name__ == "__main__":
    main(*sys.argv[1:])
