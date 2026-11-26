"""beerdev's GitHub-side jobs: picking builds, the Monday digest, the lead's plan, posting."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from scripts import beerdev


def test_next_build_is_oldest_open_unfailed_issue():
    def issue(number, state="open", labels=("beerdev",)):
        return {"number": number, "state": state, "labels": [{"name": n} for n in labels]}

    issues = [
        issue(9),
        issue(3, state="closed"),
        issue(4, labels=("beerdev", "beerdev-failed")),
        issue(6),
        {**issue(5), "pull_request": {}},
        issue(2, labels=("beerdev-infra",)),  # Needs a person, never auto-built.
    ]
    assert beerdev.next_build(issues) == 6
    assert beerdev.next_build([issue(3, state="closed")]) is None
    # Incidents jump the queue: production health beats new features.
    assert beerdev.next_build(issues + [issue(12, labels=("ops", "beerdev", "bug"))]) == 12


def github(issues_by_number, calls):
    def respond(request):
        calls.append((request.method, request.url.path, request.content))
        number = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and number.isdigit():
            return httpx.Response(200, json=issues_by_number[int(number)])
        return httpx.Response(
            201, json={"number": 99, "body": json.loads(request.content or "{}").get("body", "")}
        )

    return httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(respond))


def test_digest_reports_the_week_and_skips_quiet_ones():
    now = datetime(2026, 11, 30, 14, tzinfo=UTC)

    def issue(title, state="closed", reason="completed", days=2, labels=()):
        closed = (now - timedelta(days=days)).isoformat()
        return {
            "title": title,
            "state": state,
            "state_reason": reason,
            "closed_at": closed,
            "labels": [{"name": n} for n in labels],
        }

    issues = [
        issue("Zyn tracker"),
        issue("Gym tracker", reason="not_planned"),
        issue("Old thing", days=30),
        issue("Incident", labels=("ops",)),
        issue("Setup token", state="open", labels=("inbox",)),
    ]
    text = beerdev.digest(issues, now)
    assert "✅ Shipped: Zyn tracker" in text and "🤷 Declined or vetoed: Gym tracker" in text
    assert "Old thing" not in text and "Incident" not in text and "👀 1 waiting on a human" in text
    assert beerdev.digest([issue("Old thing", days=30)], now) == ""


def test_requests_list_each_issue_once_with_the_whole_inbox():
    by_label = {
        "beerdev": [{"number": 5, "title": "Approved proposal"}],
        "beerdev-infra": [],
        "proposal": [{"number": 5, "title": "Approved proposal"}],
        # Held work loses `beerdev`; ops' own asks never had it. Both wait on a human.
        "inbox": [{"number": 8, "title": "Held change"}, {"number": 9, "title": "Token expires"}],
    }

    def respond(request):
        return httpx.Response(200, json=by_label[request.url.params["labels"]])

    client = httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(respond))
    assert sorted(issue["number"] for issue in beerdev.filed_requests(client, "o/r")) == [5, 8, 9]


def test_propose_caps_dedupes_and_routes_owner_work_to_the_inbox(tmp_path):
    plan = tmp_path / "plan.json"
    items = [
        {"title": "Index beers by group", "body": "b", "kind": "refactor", "needs_owner": False},
        {"title": "Upgrade httpx", "body": "b", "kind": "refactor", "needs_owner": True},
        {"title": "Already open", "body": "b", "kind": "bug", "needs_owner": False},
        {"title": "Fourth", "body": "b", "kind": "feature", "needs_owner": False},
    ]
    plan.write_text("Here's the plan:\n```json\n" + json.dumps(items) + "\n```")
    calls = []
    existing = [{"title": "already OPEN", "state": "open"}]
    assert beerdev.propose(github({}, calls), "o/r", existing, plan)
    filed = [json.loads(body) for _, _, body in calls]
    assert [(f["title"], f["labels"]) for f in filed] == [
        ("Index beers by group", ["beerdev", "refactor"]),
        ("Upgrade httpx", ["proposal", "inbox"]),
    ]


def test_propose_files_nothing_from_an_answer_without_a_plan(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text("Nothing worth doing this week.")
    calls = []
    assert not beerdev.propose(github({}, calls), "o/r", [], plan)
    assert calls == []


def test_say_posts_only_as_the_beerdev_bot_threaded_when_it_can(monkeypatch):
    sent, refuse = [], {"attachments"}  # GroupMe may refuse a reply attachment from a bot.

    def post(url, json, timeout):
        sent.append(json)
        return httpx.Response(400 if refuse & json.keys() else 202)

    monkeypatch.setattr(beerdev.httpx, "post", post)
    monkeypatch.delenv("BEERDEV_BOT_ID", raising=False)
    assert not beerdev.say("hi")  # No bot id: nothing to post as.
    monkeypatch.setenv("BEERDEV_BOT_ID", "b1")
    assert not beerdev.say("  ")
    assert beerdev.say("💡 Queued: x")
    assert sent == [{"bot_id": "b1", "text": "💡 Queued: x"}]
    sent.clear()
    assert beerdev.say("🤓 Two.", reply_to="m2")  # Refused threaded, so posted plain.
    assert [post.get("attachments", [{}])[0].get("reply_id") for post in sent] == ["m2", None]
    sent.clear(), refuse.clear()
    assert beerdev.say("🤓 Two.", reply_to="m2") and len(sent) == 1
