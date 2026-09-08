"""Invitation and membership isolation using real PostgreSQL transactions."""

import asyncio

import httpx
import pytest
import pytest_asyncio

from tests.test_web import browser as _browser, login, ORIGIN
from tests.test_activity import request, mirrored_group, new_drink, enable
from src.beerbot.main import app
from src.beerbot import web, app_groups

browser = _browser


@pytest_asyncio.fixture
async def owner(browser, monkeypatch):
    monkeypatch.setattr(web.settings, "app_activity_enabled", True)
    await login(browser)
    response = await browser.post("/app/api/groups", json=request(name="App friends"))
    assert response.status_code == 200
    return browser, response.json()["workspace_id"]


async def invite(owner, email="friend@example.test"):
    client, workspace = owner
    payload = request(email=email)
    response = await client.post(f"/app/api/groups/{workspace}/invite", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["invitation_id"], payload


def client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    )


async def verify_friend(friend, invitation_id, email="friend@example.test"):
    result = await friend.post(
        f"/app/api/invitations/{invitation_id}/prepare", json={"email": email}
    )
    assert result.status_code == 200, result.text
    await friend.post("/app/api/login", json={"email": email})
    code = web.send_code.call_args.args[1]
    assert (await friend.post("/app/api/verify", json={"code": code})).status_code == 200


async def accept(friend, invitation_id):
    payload = request(name="Friend")
    result = await friend.post(f"/app/api/invitations/{invitation_id}/accept", json=payload)
    assert result.status_code == 200, result.text
    return payload


async def test_native_join_name_proof_and_personal_history_isolation(owner, pg):
    invitation, _ = await invite(owner)
    owner_client, workspace = owner
    async with client() as friend:
        preview = (await friend.get("/app/api/invitations/" + invitation)).json()
        assert preview == {"group_name": "App friends", "inviter": "Ace Test"}
        assert (
            await friend.post(
                f"/app/api/invitations/{invitation}/accept", json=request(name="Friend")
            )
        ).status_code == 401
        assert (
            await friend.post(
                f"/app/api/invitations/{invitation}/prepare", json={"email": "wrong@example.test"}
            )
        ).status_code == 403
        await verify_friend(friend, invitation)
        assert (await friend.get("/app/api/groups")).json()["needs_name"]
        assert (
            await friend.post(f"/app/api/invitations/{invitation}/accept", json=request())
        ).status_code == 422
        await accept(friend, invitation)
        assert (await friend.get("/app/api/me")).json()["person"]["name"] == "Friend"
        result = await friend.post(
            "/app/api/activity", json=request(workspace_id=workspace, quantity=3)
        )
        assert result.status_code == 200
        assert (await friend.get("/app/api/me")).json()["totals"]["drinks"] == 3
        assert (await owner_client.get("/app/api/me")).json()["totals"]["drinks"] == 0
        detail = (await owner_client.get("/app/api/groups/" + workspace)).json()
        assert {m["display_name"] for m in detail["members"]} == {"Ace Test", "Friend"}
        assert all("email" not in m for m in detail["members"])
        assert (await friend.get("/app/api/groups/" + workspace)).json()["invitations"] == []
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM users") == 2
        assert await conn.fetchval("SELECT count(*) FROM beers") == 0


@pytest.mark.parametrize(
    "case",
    ["expired", "revoked", "owner_removed", "owner_disabled", "owner_unverified", "test_workspace"],
)
async def test_unavailable_invitation_cannot_create_account(owner, pg, case):
    invitation, _ = await invite(owner)
    _, workspace = owner
    async with pg.acquire() as conn:
        if case == "expired":
            await conn.execute(
                "UPDATE app_group_invitations SET expires_at=now()-interval '1 second'"
            )
        elif case == "revoked":
            await conn.execute("UPDATE app_group_invitations SET revoked_at=now()")
        elif case == "owner_removed":
            await conn.execute("UPDATE app_workspace_access SET active=false")
        elif case == "owner_disabled":
            await conn.execute("UPDATE accounts SET status='disabled'")
        elif case == "owner_unverified":
            await conn.execute("UPDATE account_emails SET verified_at=NULL")
        else:
            await conn.execute(
                "UPDATE workspaces SET settings=jsonb_set(settings,'{environment}','\"test\"') WHERE id=$1",
                workspace,
            )
    async with client() as friend:
        assert (await friend.get("/app/api/invitations/" + invitation)).status_code == 404
        assert (
            await friend.post(
                f"/app/api/invitations/{invitation}/prepare", json={"email": "friend@example.test"}
            )
        ).status_code == 404
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM accounts") == 1


async def test_wrong_verified_email_cannot_join(owner):
    invitation, _ = await invite(owner)
    owner_client, _ = owner
    assert (
        await owner_client.post(f"/app/api/invitations/{invitation}/accept", json=request())
    ).status_code == 403


async def test_remove_revokes_writes_and_replay_does_not_restore_membership(owner):
    invitation, _ = await invite(owner)
    owner_client, workspace = owner
    async with client() as friend:
        await verify_friend(friend, invitation)
        accepted = await accept(friend, invitation)
        members = (await owner_client.get("/app/api/groups/" + workspace)).json()["members"]
        target = next(m["account_id"] for m in members if not m["is_you"])
        assert (
            await owner_client.post(
                f"/app/api/groups/{workspace}/remove", json=request(account_id=target)
            )
        ).status_code == 200
        assert (await friend.get("/app/api/groups/" + workspace)).status_code == 403
        assert (
            await friend.post("/app/api/activity", json=request(workspace_id=workspace))
        ).status_code == 403
        assert (
            await friend.post(f"/app/api/invitations/{invitation}/accept", json=accepted)
        ).status_code == 200
        assert (
            await friend.post(
                f"/app/api/invitations/{invitation}/accept", json=request(name="Friend")
            )
        ).status_code == 409
        assert (
            await friend.post("/app/api/activity", json=request(workspace_id=workspace))
        ).status_code == 403


async def test_transfer_leave_and_owner_protection(owner):
    invitation, _ = await invite(owner)
    owner_client, workspace = owner
    async with client() as friend:
        await verify_friend(friend, invitation)
        await accept(friend, invitation)
        assert (
            await owner_client.post(f"/app/api/groups/{workspace}/leave", json=request())
        ).status_code == 409
        assert (
            await friend.post(f"/app/api/groups/{workspace}/rename", json=request(name="Not owner"))
        ).status_code == 403
        extra, _ = await invite(owner, "another@example.test")
        members = (await owner_client.get("/app/api/groups/" + workspace)).json()["members"]
        target = next(m["account_id"] for m in members if not m["is_you"])
        assert (
            await owner_client.post(
                f"/app/api/groups/{workspace}/transfer", json=request(account_id=target)
            )
        ).status_code == 200
        assert (await friend.get("/app/api/invitations/" + extra)).status_code == 404
        assert (
            await friend.post(f"/app/api/groups/{workspace}/rename", json=request(name="Renamed"))
        ).status_code == 200
        assert (await owner_client.get("/app/api/groups/" + workspace)).json()["name"] == "Renamed"
        assert (
            await owner_client.post(f"/app/api/groups/{workspace}/leave", json=request())
        ).status_code == 200
        assert (
            await friend.post(f"/app/api/groups/{workspace}/leave", json=request())
        ).status_code == 409


async def test_revoke_resend_and_request_id_conflicts(owner):
    invitation, payload = await invite(owner)
    owner_client, workspace = owner
    result = await owner_client.post(f"/app/api/groups/{workspace}/invite", json=payload)
    assert result.json()["invitation_id"] == invitation
    assert (
        await owner_client.post(
            f"/app/api/groups/{workspace}/invite", json={**payload, "email": "changed@example.test"}
        )
    ).status_code == 409
    replacement, _ = await invite(owner)
    assert (await owner_client.get("/app/api/invitations/" + invitation)).status_code == 404
    assert (
        await owner_client.post(
            f"/app/api/groups/{workspace}/invitations/{replacement}/revoke", json=request()
        )
    ).status_code == 200
    assert (await owner_client.get("/app/api/invitations/" + replacement)).status_code == 404


async def test_groupme_and_unrelated_groups_cannot_be_managed(owner, pg):
    owner_client, _ = owner
    async with pg.acquire() as conn:
        await conn.execute("INSERT INTO workspaces(id,name) VALUES('legacy','Legacy')")
        await conn.execute("UPDATE groups SET workspace_id='legacy' WHERE group_id='g1'")
        await conn.execute(
            "INSERT INTO app_workspace_access(account_id,workspace_id,role) SELECT id,'legacy','owner' FROM accounts"
        )
    for action, body in [
        ("rename", request(name="Changed")),
        ("invite", request(email="friend@example.test")),
        ("leave", request()),
    ]:
        assert (
            await owner_client.post(f"/app/api/groups/legacy/{action}", json=body)
        ).status_code == 403
    assert (await owner_client.get("/app/api/groups/unrelated")).status_code == 403
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT name FROM groups WHERE group_id='g1'") == "Friends"


async def test_duplicate_prepare_accept_and_revoke_after_proof(owner, pg):
    invitation, _ = await invite(owner)
    owner_client, workspace = owner
    async with client() as friend:
        responses = await asyncio.gather(
            *(
                friend.post(
                    f"/app/api/invitations/{invitation}/prepare",
                    json={"email": "friend@example.test"},
                )
                for _ in range(3)
            )
        )
        assert all(r.status_code == 200 for r in responses)
        async with pg.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM accounts") == 2
        await verify_friend(friend, invitation)
        await owner_client.post(
            f"/app/api/groups/{workspace}/invitations/{invitation}/revoke", json=request()
        )
        assert (
            await friend.post(
                f"/app/api/invitations/{invitation}/accept", json=request(name="Friend")
            )
        ).status_code == 404


async def test_group_csrf_and_concurrent_transfer_logging(owner, monkeypatch):
    invitation, _ = await invite(owner)
    owner_client, workspace = owner
    assert (
        await owner_client.post(
            f"/app/api/groups/{workspace}/rename",
            json=request(name="bad"),
            headers={"Origin": "https://evil.test"},
        )
    ).status_code == 403
    async with client() as friend:
        await verify_friend(friend, invitation)
        await accept(friend, invitation)
        members = (await owner_client.get("/app/api/groups/" + workspace)).json()["members"]
        target = next(m["account_id"] for m in members if not m["is_you"])
        locked, proceed = asyncio.Event(), asyncio.Event()
        original = app_groups.native_workspace

        async def hold(*args, **kwargs):
            result = await original(*args, **kwargs)
            locked.set()
            await proceed.wait()
            return result

        monkeypatch.setattr(app_groups, "native_workspace", hold)
        transfer = asyncio.create_task(
            owner_client.post(
                f"/app/api/groups/{workspace}/transfer", json=request(account_id=target)
            )
        )
        await asyncio.wait_for(locked.wait(), 2)
        logging = asyncio.create_task(
            friend.post("/app/api/activity", json=request(workspace_id=workspace))
        )
        await asyncio.sleep(0.1)
        proceed.set()
        results = await asyncio.wait_for(asyncio.gather(transfer, logging), 2)
        assert [r.status_code for r in results] == [200, 200]


async def test_reinvite_does_not_restore_revoked_owner_role(owner, pg):
    invitation, _ = await invite(owner)
    owner_client, workspace = owner
    async with client() as friend:
        await verify_friend(friend, invitation)
        await accept(friend, invitation)
        async with pg.acquire() as conn:
            await conn.execute(
                "UPDATE app_workspace_access SET role='owner',active=false WHERE workspace_id=$1 AND account_id=(SELECT account_id FROM account_emails WHERE email='friend@example.test')",
                workspace,
            )
        replacement, _ = await invite(owner)
        await accept(friend, replacement)
        detail = (await friend.get("/app/api/groups/" + workspace)).json()
        assert detail["role"] == "member"


async def test_existing_groupme_person_is_not_renamed_or_replaced_by_invitation(owner, pg):
    invitation, _ = await invite(owner)
    owner_client, _ = owner
    async with client() as friend:
        await verify_friend(friend, invitation)
        await accept(friend, invitation)
        group = (await friend.post("/app/api/groups", json=request(name="Friend-owned"))).json()[
            "workspace_id"
        ]
        second = (
            await friend.post(
                f"/app/api/groups/{group}/invite", json=request(email="ace@example.test")
            )
        ).json()["invitation_id"]
        result = await owner_client.post(
            f"/app/api/invitations/{second}/accept", json=request(name="Different name")
        )
        assert result.status_code == 200
        assert (await owner_client.get("/app/api/me")).json()["person"]["name"] == "Ace Test"
        async with pg.acquire() as conn:
            assert (
                await conn.fetchval("SELECT person_id FROM users WHERE groupme_user_id='1'")
                == "ace"
            )


async def test_groupme_user_then_beer_lock_order_is_preserved(browser, pg, monkeypatch):
    await enable(monkeypatch)
    await mirrored_group(pg)
    await login(browser)
    _, entry = await new_drink(browser, quantity=4)
    revision = (await browser.get("/app/api/me")).json()["activity"][0]["revision"]
    async with pg.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE users SET name='Ace Test' WHERE person_id='ace'")
            editing = asyncio.create_task(
                browser.post(
                    "/app/api/activity/" + entry["entry_id"] + "/edit",
                    json=request(quantity=1, revision=revision),
                )
            )
            await asyncio.sleep(0.1)
            await asyncio.wait_for(conn.execute("UPDATE beers SET quantity=3"), 1)
        response = await asyncio.wait_for(editing, 2)
    assert response.status_code == 409
    assert (await browser.get("/app/api/me")).json()["totals"]["drinks"] == 3
