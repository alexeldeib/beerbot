"""ops, the SRE: watch production from outside, mitigate what's safe, page the owner, ticket the
rest. beerdev, the SWE, fixes what ops tickets.

Run every 5 minutes, after CI on main, and right away when the app reports a failure, by
.github/workflows/ops.yml, plus a daily sweep in a run of its own.
- Symptoms are what users feel: /health or /ready failing, or a message or reply waiting over
  5 minutes. They come from public endpoints and memory-only status, so a check never wakes the
  database.
- The decision table runs with no model:
  - A failing dependency is left alone: there's nothing to undo.
  - Otherwise, breakage within an hour of a deploy rolls back (rollback.yml), once per deploy.
  - Anything else gets the owner.
- Each incident is one GitHub issue assigned to the owner, paged in the private "beerbot alerts"
  GroupMe group on open, when a rollback fails, and on resolve. An `ack` label stops its pages.
- Each new ticket for beerdev starts a build (beerdev-build.yml), unless beerdev's fixes keep
  failing to deploy: then the owner gets it.
- It never posts in the main chat. Issues are public, so they carry exception types and this repo's
  stack frames, never message text. All state lives in issue bodies as markers.
"""

import os
import re
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

import httpx

APP = "https://beerbot-groupme.fly.dev"
# Incidents go to beerdev first. If it finds nothing to fix in code, it moves the issue to
# the owner's inbox (beerdev-build.yml), so every incident gets a fix attempt or a person.
INCIDENT = ["ops", "beerdev", "bug"]
NEEDS_OWNER = ["ops", "inbox"]
INCIDENT_LABELS = ["ops", "incident"]
DEPLOY_WINDOW = timedelta(minutes=60)  # Breakage this soon after a deploy rolls it back.
# A deploy or rollback unfinished this long is stuck (a pending approval, a lost runner), not
# changing production: waiting on it forever would leave ops blind.
STALE_RUN = timedelta(minutes=30)
QUEUE_LIMIT = 300  # Seconds a message or reply may wait before it counts as stuck.
# A class back this soon after closing reopens in the owner's inbox instead of starting another
# build: a fix that didn't stick goes to a person rather than looping beerdev.
REPEAT_WINDOW = timedelta(days=7)
# Deploy failures filed in a day before the next one goes to the owner instead of beerdev: its
# fixes are failing to deploy too, and another build would only loop.
DEPLOY_BREAKER = 2
INCIDENT_MARKER = "<!-- ops:incident -->"
ROLLED_BACK = "<!-- ops:rolled-back -->"
# A worker error naming one of these means an outside dependency failed: there's nothing to undo.
# Details read "module.Type [HTTP status] at frames", or a bare "Type at frames" from older
# releases. Only outages count: a SQL or schema error, or a 4xx from the model, is a bad deploy's.
DEPENDENCIES = {
    "the database": re.compile(
        r"asyncpg\.[\w.]*(Connection|Shutdown)|ConnectionRefused|CannotConnect"
        r"|TooManyConnections|ConnectionDoesNotExist|InterfaceError"
    ),
    "the model provider": re.compile(
        r"(google\.genai\.errors|openai)\.\w+ (429|5\d\d)\b|\bServerError at|ResourceExhausted"
    ),
}
# Credentials that expire, from docs/runbook.md. ops opens an inbox issue 30 days ahead.
EXPIRES = {
    "BEERDEV_GITHUB_TOKEN (Fly)": "2027-11-24",
    "BEERDEV_SHIP_TOKEN (GitHub, beerdev environment)": "2027-11-24",
    "CLAUDE_CODE_OAUTH_TOKEN (GitHub)": "2027-11-07",  # Set 2026-11-07; one year.
    "FLY_API_TOKEN (GitHub, production environment)": "2027-11-25",
}


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
    """Append to the live issue for this key, or open one. A closed one reopens in the inbox."""
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
        update = {"body": issue["body"]}
        if issue["state"] != "open":
            names = {label["name"] for label in issue.get("labels") or []}
            update |= {"state": "open", "labels": sorted(names - {"beerdev"} | {"inbox"})}
            issue.update(state="open", labels=[{"name": name} for name in update["labels"]])
            text = f"Back within a week: the fix didn't hold, so it waits for the owner.\n\n{text}"
        url = f"/repos/{repo}/issues/{issue['number']}"
        github.patch(url, json=update).raise_for_status()
        github.post(f"{url}/comments", json={"body": text}).raise_for_status()
    else:
        body = f"{marker}\n{text}"
        created = github.post(
            f"/repos/{repo}/issues", json={"title": title, "labels": labels, "body": body}
        )
        issues.append(created.raise_for_status().json())
        if "beerdev" in labels:  # Builds start only when asked: ask now, not next Monday.
            start_build(github, repo)


def start_build(github: httpx.Client, repo: str) -> None:
    """beerdev builds its queue one issue at a time, incidents first (beerdev-build.yml)."""
    path = f"/repos/{repo}/actions/workflows/beerdev-build.yml/dispatches"
    response = github.post(path, json={"ref": "main"})
    if not response.is_success:  # The ticket is filed; the next build to start picks it up.
        print(f"::warning::Could not start a beerdev build (HTTP {response.status_code})")


def deploy_labels(issues: list[dict], now: datetime) -> list[str]:
    """A deploy failure is beerdev's to fix, unless its last fixes kept failing to deploy too."""
    filed = [
        issue
        for issue in issues
        if "<!-- ops:key=deploy:" in (issue.get("body") or "")
        and now - datetime.fromisoformat(issue["created_at"]) < timedelta(days=1)
    ]
    return INCIDENT if len(filed) < DEPLOY_BREAKER else [*NEEDS_OWNER, "bug"]


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


def worker_status(app: httpx.Client, admin: dict) -> dict:
    """The app's memory-only status (never wakes the database), or why it's unavailable."""
    try:
        return app.get("/admin/workers/status", headers=admin).raise_for_status().json()
    except httpx.HTTPError as exc:
        return {"unavailable": type(exc).__name__}


def diagnose(workers: dict) -> str:
    """What the app reports about its workers."""
    if "unavailable" in workers:
        return f"Worker status unavailable ({workers['unavailable']})."
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
    """Issue key and report for a failed CI run on main, with the failing steps' log tail. None
    unless it is this repository's own failed push or dispatch of main, whatever the caller
    checked: a fork's log, which it controls, must never reach beerdev's agent."""
    run = github.get(f"/repos/{repo}/actions/runs/{run_id}").raise_for_status().json()
    if run.get("conclusion") != "failure" or run.get("head_branch") != "main":
        return None
    if not trusted(run, repo):
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


def symptoms(app: httpx.Client, workers: dict) -> list[str]:
    """What users would feel right now, or nothing."""
    found = [problem] if (problem := probe(app)) else []
    for key, what in (
        ("oldest_unprocessed_seconds", "a message has waited {} min to be answered"),
        ("oldest_undelivered_seconds", "a reply has waited {} min to be posted"),
    ):
        if (age := workers.get(key)) is not None and age > QUEUE_LIMIT:
            found.append(what.format(age // 60))
    return found


def blame(workers: dict) -> str | None:
    """The outside dependency the workers' errors name, if any."""
    details = " ".join(e["detail"] for e in (workers.get("last_error") or {}).values() if e)
    return next((name for name, pattern in DEPENDENCIES.items() if pattern.search(details)), None)


def runs(github: httpx.Client, repo: str, workflow: str, **params: str) -> list[dict]:
    path = f"/repos/{repo}/actions/workflows/{workflow}/runs"
    response = github.get(path, params={"branch": "main", "per_page": 5, **params})
    return response.raise_for_status().json()["workflow_runs"]


def trusted(run: dict, repo: str) -> bool:
    """This repository's own push or dispatch: a fork can name its branch main."""
    head = (run.get("head_repository") or {}).get("full_name")
    return run.get("event") in ("push", "workflow_dispatch") and head == repo


def changing(found: list[dict], now: datetime) -> bool:
    """A deploy or a rollback is changing production right now: wait for it."""
    return any(
        run["status"] != "completed"
        and now - datetime.fromisoformat(run["run_started_at"] or run["created_at"]) < STALE_RUN
        for run in found
    )


def last_deploy(ci: list[dict], repo: str) -> dict | None:
    """The deploy production runs, if CI finished it cleanly. CI fails a run whose deploy it rolled
    back, and a failed run may never have deployed: either way, what runs is the release before,
    which isn't ops' to roll back."""
    run = next((run for run in ci if run["status"] == "completed" and trusted(run, repo)), None)
    if run and run["conclusion"] == "success":
        return {"sha": run["head_sha"][:7], "at": datetime.fromisoformat(run["updated_at"])}
    return None


def mitigate(
    github: httpx.Client, repo: str, workers: dict, ci: list[dict], rollbacks: list[dict]
) -> str:
    """The decision table, run once as an incident opens: a deploy that landed during an incident
    is a fix attempt, not the cause. Returns what was done, or why not."""
    if dependency := blame(workers):  # An outage: rolling back a healthy release fixes nothing.
        return f"Waiting: {dependency} is failing, so there's nothing to undo or restart."
    deploy = last_deploy(ci, repo)
    if deploy and datetime.now(UTC) - deploy["at"] < DEPLOY_WINDOW:
        if any(datetime.fromisoformat(run["created_at"]) > deploy["at"] for run in rollbacks):
            return f"A rollback already ran since {deploy['sha']} deployed, so this needs you."
        github.post(
            f"/repos/{repo}/actions/workflows/rollback.yml/dispatches",
            json={"ref": "main", "inputs": {"dry_run": "false"}},
        ).raise_for_status()
        return f"Rolling back {deploy['sha']}: it deployed under an hour ago. {ROLLED_BACK}"
    return "No safe automatic fix: this needs you."


def page(text: str) -> None:
    """Post to the private "beerbot alerts" GroupMe group (only the owner and the ops bot)."""
    bot = os.environ.get("OPS_ALERT_BOT_ID")
    if not bot:
        print("::warning::OPS_ALERT_BOT_ID isn't set, so nobody was paged")
        return
    response = httpx.post(
        "https://api.groupme.com/v3/bots/post",
        json={"bot_id": bot, "text": text[:1000]},
        timeout=20,
    )
    if not response.is_success:
        print(f"::warning::Page failed (HTTP {response.status_code})")


def acked(incident: dict) -> bool:
    """The owner labeled it `ack`: they're on it, so no more pages."""
    return "ack" in {label["name"] for label in incident.get("labels") or []}


def rollback_failed(github: httpx.Client, repo: str, incident: dict, rollbacks: list[dict]) -> bool:
    """Tell the owner, once for each failed rollback.yml run, that a rollback failed during the
    incident: production is still broken and nothing automatic is left to try."""
    opened = datetime.fromisoformat(incident["created_at"])
    for run in rollbacks:
        seen = f"<!-- ops:seen=rollback-{run['id']} -->"
        body = incident.get("body") or ""
        if run["conclusion"] != "failure" or seen in body:
            continue
        if datetime.fromisoformat(run["updated_at"]) < opened:
            continue
        incident["body"] = f"{body}\n{seen}"
        path = f"/repos/{repo}/issues/{incident['number']}"
        github.patch(path, json={"body": incident["body"]}).raise_for_status()
        github.post(f"{path}/comments", json={"body": f"The [rollback]({run['html_url']}) failed."})
        if not acked(incident):
            page(f"🚨 The rollback failed: Beerius is still down and needs you. {run['html_url']}")
        return True
    return False


def handle_incident(
    github: httpx.Client, repo: str, issues: list[dict], found: list[str], workers: dict
) -> str:
    """Open, act on, or resolve the one incident. Returns a line for the log."""
    incident = next(
        (i for i in issues if i["state"] == "open" and INCIDENT_MARKER in (i.get("body") or "")),
        None,
    )
    now = datetime.now(UTC)
    if found:
        ci, rollbacks = runs(github, repo, "ci.yml"), runs(github, repo, "rollback.yml")
        if changing(ci + rollbacks, now):
            return f"Symptoms during a deploy or rollback; checking again next run: {found}"
        if incident is None:
            action = mitigate(github, repo, workers, ci, rollbacks)
            body = (
                f"{INCIDENT_MARKER}\n**Opened {now:%Y-%m-%d %H:%M} UTC:** {'; '.join(found)}.\n\n"
                f"{diagnose(workers)}\n\n**Action:** {action}"
            )
            created = github.post(
                f"/repos/{repo}/issues",
                json={
                    "title": f"Incident: {found[0]}",
                    "labels": INCIDENT_LABELS,
                    "assignees": [repo.split("/")[0]],
                    "body": body,
                },
            ).raise_for_status()
            url = created.json()["html_url"]
            page(f"🚨 Beerius: {'; '.join(found)}. {action.replace(ROLLED_BACK, '').strip()} {url}")
            return f"Opened an incident: {found}"
        if rollback_failed(github, repo, incident, rollbacks):
            return f"A rollback failed during the incident; told the owner: {found}"
        return f"Incident still open, no new action: {found}"
    if incident is None:
        return "Healthy"
    opened = datetime.fromisoformat(incident["created_at"])
    minutes = max(1, round((now - opened).total_seconds() / 60))
    path = f"/repos/{repo}/issues/{incident['number']}"
    github.post(f"{path}/comments", json={"body": f"Resolved after {minutes} min."})
    github.patch(path, json={"state": "closed"}).raise_for_status()
    rolled_back = ROLLED_BACK in (incident.get("body") or "")
    if not acked(incident):
        page(
            f"✅ Resolved after {minutes} min{' (rolled back)' if rolled_back else ''}. "
            f"{incident['html_url']}"
        )
    # Code was involved (a rollback, or this repo's frames, `delivery.py:126`): the SWE's turn.
    if rolled_back or re.search(r"\.py:\d+", incident.get("body") or ""):
        text = (
            f"Follow-up to {incident['html_url']} (resolved after {minutes} min"
            f"{', by rolling back' if rolled_back else ''}). beerdev: find the root cause from the "
            "incident's diagnosis and the changes around it, reproduce it with a failing test, "
            "and fix it."
        )
        report(
            github,
            repo,
            issues,
            f"followup-{incident['number']}",
            (f"Root cause: {incident['title']}", text, INCIDENT),
        )
    return f"Resolved an incident after {minutes} min"


def check_expiry(github: httpx.Client, repo: str, issues: list[dict], today: date) -> list[str]:
    """Inbox issues for credentials that expire within 30 days, once each."""
    due = []
    for name, day in EXPIRES.items():
        if (date.fromisoformat(day) - today).days > 30:
            continue
        marker = f"<!-- ops:key=expiry-{name.split()[0]}-{day} -->"
        due.append(name)
        if not any(marker in (i.get("body") or "") for i in issues):
            body = (
                f"{marker}\n{name} expires on {day}. Mint a new one: docs/runbook.md, Credentials."
            )
            github.post(
                f"/repos/{repo}/issues",
                json={
                    "title": f"Credential expires {day}: {name}",
                    "labels": NEEDS_OWNER,
                    "body": body,
                },
            ).raise_for_status()
    return due


def file_failures(
    github: httpx.Client, repo: str, issues: list[dict], app: httpx.Client, admin: dict
) -> list[str]:
    """Ticket failed messages and stuck replies no issue mentions yet. The queue status reads
    Postgres, so this runs only when the app reports a failure, and once a day."""
    status = app.get("/admin/messages/status", headers=admin).raise_for_status().json()
    groups = new_failures(status, issues)
    for key, rows in groups.items():
        report(github, repo, issues, key, describe(key, rows))
    return list(groups)


def sweep(
    github: httpx.Client, repo: str, issues: list[dict], app: httpx.Client, admin: dict
) -> str:
    """The daily sweep: expiring credentials, failure classes, and spend. No probes or incidents:
    the */5 checks own those."""
    expiring = check_expiry(github, repo, issues, datetime.now(UTC).date())
    classes = file_failures(github, repo, issues, app, admin)
    spend = check_spend(github, repo, issues, app, admin)
    return (
        f"Credentials expiring within 30 days: {expiring or 'none'}; new failures in "
        f"{len(classes)} class(es): {', '.join(classes) or 'none'}; {spend}"
    )


def latest_ci_failure(github: httpx.Client, repo: str) -> str | None:
    """The newest completed CI run of main, if it failed. ops.yml's concurrency can drop the
    workflow_run check that reports it, so every check looks."""
    found = runs(github, repo, "ci.yml", status="completed")
    run = next((run for run in found if trusted(run, repo)), None)
    return str(run["id"]) if run and run["conclusion"] == "failure" else None


def reported(issues: list[dict], run_id: str) -> bool:
    """An ops issue already links this CI run."""
    return any(re.search(rf"/actions/runs/{run_id}\b", i.get("body") or "") for i in issues)


def main() -> None:
    env = os.environ
    repo = env["GITHUB_REPOSITORY"]
    github = httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {env['GH_TOKEN']}"},
        timeout=30,
    )
    app = httpx.Client(base_url=APP, timeout=20)
    admin = {"Authorization": f"Bearer {env['OPS_TOKEN']}"}
    issues = (
        github.get(
            f"/repos/{repo}/issues", params={"labels": "ops", "state": "all", "per_page": 100}
        )
        .raise_for_status()
        .json()
    )

    if env.get("DAILY") == "true":  # A run of its own (ops.yml), so no */5 check can cancel it.
        print(sweep(github, repo, issues, app, admin))
        return

    run_id = env.get("CI_RUN_ID") or latest_ci_failure(github, repo)
    if run_id and reported(issues, run_id):  # Another check got to it first.
        run_id = None
    if run_id and (failure := deploy_failure(github, repo, run_id)):
        key, text = failure
        try:  # CI rolls back when verification fails; say what production runs now.
            running = app.get("/version").raise_for_status().json().get("git_sha") or "?"
        except httpx.HTTPError:
            running = "?"
        rolled_back = running != "?" and not running.startswith(key[7:])
        text += f"\n\nProduction now runs {running[:7]}{' (rolled back)' if rolled_back else ''}."
        if (labels := deploy_labels(issues, datetime.now(UTC))) != INCIDENT:
            text += "\n\nbeerdev's last fixes failed to deploy too, so this one waits for you."
        report(github, repo, issues, key, (f"Deploy failed at {key[7:]}", text, labels))
        print(f"Reported failed CI run {run_id}")

    workers = worker_status(app, admin)
    found = symptoms(app, workers)
    print(handle_incident(github, repo, issues, found, workers))
    if not found and env.get("REASON"):  # The app dispatched with a failure class to file.
        classes = file_failures(github, repo, issues, app, admin)
        print(f"Healthy; new failures in {len(classes)} class(es): {', '.join(classes) or 'none'}")


if __name__ == "__main__":
    main()
