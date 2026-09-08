"""Real database tests for native activity and legacy GroupMe compatibility."""

import asyncio
from uuid import uuid4

import asyncpg
import pytest

from tests.test_web import browser as _browser_fixture, login
from src.beerbot import web, activity
from src.beerbot.repositories import beer_repo

browser = _browser_fixture


def request(**values):
    return {"request_id": str(uuid4()), **values}


async def enable(monkeypatch):
    monkeypatch.setattr(web.settings, "app_activity_enabled", True)


async def mirrored_group(pg, *, grant=True):
    async with pg.acquire() as conn:
        await conn.execute("INSERT INTO workspaces(id,name) VALUES('w1','Friends')")
        await conn.execute("UPDATE groups SET workspace_id='w1' WHERE group_id='g1'")
        account_id = await conn.fetchval("SELECT id FROM accounts WHERE person_id='ace'")
    if grant:
        await activity.grant_access("w1", activity.AccessGrant(account_id=account_id))
    return account_id


async def new_drink(browser, workspace="w1", quantity=2):
    payload = request(workspace_id=workspace, quantity=quantity, drink_type="beer", split_the_g=0)
    response = await browser.post("/app/api/activity", json=payload)
    assert response.status_code == 200, response.text
    return payload, response.json()


async def test_native_account_group_log_edit_undo_without_groupme(browser, pg, monkeypatch):
    await enable(monkeypatch)
    invite = await web.invite_account(
        web.InviteInput(name="Native Person", email="native@example.test")
    )
    await browser.post("/app/api/login", json={"email": "native@example.test"})
    code = web.send_code.call_args.args[1]
    assert (await browser.post("/app/api/verify", json={"code": code})).status_code == 200
    group_payload = request(name="Native friends")
    group = (await browser.post("/app/api/groups", json=group_payload)).json()
    assert (await browser.post("/app/api/groups", json=group_payload)).json() == group
    payload, created = await new_drink(browser, group["workspace_id"])
    assert created["mirrored_to_groupme"] is False
    data = (await browser.get("/app/api/me")).json()
    assert data["totals"]["drinks"] == 2
    row = data["activity"][0]
    assert row["editable"] and row["app_entry_id"] == created["entry_id"]
    edit = request(quantity=3, drink_type="wine", split_the_g=1, revision=row["revision"])
    endpoint = "/app/api/activity/" + created["entry_id"]
    assert (await browser.post(endpoint + "/edit", json=edit)).status_code == 200
    assert (
        await browser.post(endpoint + "/edit", json={**edit, "request_id": str(uuid4())})
    ).status_code == 409
    data = (await browser.get("/app/api/me")).json()
    assert data["totals"]["drinks"] == 3 and data["totals"]["splits"] == 1
    undo = request(revision=data["activity"][0]["revision"])
    assert (await browser.post(endpoint + "/undo", json=undo)).status_code == 200
    assert (await browser.post(endpoint + "/undo", json=undo)).status_code == 200
    assert (await browser.post("/app/api/activity", json=payload)).json() == created
    assert (await browser.get("/app/api/me")).json()["totals"]["drinks"] == 0
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM beers") == 0
        assert await conn.fetchval("SELECT count(*) FROM users") == 2
        assert await conn.fetchval(
            "SELECT person_id LIKE 'person:app:%' FROM accounts WHERE id=$1", invite["account_id"]
        )


async def test_mirrored_log_groupme_undo_and_no_resurrection(browser, pg, monkeypatch):
    await enable(monkeypatch)
    await mirrored_group(pg)
    await login(browser)
    payload, created = await new_drink(browser)
    assert created["mirrored_to_groupme"] is True
    async with pg.acquire() as conn:
        uid = await conn.fetchval("SELECT id FROM users WHERE person_id='ace'")
        assert await conn.fetchval("SELECT message_id IS NULL FROM beers")
    assert await beer_repo.get_user_total(uid, "g1") == 2
    assert (await browser.get("/app/api/me")).json()["totals"]["drinks"] == 2
    await beer_repo.delete_last_beer(uid, "g1")
    assert (await browser.get("/app/api/me")).json()["totals"]["drinks"] == 0
    assert (await browser.post("/app/api/activity", json=payload)).json() == created
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM beers") == 0
        assert await conn.fetchval("SELECT count(*) FROM app_activity") == 0
        assert await conn.fetchval("SELECT count(*) FROM message_outbox") == 0


async def test_groupme_edit_invalidates_app_revision_and_app_undo_updates_legacy(
    browser, pg, monkeypatch
):
    await enable(monkeypatch)
    await mirrored_group(pg)
    await login(browser)
    _, created = await new_drink(browser, quantity=4)
    row = (await browser.get("/app/api/me")).json()["activity"][0]
    async with pg.acquire() as conn:
        uid = await conn.fetchval("SELECT id FROM users WHERE person_id='ace'")
    await beer_repo.remove_beers(uid, "g1", 1)
    endpoint = "/app/api/activity/" + created["entry_id"]
    assert (
        await browser.post(
            endpoint + "/edit",
            json=request(quantity=2, drink_type="wine", revision=row["revision"]),
        )
    ).status_code == 409
    current = (await browser.get("/app/api/me")).json()["activity"][0]
    assert current["quantity"] == 3
    assert (
        await browser.post(
            endpoint + "/edit",
            json=request(quantity=2, drink_type="wine", revision=current["revision"]),
        )
    ).status_code == 200
    assert await beer_repo.get_user_total(uid, "g1") == 2
    current = (await browser.get("/app/api/me")).json()["activity"][0]
    assert current["drink_type"] == "wine"
    assert (
        await browser.post(endpoint + "/undo", json=request(revision=current["revision"]))
    ).status_code == 200
    assert await beer_repo.get_user_total(uid, "g1") == 0


async def test_inferred_membership_does_not_authorize_writes_and_test_groups_denied(
    browser, pg, monkeypatch
):
    await enable(monkeypatch)
    account_id = await mirrored_group(pg, grant=False)
    async with pg.acquire() as conn:
        await conn.execute(
            "INSERT INTO workspace_memberships(id,workspace_id,person_id,status,display_name) VALUES('old','w1','ace','active','Ace')"
        )
    await login(browser)
    payload = request(workspace_id="w1")
    assert (await browser.post("/app/api/activity", json=payload)).status_code == 403
    await activity.grant_access("w1", activity.AccessGrant(account_id=account_id))
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE workspaces SET settings='{\"environment\":\"test\"}' WHERE id='w1'"
        )
    assert (await browser.post("/app/api/activity", json=payload)).status_code == 403


async def test_concurrent_retry_only_creates_one_and_changed_payload_conflicts(
    browser, pg, monkeypatch
):
    await enable(monkeypatch)
    await mirrored_group(pg)
    await login(browser)
    payload = request(workspace_id="w1")
    results = await asyncio.gather(
        *(browser.post("/app/api/activity", json=payload) for _ in range(3))
    )
    assert all(r.status_code == 200 for r in results)
    assert len({r.json()["entry_id"] for r in results}) == 1
    assert (
        await browser.post("/app/api/activity", json={**payload, "quantity": 3})
    ).status_code == 409
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM beers") == 1
        assert await conn.fetchval("SELECT count(*) FROM app_activity") == 1


async def test_projection_failure_rolls_back_legacy_and_receipt(browser, pg, monkeypatch):
    await enable(monkeypatch)
    await mirrored_group(pg)
    await login(browser)
    async with pg.acquire() as conn:
        await conn.execute("""CREATE FUNCTION fail_app_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'deliberate failure'; END $$""")
        await conn.execute(
            "CREATE TRIGGER fail_app BEFORE INSERT ON app_activity FOR EACH ROW EXECUTE FUNCTION fail_app_insert()"
        )
    with pytest.raises(asyncpg.RaiseError):
        await browser.post("/app/api/activity", json=request(workspace_id="w1"))
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM beers") == 0
        assert await conn.fetchval("SELECT count(*) FROM app_commands") == 0


async def test_revoked_access_cannot_edit_and_cross_user_entry_hidden(browser, pg, monkeypatch):
    await enable(monkeypatch)
    account_id = await mirrored_group(pg)
    await login(browser)
    _, created = await new_drink(browser)
    row = (await browser.get("/app/api/me")).json()["activity"][0]
    await activity.grant_access("w1", activity.AccessGrant(account_id=account_id, active=False))
    assert (
        await browser.post(
            "/app/api/activity/" + created["entry_id"] + "/undo",
            json=request(revision=row["revision"]),
        )
    ).status_code == 403
    async with pg.acquire() as conn:
        await conn.execute("UPDATE app_activity SET person_id='other'")
    assert (
        await browser.post(
            "/app/api/activity/" + created["entry_id"] + "/undo",
            json=request(revision=row["revision"]),
        )
    ).status_code == 404


async def test_write_flag_csrf_and_validation(browser, pg, monkeypatch):
    await login(browser)
    monkeypatch.setattr(web.settings, "app_activity_enabled", False)
    assert (await browser.post("/app/api/groups", json=request(name="Group"))).status_code == 503
    await enable(monkeypatch)
    assert (
        await browser.post(
            "/app/api/groups", json=request(name="Group"), headers={"Origin": "https://evil.test"}
        )
    ).status_code == 403
    assert (
        await browser.post("/app/api/activity", json=request(workspace_id="w1", quantity=-1))
    ).status_code == 422
    assert (
        await browser.post(
            "/app/api/activity", json=request(workspace_id="w1", split_the_g=2, quantity=1)
        )
    ).status_code == 422


async def test_changed_legacy_identity_cannot_be_modified_by_previous_owner(
    browser, pg, monkeypatch
):
    await enable(monkeypatch)
    await mirrored_group(pg)
    await login(browser)
    _, created = await new_drink(browser)
    row = (await browser.get("/app/api/me")).json()["activity"][0]
    async with pg.acquire() as conn:
        await conn.execute("UPDATE users SET person_id='other' WHERE person_id='ace'")
    assert (
        await browser.post(
            "/app/api/activity/" + created["entry_id"] + "/undo",
            json=request(revision=row["revision"]),
        )
    ).status_code == 409
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM beers") == 1


async def test_unregistered_legacy_workspace_does_not_turn_into_native_group(
    browser, pg, monkeypatch
):
    await enable(monkeypatch)
    await mirrored_group(pg)
    await login(browser)
    async with pg.acquire() as conn:
        await conn.execute("DELETE FROM groups WHERE group_id='g1'")
    assert (
        await browser.post("/app/api/activity", json=request(workspace_id="w1"))
    ).status_code == 409
