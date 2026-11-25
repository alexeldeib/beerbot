"""ops monitor: health probes, self-healing redeploys, and one issue per failure class.

Run hourly, and right away on failures when the app can dispatch, by .github/workflows/ops.yml.
It never posts to the chat. Issues are public, so they carry exception types and this repo's
stack frames, never message text. All state lives in issue bodies as markers.
"""

import os
import re
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx

APP = "https://beerbot-groupme.fly.dev"
# Failure classes a code change can fix: also labeled for beerdev. Anything else needs a person.
CODE_BUGS = {
    "ToolBudgetExceeded",
    "ModelProtocolError",
    "ValueError",
    "TypeError",
    "KeyError",
    "AttributeError",
    "IndexError",
    "ValidationError",
}
REDEPLOY_COOLDOWN = timedelta(minutes=30)
# A class closed this recently gets appended to instead of reopened as a new build: a fix that
# didn't stick goes to a person rather than looping beerdev.
REPEAT_WINDOW = timedelta(days=7)
DAILY_HOUR = 12  # UTC hour of the one unconditional database check (it wakes Neon).
UNHEALTHY = "<!-- ops:key=unhealthy -->"


def probe(app: httpx.Client, attempts: int = 3, pause: float = 30) -> str | None:
    """What's wrong with production, or None. Retries so a deploy's brief blip isn't an incident."""
    problem = None
    for attempt in range(attempts):
        try:
            if not app.get("/health").is_success:
                problem = "/health is failing: the app is down or crashing"
            elif not app.get("/ready").is_success:
                problem = "/ready is failing: queue workers stopped or the database probe failed"
            else:
                return None
        except httpx.HTTPError as exc:
            problem = f"the app is unreachable ({type(exc).__name__})"
        if attempt + 1 < attempts:
            time.sleep(pause)
    return problem


def new_failures(status: dict, issues: list[dict]) -> dict[str, list[dict]]:
    """Failures no ops issue mentions yet, keyed by issue: one per error class, one for replies."""
    seen = {m for i in issues for m in re.findall(r"ops:seen=([\w-]+)", i.get("body") or "")}
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in status.get("failed_executions", []):
        if (marker := f"inbox-{row['id']}") not in seen:
            groups[f"failed:{row['error_code']}"].append({**row, "seen": marker})
    for row in status.get("deliveries_needing_attention", []):
        if (marker := f"outbox-{row['id']}") not in seen:
            groups["delivery"].append({**row, "seen": marker})
    return groups


def describe(key: str, rows: list[dict]) -> tuple[str, str, list[str]]:
    """Title, report text, and labels for a batch of failures."""
    if key == "delivery":
        title, labels = "Replies stuck in uncertain or failed delivery", ["ops"]
        lines = [
            f"- outbox {r['id']}: {r['state']} ({r['error_code']}) at {r.get('created_at', '?')}"
            for r in rows
        ]
        advice = (
            "Check the chat for each reply. If it never arrived, retry it with "
            "`POST /admin/messages/outbox/<id>/retry?acknowledge_uncertain=true` (docs/runbook.md)."
        )
    else:
        code = key.split(":", 1)[1]
        title = f"Messages failing with {code}"
        labels = ["ops", "beerdev", "bug"] if code in CODE_BUGS else ["ops"]
        lines = [
            f"- inbox {r['id']} at {r.get('created_at', '?')}, {r.get('attempts', '?')} attempts: "
            f"`{r.get('error_detail') or code}`"
            for r in rows
        ]
        advice = "Beerius never answered these messages. " + (
            "beerdev will try a fix: reproduce it with a failing test first."
            if code in CODE_BUGS
            else "This looks environmental (database, network, or model provider): "
            "a person should check docs/runbook.md."
        )
    markers = "".join(f"<!-- ops:seen={r['seen']} -->" for r in rows)
    return title, "\n".join(lines) + f"\n\n{advice}\n{markers}", labels


def report(github: httpx.Client, repo: str, issues: list[dict], key: str, batch: tuple) -> None:
    """Append to the live issue for this key, or open one."""
    title, text, labels = batch
    marker = f"<!-- ops:key={key} -->"
    now = datetime.now(UTC)
    live = [
        issue
        for issue in issues
        if marker in (issue.get("body") or "")
        and (
            issue["state"] == "open"
            or now - datetime.fromisoformat(issue["closed_at"]) < REPEAT_WINDOW
        )
    ]
    if live:
        issue = max(live, key=lambda i: i["number"])
        issue["body"] = f"{issue['body']}\n\n{text}"
        url = f"/repos/{repo}/issues/{issue['number']}"
        github.patch(url, json={"body": issue["body"]}).raise_for_status()
        github.post(f"{url}/comments", json={"body": text}).raise_for_status()
    else:
        body = f"{marker}\n{text}"
        created = github.post(
            f"/repos/{repo}/issues", json={"title": title, "labels": labels, "body": body}
        )
        issues.append(created.raise_for_status().json())


def redeploy(github: httpx.Client, repo: str) -> str:
    """Redeploy through CI, unless CI ran recently (that run may be the fix, or this a loop)."""
    runs = (
        github.get(f"/repos/{repo}/actions/workflows/ci.yml/runs", params={"per_page": 1})
        .raise_for_status()
        .json()["workflow_runs"]
    )
    started = datetime.fromisoformat(runs[0]["created_at"]) if runs else None
    if started and datetime.now(UTC) - started < REDEPLOY_COOLDOWN:
        return "CI ran within 30 minutes, so ops did not redeploy."
    github.post(f"/repos/{repo}/actions/workflows/ci.yml/dispatches", json={"ref": "main"})
    return "Ops redeployed through CI, which starts the app on a fresh machine."


def main() -> None:
    env = os.environ
    repo = env["GITHUB_REPOSITORY"]
    github = httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {env['GH_TOKEN']}"},
        timeout=30,
    )
    app = httpx.Client(base_url=APP, timeout=20)
    admin = {"Authorization": f"Bearer {env['ADMIN_TOKEN']}"}
    issues = (
        github.get(
            f"/repos/{repo}/issues", params={"labels": "ops", "state": "all", "per_page": 100}
        )
        .raise_for_status()
        .json()
    )
    unhealthy = [i for i in issues if i["state"] == "open" and UNHEALTHY in (i.get("body") or "")]

    if problem := probe(app):
        if unhealthy:  # Already redeployed once; repeating the same deploy won't help.
            url = f"/repos/{repo}/issues/{unhealthy[0]['number']}/comments"
            github.post(url, json={"body": f"Still unhealthy: {problem}."}).raise_for_status()
        else:
            body = f"{UNHEALTHY}\nDetected: {problem}.\n\n{redeploy(github, repo)}"
            github.post(
                f"/repos/{repo}/issues",
                json={"title": "Production is unhealthy", "labels": ["ops"], "body": body},
            ).raise_for_status()
        print(f"Unhealthy: {problem}")
        return
    for issue in unhealthy:
        url = f"/repos/{repo}/issues/{issue['number']}"
        github.post(f"{url}/comments", json={"body": "Recovered: /health and /ready pass."})
        github.patch(url, json={"state": "closed"}).raise_for_status()

    # The queue status reads Postgres, so only look when something says to (or once a day).
    workers = app.get("/admin/workers/status", headers=admin).raise_for_status().json()
    daily = datetime.now(UTC).hour == DAILY_HOUR
    if not (env.get("REASON") or workers.get("terminal_failures") or daily):
        print("Healthy; no failures since the last restart")
        return
    status = app.get("/admin/messages/status", headers=admin).raise_for_status().json()
    groups = new_failures(status, issues)
    for key, rows in groups.items():
        report(github, repo, issues, key, describe(key, rows))
    print(f"Healthy; new failures in {len(groups)} class(es): {', '.join(groups) or 'none'}")


if __name__ == "__main__":
    main()
