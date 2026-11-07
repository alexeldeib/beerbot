"""beerdev scanner: chat paging, request filtering, and build selection (no network)."""

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
    def ask(message_id):
        return beerdev.Request(message_id=message_id, title="Track zyns", spec="Log pouches.")

    messages = [{"id": "7", "text": "a"}, {"id": "8", "text": "b"}]
    issues = [{"title": "Track zyns", "body": "Log.\n\n<!-- beerdev:msg=8 -->"}]
    kept = beerdev.fresh([ask("7"), ask("7"), ask("8"), ask("99")], messages, issues)
    assert [request.message_id for request in kept] == ["7"]


def test_next_build_is_oldest_open_unfailed_issue():
    def issue(number, state="open", labels=("beerdev",)):
        return {"number": number, "state": state, "labels": [{"name": n} for n in labels]}

    issues = [
        issue(9),
        issue(3, state="closed"),
        issue(4, labels=("beerdev", "beerdev-failed")),
        issue(6),
        {**issue(5), "pull_request": {}},
    ]
    assert beerdev.next_build(issues) == 6
    assert beerdev.next_build([issue(3, state="closed")]) is None
