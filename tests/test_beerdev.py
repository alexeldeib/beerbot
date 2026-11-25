"""beerdev scanner: chat paging, request filtering, and build selection (no network)."""

import json
from datetime import UTC, datetime

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


def test_react_marks_the_request_message():
    sent = []

    def respond(request):
        sent.append((request.url.path, request.content.decode()))
        return httpx.Response(200, json={"response": {}})

    groupme = httpx.Client(
        base_url="https://api.groupme.test/v3", transport=httpx.MockTransport(respond)
    )
    assert beerdev.react(groupme, "g1", "m7", "💡")
    path, body = sent[0]
    assert path == "/v3/messages/g1/m7/like"
    assert json.loads(body) == {"like_icon": {"type": "unicode", "code": "💡"}}
    failing = httpx.Client(
        base_url="https://x.test", transport=httpx.MockTransport(lambda _: httpx.Response(401))
    )
    assert not beerdev.react(failing, "g1", "m7", "💡")
