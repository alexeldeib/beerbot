"""Real PostgreSQL coverage for invitation, proof, sessions, and personal isolation."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from src.beerbot import database, web
from src.beerbot.main import app

ORIGIN = "https://beerbot-groupme.fly.dev"


@pytest_asyncio.fixture
async def browser(pg, monkeypatch):
    await database.init_db()
    monkeypatch.setattr(web.settings, "web_origin", ORIGIN)
    monkeypatch.setattr(web.settings, "environment", "production")
    monkeypatch.setattr(web.settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(web.settings, "smtp_from", "beerbot@example.test")
    monkeypatch.setattr(web, "send_code", AsyncMock())
    async with pg.acquire() as conn:
        await conn.execute(
            "INSERT INTO people(id,display_name) VALUES('ace','Ace Test'),('other','Other')"
        )
        await conn.execute(
            "INSERT INTO users(groupme_user_id,name,person_id) VALUES('1','Ace Test','ace'),('2','Other','other')"
        )
        await conn.execute(
            "INSERT INTO groups(group_id,bot_id,name) VALUES('g1','b1','Friends'),('g2','b2','Other Group')"
        )
    await web.invite_account(web.InviteInput(person_id="ace", email="ace@example.test"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        yield client


async def login(browser):
    assert (
        await browser.post("/app/api/login", json={"email": "ace@example.test"})
    ).status_code == 200
    code = web.send_code.call_args.args[1]
    result = await browser.post("/app/api/verify", json={"code": code})
    assert result.status_code == 200, result.text
    return code


async def test_invite_proof_session_and_legacy_unchanged(browser, pg):
    assert (await browser.get("/app/api/me")).status_code == 401
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM accounts") == "pending"
    code = await login(browser)
    response = await browser.get("/app/api/me")
    assert response.status_code == 200
    assert response.json()["person"] == {"name": "Ace Test", "email": "ace@example.test"}
    assert response.json()["totals"]["drinks"] == 0
    assert response.headers["cache-control"] == "no-store"
    session = browser.cookies.get(web.SESSION_COOKIE)
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT token_hash FROM web_sessions") == web.digest(session)
        assert await conn.fetchval("SELECT verified_at IS NOT NULL FROM account_emails")
        assert await conn.fetchval("SELECT count(*) FROM beers") == 0
        assert await conn.fetchval("SELECT status FROM people WHERE id='ace'") == "provisional"
        assert await conn.fetchval("SELECT count(*) FROM workspace_memberships") == 0
    assert (await browser.post("/app/api/verify", json={"code": code})).status_code == 400
    assert (await browser.post("/app/api/logout", json={})).status_code == 200
    browser.cookies.set(web.SESSION_COOKIE, session)
    assert (await browser.get("/app/api/me")).status_code == 401


async def test_personal_scope_groups_week_boundaries_and_xss(browser, pg):
    async with pg.acquire() as conn:
        # This Monday, last Sunday, and historical. Other person's 99 must never leak.
        await conn.execute("""INSERT INTO beers(user_id,group_id,quantity,drink_type,logged_at)
            SELECT id,'g1',3,'beer',date_trunc('week',now() AT TIME ZONE 'America/New_York')
                AT TIME ZONE 'America/New_York' FROM users WHERE person_id='ace'""")
        await conn.execute("""INSERT INTO beers(user_id,group_id,quantity,drink_type,logged_at)
            SELECT id,'g2',2,'wine',(date_trunc('week',now() AT TIME ZONE 'America/New_York')
                AT TIME ZONE 'America/New_York')-interval '1 second' FROM users WHERE person_id='ace'""")
        await conn.execute("""INSERT INTO beers(user_id,group_id,quantity,drink_type,logged_at)
            SELECT id,'g1',4,'beer',now()-interval '1 year' FROM users WHERE person_id='ace'""")
        await conn.execute("""INSERT INTO beers(user_id,group_id,quantity) SELECT id,'g1',99
            FROM users WHERE person_id='other'""")
    await login(browser)
    data = (await browser.get("/app/api/me")).json()
    assert data["totals"] == {"drinks": 9, "splits": 0, "this_week": 3, "last_week": 2}
    assert len(data["activity"]) == 3
    assert {g["group_id"] for g in data["groups"]} == {"g1", "g2"}
    scoped = (await browser.get("/app/api/me?group=g2")).json()
    assert scoped["totals"]["drinks"] == 2
    assert len(scoped["activity"]) == 1
    assert (await browser.get("/app/api/me?group=unrelated")).status_code == 404
    assert (await browser.get("/app/api/me?person_id=other")).json()["totals"]["drinks"] == 9


async def test_csrf_and_secure_cookie(browser):
    result = await browser.post(
        "/app/api/login",
        json={"email": "ace@example.test"},
        headers={"Origin": "https://evil.test"},
    )
    assert result.status_code == 403
    assert not web.send_code.called
    await login(browser)
    cookie = next(c for c in browser.cookies.jar if c.name == web.SESSION_COOKIE)
    assert cookie.secure and cookie._rest["HttpOnly"] is None
    assert cookie._rest["SameSite"] == "strict"
    assert (
        await browser.post("/app/api/logout", json={}, headers={"Origin": "null"})
    ).status_code == 403


async def test_challenge_browser_binding_attempt_limit_and_expiry(browser, pg):
    await browser.post("/app/api/login", json={"email": "ace@example.test"})
    code = web.send_code.call_args.args[1]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as other:
        assert (await other.post("/app/api/verify", json={"code": code})).status_code == 400
    wrong = "00000000" if code != "00000000" else "11111111"
    for _ in range(5):
        assert (await browser.post("/app/api/verify", json={"code": wrong})).status_code == 400
    assert (await browser.post("/app/api/verify", json={"code": code})).status_code == 400
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT attempts FROM login_challenges") == 5
        await conn.execute(
            "UPDATE login_challenges SET attempts=0,expires_at=now()-interval '1 second'"
        )
    assert (await browser.post("/app/api/verify", json={"code": code})).status_code == 400


async def test_unknown_email_rate_limit_and_delivery_failure(browser, pg):
    unknown = await browser.post("/app/api/login", json={"email": "unknown@example.test"})
    assert not web.send_code.called
    known = await browser.post("/app/api/login", json={"email": "ace@example.test"})
    assert unknown.json() == known.json()
    await browser.post("/app/api/login", json={"email": "ace@example.test"})
    assert web.send_code.call_count == 1
    async with pg.acquire() as conn:
        await conn.execute("UPDATE login_challenges SET created_at=now()-interval '2 minutes'")
    web.send_code.side_effect = RuntimeError("secret SMTP details")
    result = await browser.post("/app/api/login", json={"email": "ace@example.test"})
    assert result.json() == unknown.json()
    code = web.send_code.call_args.args[1]
    assert (await browser.post("/app/api/verify", json={"code": code})).status_code == 400


@pytest.mark.parametrize("change", ["expired", "disabled", "merged"])
async def test_sessions_fail_closed(browser, pg, change):
    await login(browser)
    async with pg.acquire() as conn:
        if change == "expired":
            await conn.execute("UPDATE web_sessions SET expires_at=now()-interval '1 second'")
        elif change == "disabled":
            await conn.execute("UPDATE accounts SET status='disabled'")
        else:
            await conn.execute("UPDATE people SET canonical_person_id='other' WHERE id='ace'")
    assert (await browser.get("/app/api/me")).status_code == 401


async def test_admin_invitation_conflicts_and_access(browser, pg, monkeypatch):
    monkeypatch.setattr(web.settings, "admin_token", "test-admin")
    assert (
        await browser.post(
            "/admin/accounts/invite", json={"person_id": "other", "email": "other@example.test"}
        )
    ).status_code == 401
    for payload in [
        {"person_id": "ace", "email": "different@example.test"},
        {"person_id": "other", "email": "ace@example.test"},
    ]:
        result = await browser.post(
            "/admin/accounts/invite", json=payload, headers={"Authorization": "Bearer test-admin"}
        )
        assert result.status_code == 409
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM accounts") == 1
        assert await conn.fetchval("SELECT email FROM account_emails") == "ace@example.test"


async def test_expired_invitation_cannot_claim(browser, pg):
    async with pg.acquire() as conn:
        await conn.execute("UPDATE account_emails SET invite_expires_at=now()-interval '1 second'")
    await browser.post("/app/api/login", json={"email": "ace@example.test"})
    assert not web.send_code.called


async def test_concurrent_verification_consumes_code_once(browser):
    await browser.post("/app/api/login", json={"email": "ace@example.test"})
    code = web.send_code.call_args.args[1]
    results = await asyncio.gather(
        *(browser.post("/app/api/verify", json={"code": code}) for _ in range(2))
    )
    assert sorted(r.status_code for r in results) == [200, 400]


async def test_unconfigured_email_and_static_security(browser, monkeypatch):
    monkeypatch.setattr(web.settings, "smtp_host", None)
    assert (await browser.get("/app/api/config")).json() == {"email_signin_available": False}
    assert (
        await browser.post("/app/api/login", json={"email": "ace@example.test"})
    ).status_code == 503
    response = await browser.get("/app")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "no-referrer" == response.headers["referrer-policy"]
    assert (await browser.get("/app/assets/app.js")).status_code == 200
    assert (await browser.get("/app/assets/web.py")).status_code == 404


async def test_test_workspaces_excluded_everywhere_and_reversible(browser, pg, monkeypatch):
    monkeypatch.setattr(web.settings, "admin_token", "test-admin")
    async with pg.acquire() as conn:
        await conn.execute(
            "INSERT INTO workspaces(id,name,settings) VALUES('sandbox','Sandbox','{\"other\":true}')"
        )
        await conn.execute("UPDATE groups SET workspace_id='sandbox' WHERE group_id='g2'")
        await conn.execute("""INSERT INTO beers(user_id,group_id,quantity,drink_type,split_the_g)
            SELECT id,'g1',2,'beer',0 FROM users WHERE person_id='ace'""")
        await conn.execute("""INSERT INTO beers(user_id,group_id,quantity,drink_type,split_the_g)
            SELECT id,'g2',10,'wine',3 FROM users WHERE person_id='ace'""")
    await login(browser)
    assert (await browser.get("/app/api/me")).json()["totals"]["drinks"] == 12
    path = "/admin/workspaces/sandbox/environment"
    assert (await browser.patch(path, json={"environment": "test"})).status_code == 401
    headers = {"Authorization": "Bearer test-admin"}
    assert (
        await browser.patch(path, json={"environment": "test"}, headers=headers)
    ).status_code == 200
    result = (await browser.get("/app/api/me")).json()
    assert result["totals"]["drinks"] == 2
    assert result["totals"]["this_week"] == 2
    assert result["totals"]["splits"] == 0
    assert result["groups"] == [{"group_id": "g1", "name": "Friends"}]
    assert result["breakdown"] == [{"drink_type": "beer", "drinks": 2}]
    assert sum(row["drinks"] for row in result["trend"]) == 2
    assert len(result["activity"]) == 1 and result["activity"][0]["drink_type"] == "beer"
    assert (await browser.get("/app/api/me?group=g2")).status_code == 404
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM beers") == 2
        assert (
            await conn.fetchval("SELECT settings->>'other' FROM workspaces WHERE id='sandbox'")
            == "true"
        )
        assert await conn.fetchval("SELECT person_id FROM users WHERE groupme_user_id='1'") == "ace"
    assert (
        await browser.patch(path, json={"environment": "production"}, headers=headers)
    ).status_code == 200
    assert (await browser.get("/app/api/me")).json()["totals"]["drinks"] == 12
    assert (
        await browser.patch(path, json={"environment": "unknown"}, headers=headers)
    ).status_code == 422
    assert (
        await browser.patch(
            "/admin/workspaces/missing/environment", json={"environment": "test"}, headers=headers
        )
    ).status_code == 404
