"""beerdev, the peer agent: the router, its narrow tools, and answering as its own bot."""

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.beerbot import beerdev, delivery
from src.beerbot.agent import BeerAgent
from src.beerbot.groupme_client import DeliveryResult
from src.beerbot.models import GroupMeMessage
from tests.test_durable_delivery import setup

LINKED = "💡 Queued: Track gym sessions (https://github.com/o/r/issues/31)"
ISSUES = [
    {
        "number": 31,
        "title": "Track gym sessions",
        "state": "open",
        "labels": [{"name": "beerdev"}],
        "body": "Log gym visits.\n\n<!-- beerdev:msg=m0:feature -->",
    },
    {
        "number": 19,
        "title": "Log Zyn consumption",
        "state": "closed",
        "state_reason": "completed",
        "labels": [{"name": "beerdev"}],
        "body": "",
    },
]


def msg(text: str, id: str = "m1", user: str = "pal", reply_to: str | None = None):
    thread = [{"type": "reply", "reply_id": reply_to, "base_reply_id": reply_to}]
    return GroupMeMessage(
        id=id,
        group_id="g1",
        user_id=user,
        sender_id=user,
        sender_type="user",
        name=user.title(),
        text=text,
        created_at=0,
        attachments=thread if reply_to else [],
    )


def tools_for(text: str = "", owner: bool = False, replied: str = "", message_id: str = "m1"):
    ctx = beerdev.Context("g1", message_id, owner, text, replied)
    return ctx, {tool.__name__: tool for tool in beerdev.create_tools(ctx)}


def github(monkeypatch, issues: list[dict], calls: list) -> None:
    def respond(request):
        calls.append((request.method, request.url.path, json.loads(request.content or "null")))
        path = request.url.path
        if request.method == "GET" and path.endswith("/issues"):
            label, state = request.url.params["labels"], request.url.params.get("state", "open")
            found = [
                i
                for i in issues
                if label in {lb["name"] for lb in i["labels"]} and state in ("all", i["state"])
            ]
            return httpx.Response(200, json=found)
        if request.method == "GET" and "/issues/" in path:
            number = int(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=next(i for i in issues if i["number"] == number))
        if request.method == "POST" and path.endswith("/issues"):
            return httpx.Response(201, json={"number": 77})
        return httpx.Response(200, json={})

    monkeypatch.setattr(beerdev.settings, "github_token", "t")
    monkeypatch.setattr(
        beerdev,
        "_github",
        lambda: httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(respond)
        ),
    )


async def test_router_takes_messages_opening_with_beerdev_and_replies_to_its_posts(pg, monkeypatch):
    await setup(pg)
    for text in (
        "beerdev what's our spend?",
        "Beerdev, what's queued?",
        "@beerdev help",
        " @BeerDev: hi",
    ):
        assert await beerdev.addressed(msg(text)), text
    for text in (
        "+1 🍺 thanks beerdev",  # Names beerdev, but it's a drink for Beerius.
        "hey beerdev, ship it",
        "beerdevelopment is hard",
        "beer dev",
        "+1 beer",
    ):
        assert not await beerdev.addressed(msg(text)), text
    line = GroupMeMessage(
        id="dev-1",
        group_id="g1",
        user_id="b",
        sender_id="b",
        sender_type="bot",
        name="beerdev",
        text=LINKED,
        created_at=0,
    )
    await delivery.accept_context(line, "beerdev")
    await delivery.accept_message(msg("+1 beer", id="b-1"))
    await delivery.accept_message(msg("beerdev any errors?", id="q-1", user="kim"), "beerdev")
    agent = BeerAgent()
    monkeypatch.setattr(agent, "process_message", AsyncMock(return_value=None))
    monkeypatch.setattr(beerdev, "process", AsyncMock(return_value="None lately."))
    assert await delivery.execute_one(agent) and await delivery.execute_one(agent)
    assert await beerdev.addressed(msg("veto", id="m2", reply_to="dev-1"))
    assert not await beerdev.addressed(msg("lol", id="m3", reply_to="b-1"))  # Beerius's thread
    assert not await beerdev.addressed(msg("same", id="m4", reply_to="q-1"))  # Kim's question
    await pg.execute("UPDATE message_inbox SET payload=NULL")  # Retention keeps the routing.
    assert await beerdev.addressed(msg("veto", id="m5", reply_to="dev-1"))
    assert await beerdev.replied_text(msg("veto", id="m2", reply_to="dev-1")) == ""


async def test_replied_text_reads_the_post_replied_to(pg):
    await setup(pg)
    line = GroupMeMessage(
        id="dev-1",
        group_id="g1",
        user_id="b",
        sender_id="b",
        sender_type="bot",
        name="beerdev",
        text=LINKED,
        created_at=0,
    )
    await delivery.accept_context(line, "beerdev")
    assert await beerdev.replied_text(msg("veto", id="m2", reply_to="dev-1")) == LINKED
    assert await beerdev.replied_text(msg("veto", id="m3")) == ""


def test_named_and_filed_find_the_right_request():
    issues = ISSUES + [{"number": 40, "title": "Log Zyn consumption", "state": "open"}]
    assert beerdev.named("gym tracker", issues) == 31
    assert beerdev.named("zyn tracker", issues) == 40  # The word beats a stem; open beats shipped.
    assert beerdev.named("espresso martinis", issues) is None
    assert beerdev.filed("m0", "feature", ISSUES)["number"] == 31
    assert beerdev.filed("m0", "bug", ISSUES) is None  # One issue per message and kind,
    assert beerdev.filed("m8", "infra", [{"body": "<!-- beerdev:msg=m8 -->"}])  # old markers: any


async def test_read_doc_reads_only_its_allowlist():
    _, tools = tools_for()
    assert "Beerius" in (await tools["read_doc"]("README.md"))["text"]
    for path in ("../.env", ".env", "/etc/passwd", "src/beerbot/config.py", "README.md/../.env"):
        with pytest.raises(ValueError):
            await tools["read_doc"](path)


async def test_steer_only_does_what_the_owner_wrote(monkeypatch):
    calls, started = [], []
    github(monkeypatch, ISSUES, calls)
    monkeypatch.setattr(beerdev, "dispatch", lambda workflow, every: started.append(workflow))
    for text, owner, replied, kwargs in [
        ("veto", False, LINKED, {"issue": 31}),  # Not the owner.
        ("+2 beers", True, LINKED, {"issue": 31}),  # No veto word: an injected reading.
        ("veto", True, LINKED, {"issue": 19}),  # Not the issue they replied to.
        ("veto it", True, "", {"request": "zyn tracker"}),  # Words they didn't write.
        ("veto", True, "", {"request": "veto"}),  # The action word names nothing.
    ]:
        _, tools = tools_for(text, owner, replied)
        with pytest.raises(ValueError):
            await tools["steer"]("veto", **kwargs)
    assert [method for method, _, _ in calls if method != "GET"] == []  # GitHub untouched.

    _, tools = tools_for("veto", True, LINKED)
    assert (await tools["steer"]("veto", issue=31))["status"] == "vetoed and closed"
    _, tools = tools_for("beerdev veto the zyn tracker", True)
    removal = await tools["steer"]("veto", request="zyn tracker")  # Shipped: a removal build.
    assert removal["status"].startswith("vetoed after shipping") and started == [
        "beerdev-build.yml"
    ]


async def test_file_request_files_once_and_starts_only_buildable_work(monkeypatch):
    calls, started = [], []
    github(monkeypatch, ISSUES, calls)
    monkeypatch.setattr(beerdev, "dispatch", lambda workflow, every: started.append(workflow))
    _, tools = tools_for("beerdev can you track naps?", message_id="m5")
    result = await tools["file_request"]("feature", "Track naps", "Log naps like drinks.")
    assert result["status"] == "filed" and result["url"].endswith("/issues/77")
    posted = next(body for method, _, body in calls if method == "POST")
    assert posted["labels"] == ["beerdev"] and "beerdev:msg=m5:feature" in posted["body"]
    assert started == ["beerdev-build.yml"]

    _, tools = tools_for("beerdev we need a Fly token", message_id="m6")
    result = await tools["file_request"]("infra", "Add a Fly token", "Mint one.")
    assert result["waits_for_owner"] and started == ["beerdev-build.yml"]  # No build for infra.
    with pytest.raises(ValueError):
        await tools["file_request"]("question", "What is queued?", "Asked in chat.")

    _, tools = tools_for("beerdev track gym sessions", message_id="m0")  # A retried message:
    assert (await tools["file_request"]("feature", "Gym", "Again."))["status"] == "already filed"


async def test_request_queue_shows_work_waiting_on_the_owner(monkeypatch):
    def issue(number: int, state: str, *labels: str) -> dict:
        names = [{"name": name} for name in labels]
        return {"number": number, "title": f"#{number}", "state": state, "labels": names}

    held = [
        issue(51, "open", "inbox"),  # A change the reviewer held, waiting on its open PR.
        issue(50, "open", "ops", "inbox"),  # ops' ticket for a person.
        issue(49, "closed", "ops", "inbox"),  # Done: not waiting on anyone.
    ]
    github(monkeypatch, ISSUES + held, [])
    _, tools = tools_for()
    rows = (await tools["request_queue"]())["requests"]
    assert [row["url"].rsplit("/", 1)[1] for row in rows] == ["51", "50", "31", "19"]
    assert "inbox" in rows[0]["labels"]
    # Filing and steering still see only beerdev's own requests.
    async with beerdev._github() as client:
        assert [i["number"] for i in await beerdev._requests(client)] == [31, 19]


async def test_an_answer_in_plain_text_is_still_sent():
    # beerdev must always answer; a model that skips the reply tool used to be dropped.
    from google import genai
    from google.genai import types

    part = types.Part(text="About $0.42 so far this month.")
    answer = types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=[part]), finish_reason="STOP")
        ]
    )
    agent = BeerAgent()
    agent.client = genai.Client(api_key="synthetic")
    agent.client.aio.models.generate_content = AsyncMock(return_value=answer)
    reply = await beerdev.process(msg("beerdev what's our spend?"), agent)
    assert reply == "About $0.42 so far this month."


async def test_app_status_is_curated_and_json_safe(pg):
    await setup(pg)
    await delivery.accept_message(msg("a private message", id="f1"))
    await pg.execute("""UPDATE message_inbox SET state='failed', error_code='model_error',
        error_detail='RuntimeError at agent.py:1'""")
    _, tools = tools_for()
    status = await tools["app_status"]()
    text = json.dumps(status)  # Timestamps and all: safe for the model and the trace.
    assert status["recent_failures"][0]["error_code"] == "model_error"
    assert "a private message" not in text and {"spend", "version"} <= status.keys()


async def test_beerdev_answers_post_as_its_bot_in_the_thread(pg, monkeypatch):
    await setup(pg)
    agent = BeerAgent()
    beerius = AsyncMock(side_effect=AssertionError("Beerius must never see beerdev's messages"))
    monkeypatch.setattr(agent, "process_message", beerius)
    monkeypatch.setattr(beerdev, "process", AsyncMock(return_value="🤓 $0.42 so far."))
    monkeypatch.setattr(delivery.settings, "beerdev_bot_id", "beerdev-bot")
    await delivery.accept_message(msg("beerdev what's our spend?", id="q1"), "beerdev")
    assert await delivery.execute_one(agent)
    sender = SimpleNamespace(deliver_message=AsyncMock(return_value=DeliveryResult("sent")))
    assert await delivery.deliver_one(sender)
    assert sender.deliver_message.await_args.kwargs == {
        "group_id": "g1",
        "bot_id": "beerdev-bot",
        "reply_to": "q1",
    }
    # Beerius's history never claims beerdev's answer as its own: it shows the question only
    # until beerdev's own post comes back as beerdev's.
    monkeypatch.setattr(agent, "process_message", AsyncMock(return_value=None))
    await delivery.accept_message(msg("+1 beer", id="b1"))
    assert await delivery.execute_one(agent)
    history = agent.process_message.await_args.args[1]
    assert [(m.name, m.text) for m in history] == [("Pal", "beerdev what's our spend?")]


def test_the_image_ships_every_doc_beerdev_can_read():
    # read_doc serves files from the image; a doc missing there is an error in chat.
    dockerfile = (beerdev.ROOT / "Dockerfile").read_text()
    copied = " ".join(re.findall(r"^COPY --chown=\S+ (.+?) \./", dockerfile, re.M)).split()
    for doc in beerdev.DOCS:
        assert doc in copied or f"{doc.split('/')[0]}/" in copied, doc
