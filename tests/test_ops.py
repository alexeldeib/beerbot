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
    assert labels == ["ops", "beerdev", "bug"] and "<!-- ops:seen=inbox-9 -->" in text
    assert ops.describe("failed:TimeoutError", groups["failed:TimeoutError"])[2] == ["ops"]


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
