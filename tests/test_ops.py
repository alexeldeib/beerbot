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


def test_redeploy_waits_out_the_cooldown():
    def runs(created):
        def respond(request):
            if request.method == "GET":
                return httpx.Response(200, json={"workflow_runs": [{"created_at": created}]})
            return httpx.Response(204)

        return httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(respond))

    now = datetime.now(UTC)
    assert "did not redeploy" in ops.redeploy(runs((now - timedelta(minutes=5)).isoformat()), "o/r")
    assert "redeployed" in ops.redeploy(runs((now - timedelta(hours=2)).isoformat()), "o/r")


def test_diagnose_reports_what_the_workers_last_saw():
    def workers(payload):
        respond = lambda _: httpx.Response(200, json=payload)  # noqa: E731
        return httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(respond))

    error = {"at": "01:31", "detail": "InvalidCachedStatementError at delivery.py:126 execute_one"}
    status = {"failed": {"execution": True}, "last_error": {"execution": error, "delivery": None}}
    text = ops.diagnose(workers(status), {})
    assert "execution worker (failing)" in text and "InvalidCachedStatementError" in text
    assert ops.diagnose(workers({"last_error": {}}), {}) == "Workers report no errors."
    down = httpx.Client(
        base_url="https://x.test", transport=httpx.MockTransport(lambda _: httpx.Response(503))
    )
    assert ops.diagnose(down, {}).startswith("Worker status unavailable")


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
