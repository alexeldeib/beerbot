"""beerdev: handling asks from the app, steering, answers, and builds (no network)."""

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


def test_approve_is_quiet_once_buildable():
    issue = {"title": "Ops bot", "state": "open", "labels": [{"name": "beerdev"}]}
    calls = []
    assert beerdev.steer(github({3: issue}, calls), "o/r", [], "approve", 3) == ""
    assert [method for method, _, _ in calls] == ["GET"]


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


def test_filed_is_one_issue_per_message_and_kind():
    issues = [
        {"title": "Old", "body": "<!-- beerdev:msg=8 -->"},  # Pre-kind marker covers all kinds.
        {"title": "Bug", "body": "<!-- beerdev:msg=9:bug -->"},
    ]
    assert beerdev.filed({"message_id": "8", "kind": "infra"}, issues)
    assert beerdev.filed({"message_id": "9", "kind": "bug"}, issues)
    assert not beerdev.filed({"message_id": "9", "kind": "infra"}, issues)
    assert not beerdev.filed({"message_id": "7", "kind": "feature"}, issues)


def test_named_picks_the_request_the_owner_named_or_none():
    issues = [
        {"number": 31, "title": "Track gym sessions", "state": "open"},
        {"number": 25, "title": "Ops monitoring that catches Beerius failures", "state": "open"},
        {"number": 19, "title": "Log Zyn consumption", "state": "closed"},
        {"number": 40, "title": "Log Zyn consumption", "state": "open"},  # A duplicate ask.
    ]
    assert beerdev.named("gym tracker", issues) == 31
    assert beerdev.named("the ops monitoring one", issues) == 25
    assert beerdev.named("zyn tracker", issues) == 40  # The word beats a stem; open beats shipped.
    assert beerdev.named("espresso martinis", issues) is None
    assert (
        beerdev.named("log", issues[2:3] + [{"number": 7, "title": "Log naps", "state": "closed"}])
        is None
    )


def test_answers_come_from_the_code_and_queue_without_tools(monkeypatch):
    seen = {}

    class Models:
        def generate_content(self, model, contents, **config):
            seen.update(contents=contents, config=config)
            return type("Answer", (), {"text": " Gym tracking is queued as #31. "})()

    monkeypatch.setattr(
        beerdev.genai, "Client", lambda api_key: type("C", (), {"models": Models()})()
    )
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    issues = [
        {
            "number": 31,
            "title": "Track gym sessions",
            "state": "open",
            "labels": [{"name": "beerdev"}],
        }
    ]
    line = beerdev.answer("What is queued?", issues)
    assert line == "🤓 Gym tracking is queued as #31."
    prompt = seen["contents"]
    assert "#31 open [beerdev] Track gym sessions" in prompt and "What is queued?" in prompt
    assert "=== src/beerbot/tools.py ===" in prompt and "async def log_drinks" in prompt
    assert seen["config"] == {}  # No tools, so nothing in the runner is reachable.


def test_scan_handles_each_ask_once_and_leaves_failures_for_a_retry(monkeypatch, tmp_path):
    now = datetime.now(UTC)

    def ask(id, kind, title="", issue=None, age=timedelta(0)):
        return {
            "id": id,
            "message_id": f"m{id}",
            "kind": kind,
            "title": title,
            "spec": "Details.",
            "issue": issue,
            "created_at": (now - age).isoformat(),
        }

    asks = [
        ask(1, "feature", "Track gym sessions"),
        ask(2, "question", "What is queued?"),
        ask(3, "veto", "zyn tracker"),  # Named by the owner's own words.
        ask(4, "approve", issue=404),  # Not a beerdev request.
        ask(5, "feature", "Ancient ask", age=timedelta(days=2)),  # Given up on: claimed, no post.
        ask(6, "question", "Breaks the model"),
    ]
    zyn = {
        "number": 19,
        "title": "Log Zyn consumption",
        "state": "open",
        "labels": [{"name": "beerdev"}],
        "body": "",
    }
    claimed, said = [], []

    def app(request):
        if request.method == "GET":
            return httpx.Response(200, json={"requests": asks})
        claimed.append(int(request.url.path.split("/")[-2]))
        return httpx.Response(200, json={"status": "claimed"})

    def github(request):
        path = request.url.path
        if request.method == "GET" and path.endswith("/issues"):
            return httpx.Response(
                200, json=[zyn] if request.url.params["labels"] == "beerdev" else []
            )
        if request.method == "GET":
            return httpx.Response(200, json=zyn)
        if request.method == "POST" and path.endswith("/issues"):
            created = json.loads(request.content) | {"number": 41, "state": "open"}
            return httpx.Response(201, json=created | {"labels": [{"name": "beerdev"}]})
        return httpx.Response(200, json={})

    real = httpx.Client

    def client(base_url, headers, timeout):
        handler = app if base_url == "https://app.test" else github
        return real(base_url=base_url, headers=headers, transport=httpx.MockTransport(handler))

    def answer(question, issues):
        if "Breaks" in question:
            raise RuntimeError("model down")
        return "🤓 Gym tracking is queued."

    monkeypatch.setattr(beerdev.httpx, "Client", client)
    monkeypatch.setattr(beerdev, "answer", answer)
    monkeypatch.setattr(beerdev, "say", lambda text, reply_to=None: said.append((text, reply_to)))
    for key, value in {
        "GITHUB_REPOSITORY": "o/r",
        "GH_TOKEN": "t",
        "ADMIN_TOKEN": "a",
        "APP_URL": "https://app.test",
        "GITHUB_OUTPUT": str(tmp_path / "out"),
    }.items():
        monkeypatch.setenv(key, value)
    beerdev.main("scan")
    assert claimed == [1, 2, 3, 4, 5]  # 6 failed: the app offers it again within the hour.
    assert said == [
        (f"💡 Queued: Track gym sessions ({beerdev.REPO_URL}/41). I'll post when it ships.", "m1"),
        ("🤓 Gym tracking is queued.", "m2"),
        ("🚫 Vetoed: Log Zyn consumption.", "m3"),
        ("🤷 Which request? Reply to my message about it, or name it.", "m4"),
    ]
    assert (tmp_path / "out").read_text() == "build=true\n"
