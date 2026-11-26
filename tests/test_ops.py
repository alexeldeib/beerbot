"""ops monitor: probing, failure grouping, and issue filing (no network)."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from scripts import ops


def client(routes: dict, calls: list | None = None) -> httpx.Client:
    def respond(request):
        if calls is not None:
            calls.append((request.method, request.url.path, request.content))
        status = routes.get((request.method, request.url.path), 200)
        return httpx.Response(status, json={"number": 99, "body": "", "state": "open"})

    return httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(respond))


def test_probe_tolerates_a_blip_and_reports_a_lasting_problem():
    assert ops.probe(client({}), pause=0) is None
    down = client({("GET", "/ready"): 503})
    assert "/ready is failing" in ops.probe(down, attempts=2, pause=0)
    flaky = iter([503, 200])

    def respond(request):
        return httpx.Response(next(flaky) if request.url.path == "/health" else 200)

    recovering = httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(respond))
    assert ops.probe(recovering, pause=0) is None


def test_failures_group_by_class_and_skip_what_issues_already_mention():
    status = {
        "failed_executions": [
            {"id": 9, "error_code": "ValueError", "attempts": 3, "created_at": "t"},
            {"id": 8, "error_code": "ValueError", "attempts": 3, "created_at": "t"},
            {"id": 7, "error_code": "TimeoutError", "attempts": 3, "created_at": "t"},
        ],
        "deliveries_needing_attention": [
            {"id": 5, "state": "uncertain", "error_code": "ReadTimeout", "created_at": "t"}
        ],
    }
    issues = [{"body": "<!-- ops:seen=inbox-8 --><!-- ops:seen=outbox-5 -->"}]
    groups = ops.new_failures(status, issues)
    assert {k: [r["id"] for r in v] for k, v in groups.items()} == {
        "failed:ValueError": [9],
        "failed:TimeoutError": [7],
    }
    title, text, labels = ops.describe("failed:ValueError", groups["failed:ValueError"])
    assert labels == ops.INCIDENT and "<!-- ops:seen=inbox-9 -->" in text
    # Every class goes to beerdev first; it hands non-code causes to the owner's inbox.
    assert ops.describe("failed:TimeoutError", groups["failed:TimeoutError"])[2] == ops.INCIDENT
    stuck = [{"id": 5, "state": "uncertain", "error_code": "ReadTimeout", "seen": "outbox-5"}]
    assert ops.describe("delivery", stuck)[2] == ops.NEEDS_OWNER


def test_report_appends_to_a_live_issue_and_otherwise_opens_one():
    batch = ("Messages failing with ValueError", "- inbox 9\n<!-- ops:seen=inbox-9 -->", ["ops"])
    key = "<!-- ops:key=failed:ValueError -->"
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    for issues, expected in [
        ([{"number": 4, "state": "open", "body": key}], ["PATCH", "POST"]),
        ([{"number": 4, "state": "closed", "closed_at": recent, "body": key}], ["PATCH", "POST"]),
        ([{"number": 4, "state": "closed", "closed_at": old, "body": key}], ["POST"]),
        ([], ["POST"]),
    ]:
        calls = []
        ops.report(client({}, calls), "o/r", issues, "failed:ValueError", batch)
        assert [method for method, _, _ in calls] == expected
        if expected == ["POST"]:
            assert calls[0][1] == "/repos/o/r/issues"
            assert json.loads(calls[0][2])["body"].startswith(key)
            assert issues[-1]["number"] == 99  # Later batches in the same run append to it.
        else:
            assert calls[0][1] == "/repos/o/r/issues/4" and "inbox-9" in issues[0]["body"]


def test_diagnose_reports_what_the_workers_last_saw():
    def workers(payload):
        respond = lambda _: httpx.Response(200, json=payload)  # noqa: E731
        return httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(respond))

    error = {"at": "01:31", "detail": "InvalidCachedStatementError at delivery.py:126 execute_one"}
    status = {"failed": {"execution": True}, "last_error": {"execution": error, "delivery": None}}
    text = ops.diagnose(ops.worker_status(workers(status), {}))
    assert "execution worker (failing)" in text and "InvalidCachedStatementError" in text
    assert ops.diagnose(ops.worker_status(workers({"last_error": {}}), {})) == (
        "Workers report no errors."
    )
    down = httpx.Client(
        base_url="https://x.test", transport=httpx.MockTransport(lambda _: httpx.Response(503))
    )
    assert ops.diagnose(ops.worker_status(down, {})).startswith("Worker status unavailable")


def test_symptoms_are_what_users_feel(monkeypatch):
    healthy = httpx.Client(
        base_url="https://x.test", transport=httpx.MockTransport(lambda _: httpx.Response(200))
    )
    assert ops.symptoms(healthy, {"oldest_unprocessed_seconds": 60}) == []
    stuck = {"oldest_unprocessed_seconds": 420, "oldest_undelivered_seconds": 900}
    assert ops.symptoms(healthy, stuck) == [
        "a message has waited 7 min to be answered",
        "a reply has waited 15 min to be posted",
    ]
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    down = httpx.Client(
        base_url="https://x.test", transport=httpx.MockTransport(lambda _: httpx.Response(503))
    )
    assert ops.symptoms(down, {}) == ["/health is failing: the app is down or crashing"]


def test_blame_names_failing_dependencies_only():
    def workers(detail):
        return {"last_error": {"execution": {"at": "x", "detail": detail}}}

    db, model = "the database", "the model provider"
    for detail, expected in [
        ("asyncpg.exceptions.ConnectionDoesNotExistError at delivery.py:170 execute_one", db),
        ("asyncpg.exceptions.AdminShutdownError at delivery.py:170 execute_one", db),
        ("ConnectionRefusedError at database.py:40 get_pool", db),  # Before module names.
        (
            "google.genai.errors.ServerError 503 at agent.py:458 process_message"
            " <- delivery.py:231 execute_one",
            model,
        ),
        ("google.genai.errors.ClientError 429 at model_runtime.py:180 next_turn", model),
        ("openai.InternalServerError 500 at model_runtime.py:240 next_turn", model),
        ("ServerError at model_runtime.py:180 next_turn", model),  # Before module names.
        # This repo's bugs, which a rollback can undo:
        ("asyncpg.exceptions.UndefinedColumnError at repositories.py:88 get_stats", None),
        ("google.genai.errors.ClientError 400 at model_runtime.py:180 next_turn", None),
        ("KeyError at agent.py:429 process_message", None),  # A line number, not a status.
        ("KeyError at tools.py:88 log_drinks", None),
    ]:
        assert ops.blame(workers(detail)) == expected, detail
    assert ops.blame({}) is None


def run(minutes_ago=1, status="completed", conclusion="success", event="push", repo="o/r", id=1):
    """A workflow run as GitHub lists it."""
    at = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "id": id,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "event": event,
        "head_sha": "abcdef1234",
        "head_repository": {"full_name": repo},
        "html_url": f"https://g/o/r/actions/runs/{id}",
        "created_at": at,
        "run_started_at": at,
        "updated_at": at,
    }


class FakeGitHub:
    """Enough of the GitHub API for ops: workflow runs (newest first), issues, dispatches."""

    def __init__(self, ci=(), rollbacks=()):
        self.calls, self.created, self.dispatched = [], [], []
        self.runs = {"ci.yml": list(ci), "rollback.yml": list(rollbacks)}

    def transport(self):
        def respond(request):
            path, method = request.url.path, request.method
            body = json.loads(request.content or "null")
            self.calls.append((method, path, body))
            if path.endswith("/runs"):
                found = self.runs[path.split("/")[-2]]
                if status := request.url.params.get("status"):
                    found = [r for r in found if status in (r["status"], r["conclusion"])]
                return httpx.Response(200, json={"workflow_runs": found})
            if path.endswith("/dispatches"):
                self.dispatched.append(path.split("/")[-2])
                return httpx.Response(204)
            if method == "GET" and path.endswith("/issues"):
                return httpx.Response(200, json=[])
            if method == "POST" and path.endswith("/issues"):
                self.created.append(body)
                number = 100 + len(self.created)
                return httpx.Response(
                    201,
                    json={
                        "number": number,
                        "html_url": f"https://g/{number}",
                        "body": body["body"],
                        "state": "open",
                    },
                )
            return httpx.Response(200, json={})

        return httpx.MockTransport(respond)

    def client(self):
        return httpx.Client(base_url="https://x.test", transport=self.transport())


def run_main(monkeypatch, fake, app, **env):
    """ops.main() against a fake GitHub and an app that answers with `app(request)`."""
    real = httpx.Client
    transports = {"https://api.github.com": fake.transport(), ops.APP: httpx.MockTransport(app)}

    def connect(base_url, **_):
        return real(base_url=base_url, transport=transports[base_url])

    monkeypatch.setattr(ops.httpx, "Client", connect)
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    pages = []
    monkeypatch.setattr(ops, "page", pages.append)
    env = {"GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "t", "OPS_TOKEN": "o", **env}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    ops.main()
    return pages


def test_ops_only_reads_the_app(monkeypatch):
    # No wakes and no restarts: a failed worker retries within a minute by itself.
    seen = []

    def app(request):
        seen.append(request.method)
        if request.url.path == "/ready":
            return httpx.Response(503)
        workers = {"failed": {"execution": True}, "last_error": {}}
        return httpx.Response(200, json=workers)

    pages = run_main(monkeypatch, FakeGitHub(), app)
    assert set(seen) == {"GET"} and len(pages) == 1  # Paged once, and changed nothing.


def incident_run(monkeypatch, fake, found, workers=None, issues=None):
    pages = []
    monkeypatch.setattr(ops, "page", pages.append)
    issues = issues if issues is not None else []
    line = ops.handle_incident(fake.client(), "o/r", issues, found, workers or {})
    return line, pages


def test_breakage_right_after_a_deploy_rolls_it_back_and_pages(monkeypatch):
    fake = FakeGitHub(ci=[run(12)])
    line, pages = incident_run(monkeypatch, fake, ["/ready is failing"])
    assert fake.dispatched == ["rollback.yml"] and line.startswith("Opened an incident")
    incident = fake.created[0]
    assert incident["labels"] == ops.INCIDENT_LABELS and incident["assignees"] == ["o"]
    assert ops.ROLLED_BACK in incident["body"]
    assert len(pages) == 1 and pages[0].startswith(
        "🚨 Beerius: /ready is failing. Rolling back abcdef1"
    )


def test_without_a_recent_deploy_ops_never_restarts_it_pages(monkeypatch):
    dependency = {"last_error": {"execution": {"at": "x", "detail": "ServerError at model.py:1"}}}
    for workers, expected in [
        (dependency, "Waiting: the model provider is failing"),
        ({}, "No safe automatic fix: this needs you."),
    ]:
        fake = FakeGitHub(ci=[run(300)])
        _, pages = incident_run(monkeypatch, fake, ["/ready is failing"], workers)
        assert fake.dispatched == [] and expected in pages[0]


def test_a_deploy_is_rolled_back_at_most_once_and_never_for_an_outage(monkeypatch):
    database = {"execution": {"at": "x", "detail": "asyncpg.exceptions.CannotConnectNowError at"}}
    for fake, workers, expected in [
        # The database is down: the release that just deployed is fine, and stays.
        (FakeGitHub(ci=[run(12)]), {"last_error": database}, "Waiting: the database"),
        # It was already rolled back once (by ops or by hand): the owner decides what's next.
        (FakeGitHub(ci=[run(30)], rollbacks=[run(20)]), {}, "A rollback already ran since"),
        # CI rolled its own deploy back and failed the run: what runs is the release before it.
        (FakeGitHub(ci=[run(10, conclusion="failure"), run(40)]), {}, "No safe automatic fix"),
        # A fork's "main" never counts as a deploy.
        (FakeGitHub(ci=[run(5, event="pull_request", repo="evil/r")]), {}, "No safe automatic"),
    ]:
        _, pages = incident_run(monkeypatch, fake, ["/ready is failing"], workers)
        assert fake.dispatched == [] and expected in pages[0], expected
    fake = FakeGitHub(ci=[run(5, event="pull_request", repo="evil/r"), run(15)])
    incident_run(monkeypatch, fake, ["/ready is failing"])
    assert fake.dispatched == ["rollback.yml"]  # Past the fork's run to this repo's deploy.


def test_an_open_incident_pages_once_and_resolves_with_a_follow_up(monkeypatch):
    opened = (datetime.now(UTC) - timedelta(minutes=12)).isoformat()
    incident = {
        "number": 7,
        "state": "open",
        "title": "Incident: /ready is failing",
        "html_url": "https://g/7",
        "created_at": opened,
        "body": f"{ops.INCIDENT_MARKER} ... {ops.ROLLED_BACK}",
    }
    fake = FakeGitHub(ci=[run(5)])  # A fix landed: never roll it back mid-incident.
    line, pages = incident_run(monkeypatch, fake, ["/ready is failing"], issues=[incident])
    assert pages == [] and fake.dispatched == [] and "still open" in line

    fake = FakeGitHub()
    line, pages = incident_run(monkeypatch, fake, [], issues=[incident])
    assert pages == ["✅ Resolved after 12 min (rolled back). https://g/7"]
    assert ("PATCH", "/repos/o/r/issues/7", {"state": "closed"}) in fake.calls
    follow_up = fake.created[0]
    assert follow_up["title"] == "Root cause: Incident: /ready is failing"
    assert follow_up["labels"] == ops.INCIDENT  # The SWE (beerdev) takes it from here.


def test_a_resolved_incident_in_this_repos_code_gets_a_root_cause_ticket(monkeypatch):
    opened = (datetime.now(UTC) - timedelta(minutes=9)).isoformat()
    for diagnosis, tickets in [
        ("- execution worker, last error: `KeyError at delivery.py:126 execute_one`", 1),
        ("Workers report no errors.", 0),
    ]:
        incident = {
            "number": 7,
            "state": "open",
            "title": "Incident: /ready is failing",
            "html_url": "https://g/7",
            "created_at": opened,
            "body": f"{ops.INCIDENT_MARKER}\n{diagnosis}",
        }
        fake = FakeGitHub()
        incident_run(monkeypatch, fake, [], issues=[incident])
        assert len(fake.created) == tickets, diagnosis


def test_ops_waits_while_production_is_changing(monkeypatch):
    fake = FakeGitHub(ci=[run(1, status="in_progress"), run(3)])
    line, pages = incident_run(monkeypatch, fake, ["/health is failing"])
    assert pages == [] and fake.created == [] and "checking again next run" in line
    # Queued behind an approval for 45 minutes is stuck, not a deploy: ops stops waiting.
    fake = FakeGitHub(ci=[run(45, status="queued"), run(300)])
    line, pages = incident_run(monkeypatch, fake, ["/health is failing"])
    assert line.startswith("Opened an incident") and len(pages) == 1


def test_a_failed_rollback_pages_once_and_ack_silences_an_incident(monkeypatch):
    opened = (datetime.now(UTC) - timedelta(minutes=12)).isoformat()

    def incident(*labels):
        return {
            "number": 7,
            "state": "open",
            "title": "Incident: /ready is failing",
            "html_url": "https://g/7",
            "created_at": opened,
            "labels": [{"name": name} for name in labels],
            "body": f"{ops.INCIDENT_MARKER} ... {ops.ROLLED_BACK}",
        }

    # The second failure finished before this incident opened: someone else's, never paged.
    rollbacks = [run(2, conclusion="failure", id=9), run(30, conclusion="failure", id=8)]
    live = incident()
    _, pages = incident_run(monkeypatch, FakeGitHub(rollbacks=rollbacks), ["down"], issues=[live])
    assert len(pages) == 1 and pages[0].startswith("🚨 The rollback failed")
    assert "https://g/o/r/actions/runs/9" in pages[0]
    _, pages = incident_run(monkeypatch, FakeGitHub(rollbacks=rollbacks), ["down"], issues=[live])
    assert pages == []  # Once.

    quiet = incident("ack")
    _, pages = incident_run(monkeypatch, FakeGitHub(rollbacks=rollbacks), ["down"], issues=[quiet])
    assert pages == [] and "ops:seen=rollback-9" in quiet["body"]  # Recorded, not paged.
    fake = FakeGitHub()
    _, pages = incident_run(monkeypatch, fake, [], issues=[quiet])
    assert pages == [] and ("PATCH", "/repos/o/r/issues/7", {"state": "closed"}) in fake.calls
    assert fake.created[0]["title"] == "Root cause: Incident: /ready is failing"


def test_expiring_credentials_get_one_inbox_issue_each():
    fake = FakeGitHub()
    issues = []
    due = ops.check_expiry(fake.client(), "o/r", issues, datetime(2027, 11, 1).date())
    assert len(due) == 3 and len(fake.created) == 3
    assert all(issue["labels"] == ops.NEEDS_OWNER for issue in fake.created)
    assert ops.check_expiry(fake.client(), "o/r", issues, datetime(2027, 1, 1).date()) == []


def test_deploy_failure_names_the_step_the_log_and_the_pr():
    trusted = {"event": "push", "head_branch": "main", "head_repository": {"full_name": "o/r"}}
    routes = {
        "/repos/o/r/actions/runs/1": {
            **trusted,
            "conclusion": "failure",
            "head_sha": "abc1234def",
            "html_url": "u",
        },
        "/repos/o/r/actions/runs/2": {**trusted, "conclusion": "success", "head_sha": "x"},
        # A fork PR can name its branch main; its log is attacker-controlled.
        "/repos/o/r/actions/runs/3": {
            **trusted,
            "conclusion": "failure",
            "event": "pull_request",
            "head_repository": {"full_name": "evil/r"},
        },
        "/repos/o/r/actions/runs/1/jobs": {
            "jobs": [
                {"id": 8, "name": "test", "conclusion": "success", "steps": []},
                {
                    "id": 7,
                    "name": "Deploy production",
                    "conclusion": "failure",
                    "steps": [{"name": "Verify production health", "conclusion": "failure"}],
                },
            ]
        },
        "/repos/o/r/commits/abc1234def/pulls": [{"number": 28}],
    }

    def respond(request):
        if request.url.path == "/repos/o/r/actions/jobs/7/logs":
            log = "setup\n##[group]Run curl /ready\n\x1b[31mcurl: (28) timed out\x1b[0m\n"
            log += (
                "##[error]Process completed with exit code 28.\nPost job cleanup.\nRemoving creds"
            )
            return httpx.Response(200, text=log)
        return httpx.Response(200, json=routes[request.url.path])

    github = httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(respond))
    key, text = ops.deploy_failure(github, "o/r", "1")
    assert key == "deploy:abc1234" and "#28" in text
    assert "**Deploy production** failed at Verify production health" in text
    assert "Run curl /ready" in text and "curl: (28) timed out" in text and "\x1b" not in text
    assert "##[error]" in text and "Removing creds" not in text  # Cleanup noise is dropped.
    assert ops.deploy_failure(github, "o/r", "2") is None
    assert ops.deploy_failure(github, "o/r", "3") is None  # Untrusted: never reaches beerdev.


def test_failure_excerpt_keeps_the_step_header_when_output_is_long():
    lines = ["noise"] * 5 + ["##[group]Run curl /health"] + ["503"] * 100 + ["##[error]exit 22"]
    excerpt = ops.failure_excerpt(lines, limit=10)
    assert excerpt[0] == "##[group]Run curl /health" and excerpt[1] == "..."
    assert excerpt[-1] == "##[error]exit 22" and len(excerpt) == 12
    assert ops.failure_excerpt(["a", "b", "c"], limit=2) == ["b", "c"]  # No marker: plain tail.


def test_spend_over_budget_files_one_inbox_issue_a_month():
    spend = {
        "month": "2026-11",
        "usd": 6.1,
        "projected_usd": 7.4,
        "budget_usd": 5.0,
        "over_budget": True,
        "by_source": {"beerius": {"usd": 5.9, "calls": 330}, "beerdev": {"usd": 0.2, "calls": 4}},
    }
    app = httpx.Client(
        base_url="https://x.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=spend)),
    )
    posted = []

    def github_respond(request):
        posted.append(json.loads(request.content))
        return httpx.Response(201, json={"number": 50})

    github = httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(github_respond))
    issues = []
    line = ops.check_spend(github, "o/r", issues, app, {})
    assert line.startswith("model spend $6.10 so far in 2026-11, $7.40 projected, budget $5.00")
    assert len(posted) == 1 and posted[0]["labels"] == ops.NEEDS_OWNER
    assert "beerius $5.90 (330 calls)" in posted[0]["body"]
    issues.append({"body": posted[0]["body"], "state": "closed"})  # Even once closed:
    ops.check_spend(github, "o/r", issues, app, {})
    assert len(posted) == 1  # one issue per month, not one per day.
    spend["over_budget"] = False
    ops.check_spend(github, "o/r", [], app, {})
    assert len(posted) == 1
