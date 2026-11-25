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
# Incidents go to beerdev first. If it finds nothing to fix in code, it moves the issue to
# the owner's inbox (beerdev-build.yml), so every incident gets a fix attempt or a person.
INCIDENT = ["ops", "beerdev", "bug"]
NEEDS_OWNER = ["ops", "inbox"]
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
        title, labels = "Replies stuck in uncertain or failed delivery", NEEDS_OWNER
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
        labels = INCIDENT
        lines = [
            f"- inbox {r['id']} at {r.get('created_at', '?')}, {r.get('attempts', '?')} attempts: "
            f"`{r.get('error_detail') or code}`"
            for r in rows
        ]
        advice = (
            "Beerius never answered these messages. beerdev: reproduce it with a failing test "
            "and fix the cause; if it is not a code problem, say what a person must do."
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


def check_spend(
    github: httpx.Client, repo: str, issues: list[dict], app: httpx.Client, admin: dict
) -> str:
    """Once a day: at most one inbox issue a month when model spend is over budget."""
    spend = app.get("/admin/usage", headers=admin).raise_for_status().json()
    line = (
        f"model spend ${spend['usd']:.2f} so far in {spend['month']}, "
        f"${spend['projected_usd']:.2f} projected, budget ${spend['budget_usd']:.2f}"
    )
    marker = f"<!-- ops:key=budget-{spend['month']} -->"
    if spend.get("over_budget") and not any(marker in (i.get("body") or "") for i in issues):
        sources = ", ".join(
            f"{name} ${totals['usd']:.2f} ({totals['calls']} calls)"
            for name, totals in sorted(spend["by_source"].items())
        )
        body = (
            f"{marker}\nOver budget: {line}. By source: {sources or 'none'}.\n\n"
            "Options: a cheaper model (`LLM_MODEL`, e.g. a Flash-Lite), a smaller answer context, "
            "or a higher budget (`MODEL_BUDGET_USD` on Fly). Details: GET /admin/usage and "
            "docs/runbook.md (Spend)."
        )
        github.post(
            f"/repos/{repo}/issues",
            json={
                "title": f"Model spend over budget ({spend['month']})",
                "labels": NEEDS_OWNER,
                "body": body,
            },
        ).raise_for_status()
    return line


def diagnose(app: httpx.Client, admin: dict) -> str:
    """What the app reports about its workers. Memory-only, so it never wakes the database."""
    try:
        workers = app.get("/admin/workers/status", headers=admin).raise_for_status().json()
    except httpx.HTTPError as exc:
        return f"Worker status unavailable ({type(exc).__name__})."
    lines = [
        f"- {name} worker ({'failing' if workers.get('failed', {}).get(name) else 'recovered'}), "
        f"last error at {error['at']}: `{error['detail']}`"
        for name, error in (workers.get("last_error") or {}).items()
        if error
    ]
    return "\n".join(lines) or "Workers report no errors."


def failure_excerpt(lines: list[str], limit: int = 40) -> list[str]:
    """The failing step: its "Run" header, then the lines up to GitHub's first error marker.
    The tail of a job log is post-job cleanup, which never explains anything."""
    error = next((i for i, line in enumerate(lines) if "##[error]" in line), None)
    if error is None:
        return lines[-limit:]
    header = max(
        (i for i, line in enumerate(lines[:error]) if "##[group]Run" in line), default=None
    )
    body = lines[max(0, error + 1 - limit) : error + 1]
    return ([lines[header], "..."] if header is not None and header < error + 1 - limit else []) + (
        lines[header : error + 1] if header is not None and header >= error + 1 - limit else body
    )


def deploy_failure(github: httpx.Client, repo: str, run_id: str) -> tuple[str, str] | None:
    """Issue key and report for a failed CI run on main, with the failing steps' log tail."""
    run = github.get(f"/repos/{repo}/actions/runs/{run_id}").raise_for_status().json()
    trusted = (
        run.get("event") in ("push", "workflow_dispatch")  # Deploys; never pull_request runs.
        and run.get("head_branch") == "main"
        and (run.get("head_repository") or {}).get("full_name") == repo  # Not a fork's "main".
    )
    if run["conclusion"] != "failure" or not trusted:
        return None
    jobs = github.get(f"/repos/{repo}/actions/runs/{run_id}/jobs").raise_for_status().json()
    sections = []
    for job in jobs["jobs"]:
        if job["conclusion"] != "failure":
            continue
        steps = [s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"]
        log = github.get(f"/repos/{repo}/actions/jobs/{job['id']}/logs", follow_redirects=True)
        lines = failure_excerpt(log.text.splitlines()) if log.is_success else ["(log unavailable)"]
        tail = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", line)[:300] for line in lines)
        sections.append(f"**{job['name']}** failed at {', '.join(steps) or '?'}:\n```\n{tail}\n```")
    sha = run["head_sha"]
    pulls = github.get(f"/repos/{repo}/commits/{sha}/pulls")
    prs = ", ".join(f"#{p['number']}" for p in pulls.json()) if pulls.is_success else ""
    text = (
        f"[CI run]({run['html_url']}) for {sha[:7]} ({prs or 'no PR'}) failed on main.\n\n"
        + "\n\n".join(sections)
        + "\n\nbeerdev: find the cause in this log and the change that triggered it, then fix it."
    )
    return f"deploy:{sha[:7]}", text


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

    if (run_id := env.get("CI_RUN_ID")) and (failure := deploy_failure(github, repo, run_id)):
        key, text = failure
        try:  # CI rolls back when verification fails; say what production runs now.
            running = app.get("/version").raise_for_status().json().get("git_sha") or "?"
        except httpx.HTTPError:
            running = "?"
        rolled_back = running != "?" and not running.startswith(key[7:])
        text += f"\n\nProduction now runs {running[:7]}{' (rolled back)' if rolled_back else ''}."
        report(github, repo, issues, key, (f"Deploy failed at {key[7:]}", text, INCIDENT))
        print(f"Reported failed CI run {run_id}")

    if problem := probe(app):
        diagnosis = diagnose(app, admin)
        if unhealthy:  # Already redeployed once; repeating the same deploy won't help.
            url = f"/repos/{repo}/issues/{unhealthy[0]['number']}/comments"
            text = f"Still unhealthy: {problem}.\n\n{diagnosis}"
            github.post(url, json={"body": text}).raise_for_status()
        else:
            body = (
                f"{UNHEALTHY}\nDetected: {problem}.\n\n{diagnosis}\n\n{redeploy(github, repo)}"
                "\n\nbeerdev: find the cause from the errors above and recent changes, reproduce "
                "it with a failing test, and fix it."
            )
            github.post(
                f"/repos/{repo}/issues",
                json={"title": "Production is unhealthy", "labels": INCIDENT, "body": body},
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
    spend = check_spend(github, repo, issues, app, admin) if daily else "spend checked daily"
    print(
        f"Healthy; new failures in {len(groups)} class(es): {', '.join(groups) or 'none'}; {spend}"
    )


if __name__ == "__main__":
    main()
