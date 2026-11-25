"""beerdev scanner: chat paging, request filtering, and build selection (no network)."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from scripts import beerdev


def test_capabilities_come_from_tool_source():
    summary = beerdev.capabilities()
    assert "- log_drinks: Log drinks for one or more users." in summary
    assert "reply" not in summary


def test_chat_messages_page_back_to_window_and_skip_bots():
    since = datetime.fromtimestamp(1_000, UTC)
    pages = {
        None: [
            {"id": "4", "created_at": 1_400, "sender_type": "user", "text": "zyn tracker pls"},
            {"id": "3", "created_at": 1_300, "sender_type": "bot", "text": "+1🍺 for Al"},
        ],
        "3": [
            {"id": "2", "created_at": 1_200, "sender_type": "user", "text": None},
            {"id": "1", "created_at": 900, "sender_type": "user", "text": "too old"},
        ],
    }

    def respond(request):
        assert request.headers["X-Access-Token"] == "t"
        page = pages[request.url.params.get("before_id")]
        return httpx.Response(200, json={"response": {"messages": page}})

    groupme = httpx.Client(
        base_url="https://api.groupme.test",
        headers={"X-Access-Token": "t"},
        transport=httpx.MockTransport(respond),
    )
    assert beerdev.chat_messages(groupme, "g1", since) == [{"id": "4", "text": "zyn tracker pls"}]

    empty = httpx.Client(
        base_url="https://api.groupme.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(304)),
    )
    assert beerdev.chat_messages(empty, "g1", since) == []


def test_fresh_drops_invented_duplicate_and_filed_requests():
    def ask(message_id, kind="feature"):
        return beerdev.Request(message_id=message_id, kind=kind, title="T", spec="S.")

    messages = [{"id": i, "text": "a"} for i in ("6", "7", "8", "9")]
    issues = [
        {"title": "Old", "body": "<!-- beerdev:msg=8 -->"},  # Pre-kind marker covers all kinds.
        {"title": "Bug", "body": "<!-- beerdev:msg=9:bug -->"},
    ]
    asks = [ask("7"), ask("7"), ask("7", "infra"), ask("8", "bug"), ask("9", "bug")]
    asks += [ask("9", "infra"), ask("99")]
    kept = beerdev.fresh(asks, messages, issues)
    assert [(r.message_id, r.kind) for r in kept] == [
        ("7", "feature"),
        ("7", "infra"),
        ("9", "infra"),
    ]


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


def test_say_posts_only_as_the_beerdev_bot(monkeypatch):
    sent = []
    monkeypatch.setattr(
        beerdev.httpx,
        "post",
        lambda url, json, timeout: sent.append((url, json)) or httpx.Response(202),
    )
    monkeypatch.delenv("BEERDEV_BOT_ID", raising=False)
    assert not beerdev.say("hi")  # No bot id: nothing to post as.
    monkeypatch.setenv("BEERDEV_BOT_ID", "b1")
    assert not beerdev.say("  ")
    assert beerdev.say("💡 Queued: x")
    assert sent == [
        ("https://api.groupme.com/v3/bots/post", {"bot_id": "b1", "text": "💡 Queued: x"})
    ]


def test_acks_say_queued_or_waiting_on_a_human():
    def ask(kind):
        return beerdev.Request(message_id="1", kind=kind, title="Track zyns", spec="S.")

    assert beerdev.ack(ask("feature"), 7).startswith("💡 Queued: Track zyns (")
    assert beerdev.ack(ask("infra"), 7).startswith("👀 Filed for a human: Track zyns (")


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


def test_veto_closes_queued_work_and_removes_shipped_work_once():
    labels = [{"name": "beerdev"}, {"name": "inbox"}]
    issues = {
        5: {"title": "Gym tracker", "state": "open", "labels": labels},
        6: {"title": "Zyn tracker", "state": "closed", "state_reason": "completed", "labels": []},
        7: {"title": "Dropped", "state": "closed", "state_reason": "not_planned", "labels": []},
    }
    calls, known = [], []
    client = github(issues, calls)
    assert beerdev.steer(client, "o/r", known, "veto", 5) == "🚫 Vetoed: Gym tracker."
    assert ("PATCH", "/repos/o/r/issues/5") in [(m, path) for m, path, _ in calls]
    assert beerdev.steer(client, "o/r", known, "veto", 6).startswith("🚫 Vetoed after it shipped")
    removal = json.loads(calls[-1][2])
    assert removal["title"] == "Remove: Zyn tracker" and "beerdev:veto=6" in removal["body"]
    assert beerdev.steer(client, "o/r", known, "veto", 6) == ""  # Already removing it.
    assert beerdev.steer(client, "o/r", known, "veto", 7) == ""  # Never shipped: nothing to do.


def test_approve_makes_a_waiting_issue_buildable():
    issue = {
        "title": "Ops bot",
        "state": "open",
        "labels": [{"name": "proposal"}, {"name": "inbox"}],
    }
    calls = []
    line = beerdev.steer(github({3: issue}, calls), "o/r", [], "approve", 3)
    assert line == "💡 Approved: Ops bot. Queued to build."
    assert json.loads(calls[-1][2]) == {"labels": ["beerdev", "proposal"]}


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


def test_propose_caps_dedupes_and_routes_owner_work_to_the_inbox(tmp_path):
    plan = tmp_path / "plan.json"
    items = [
        {"title": "Index beers by group", "body": "b", "kind": "refactor", "needs_owner": False},
        {"title": "Upgrade httpx", "body": "b", "kind": "refactor", "needs_owner": True},
        {"title": "Already open", "body": "b", "kind": "bug", "needs_owner": False},
        {"title": "Fourth", "body": "b", "kind": "feature", "needs_owner": False},
    ]
    plan.write_text(json.dumps(items))
    calls = []
    existing = [{"title": "already OPEN", "state": "open"}]
    assert beerdev.propose(github({}, calls), "o/r", existing, plan)
    filed = [json.loads(body) for _, _, body in calls]
    assert [(f["title"], f["labels"]) for f in filed] == [
        ("Index beers by group", ["beerdev", "refactor"]),
        ("Upgrade httpx", ["proposal", "inbox"]),
    ]
