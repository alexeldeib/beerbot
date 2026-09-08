"""Invite-only first-party access. Never infer ownership from a name or membership.

Admins approve the person/email binding; the person proves mailbox ownership with
a short-lived code bound to the browser which requested it. Legacy data is read-only.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .config import settings
from .database import get_pool

router = APIRouter()
STATIC = Path(__file__).with_name("static")
SESSION_COOKIE = "beerbot_session"
CHALLENGE_COOKIE = "beerbot_challenge"
EASTERN = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class EmailInput(BaseModel):
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if value.count("@") != 1 or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("Enter a valid email address")
        local, domain = value.split("@")
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("Enter a valid email address")
        return value


class InviteInput(EmailInput):
    person_id: str = Field(min_length=1, max_length=200)


class CodeInput(BaseModel):
    code: str = Field(pattern=r"^\d{8}$", max_length=8)


def email_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def check_origin(request: Request) -> None:
    # A configured origin, never a client-supplied Host/X-Forwarded-Host.
    if request.headers.get("origin") != settings.web_origin.rstrip("/"):
        raise HTTPException(403, "Untrusted request origin")


def cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        path="/",
        secure=not settings.is_development,
    )


def _send_code(email: str, code: str) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = email
    message["Subject"] = "Your Beerbot sign-in code"
    message.set_content(
        f"Your Beerbot code is {code}.\n\n"
        "Enter it in the browser where you requested it. It expires in 10 minutes.\n"
        "If you did not request this code, ignore this email. Do not share the code.\n"
    )
    context = ssl.create_default_context()
    # No plaintext fallback, including after a TLS or certificate failure.
    if settings.smtp_port == 465:
        client = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=10, context=context
        )
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
    with client:
        if settings.smtp_port != 465:
            client.starttls(context=context)
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password or "")
        client.send_message(message)


async def send_code(email: str, code: str) -> None:
    await asyncio.to_thread(_send_code, email, code)


async def invite_account(data: InviteInput) -> dict:
    """Admin approval is explicit. Never silently reassign an existing binding."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            person = await conn.fetchrow(
                "SELECT * FROM people WHERE id=$1 FOR UPDATE", data.person_id
            )
            if (
                not person
                or person["canonical_person_id"]
                or person["status"] not in ("claimed", "provisional")
            ):
                raise HTTPException(409, "Person is unavailable for invitation")
            if await conn.fetchval("SELECT 1 FROM accounts WHERE person_id=$1", data.person_id):
                raise HTTPException(409, "Person already has an account; no binding was changed")
            account_id = "account:" + secrets.token_hex(16)
            await conn.execute(
                "INSERT INTO accounts(id,person_id,status) VALUES($1,$2,'pending')",
                account_id,
                data.person_id,
            )
            await conn.execute(
                "INSERT INTO account_emails(account_id,email) VALUES($1,$2)",
                account_id,
                data.email,
            )
            return {"status": "invited", "account_id": account_id, "name": person["display_name"]}
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "Email or person already has an account") from None


@router.get("/app")
async def dashboard_page():
    return FileResponse(STATIC / "app.html", media_type="text/html")


@router.get("/app/assets/{name}")
async def dashboard_asset(name: str):
    if name not in ("app.css", "app.js"):
        raise HTTPException(404)
    return FileResponse(STATIC / name)


@router.get("/app/api/config")
async def web_config():
    return {"email_signin_available": email_configured()}


@router.post("/app/api/login", dependencies=[Depends(check_origin)])
async def request_login(data: EmailInput, response: Response):
    if not email_configured():
        raise HTTPException(503, "Email sign-in is not available yet")
    token, code = secrets.token_urlsafe(32), f"{secrets.randbelow(100_000_000):08d}"
    pool = await get_pool()
    send = False
    async with pool.acquire() as conn, conn.transaction():
        # Serialize rate-limit checks across machines. This is separate from the bot queue.
        await conn.execute("SELECT pg_advisory_xact_lock(825437119)")
        await conn.execute("DELETE FROM login_challenges WHERE created_at < now()-interval '1 day'")
        await conn.execute("DELETE FROM web_sessions WHERE expires_at < now()")
        account_id = await conn.fetchval(
            """SELECT a.id FROM account_emails e JOIN accounts a ON a.id=e.account_id
               JOIN people p ON p.id=a.person_id
               WHERE e.email=$1 AND a.status IN ('active','pending')
               AND p.status IN ('claimed','provisional') AND p.canonical_person_id IS NULL
               AND (e.verified_at IS NOT NULL OR e.invite_expires_at>now())""",
            data.email,
        )
        recent = await conn.fetchval(
            "SELECT count(*) FROM login_challenges WHERE created_at>now()-interval '15 minutes'"
        )
        if account_id and recent < 100:
            limited = await conn.fetchval(
                """SELECT count(*)>=3 OR coalesce(max(created_at)>now()-interval '1 minute',false)
                   FROM login_challenges WHERE account_id=$1
                   AND created_at>now()-interval '15 minutes'""",
                account_id,
            )
            if not limited:
                await conn.execute(
                    """INSERT INTO login_challenges(token_hash,account_id,code_hash)
                       VALUES($1,$2,$3)""",
                    digest(token),
                    account_id,
                    digest(token + code),
                )
                send = True
    if send:
        try:
            await send_code(data.email, code)
        except Exception:
            logger.warning("Web sign-in email delivery failed; code invalidated")
            # Never log SMTP errors, recipients, codes, or credentials. Failed codes
            # cannot authenticate; keep the generic response to avoid enumeration.
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE login_challenges SET consumed_at=now() WHERE token_hash=$1",
                    digest(token),
                )
    cookie(response, CHALLENGE_COOKIE, token, 600)
    return {"status": "check_email"}


@router.post("/app/api/verify", dependencies=[Depends(check_origin)])
async def verify_code(data: CodeInput, request: Request, response: Response):
    token = request.cookies.get(CHALLENGE_COOKIE, "")
    pool = await get_pool()
    session = None
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """SELECT c.*,a.status,e.verified_at,e.invite_expires_at>now() AS invited,
                      p.status AS person_status,p.canonical_person_id
               FROM login_challenges c JOIN accounts a ON a.id=c.account_id
               JOIN account_emails e ON e.account_id=a.id JOIN people p ON p.id=a.person_id
               WHERE c.token_hash=$1 AND c.expires_at>now() AND c.consumed_at IS NULL
               AND c.attempts<5 FOR UPDATE OF c,a""",
            digest(token),
        )
        if row:
            await conn.execute(
                "UPDATE login_challenges SET attempts=attempts+1 WHERE token_hash=$1", digest(token)
            )
            if (
                hmac.compare_digest(row["code_hash"], digest(token + data.code))
                and row["status"] in ("active", "pending")
                and row["person_status"] in ("claimed", "provisional")
                and row["canonical_person_id"] is None
                and (row["verified_at"] or row["invited"])
            ):
                await conn.execute(
                    "UPDATE login_challenges SET consumed_at=now() WHERE account_id=$1",
                    row["account_id"],
                )
                await conn.execute(
                    "UPDATE account_emails SET verified_at=coalesce(verified_at,now()) WHERE account_id=$1",
                    row["account_id"],
                )
                await conn.execute(
                    "UPDATE accounts SET status='active',updated_at=now() WHERE id=$1",
                    row["account_id"],
                )
                session = secrets.token_urlsafe(32)
                await conn.execute(
                    "INSERT INTO web_sessions(token_hash,account_id) VALUES($1,$2)",
                    digest(session),
                    row["account_id"],
                )
    # Outside the transaction so failed attempts are committed, not rolled back.
    if session is None:
        raise HTTPException(400, "Code invalid or expired. Request a new code if needed.")
    cookie(response, SESSION_COOKIE, session, 30 * 24 * 3600)
    response.delete_cookie(CHALLENGE_COOKIE, path="/")
    return {"status": "signed_in"}


async def signed_in_person(request: Request) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT p.id,p.display_name,e.email FROM web_sessions s
               JOIN accounts a ON a.id=s.account_id JOIN account_emails e ON e.account_id=a.id
               JOIN people p ON p.id=a.person_id WHERE s.token_hash=$1 AND s.expires_at>now()
               AND a.status='active' AND e.verified_at IS NOT NULL
               AND p.status IN ('claimed','provisional') AND p.canonical_person_id IS NULL""",
            digest(request.cookies.get(SESSION_COOKIE, "")),
        )
    if not row:
        raise HTTPException(401, "Sign in to view your history")
    return dict(row)


@router.post("/app/api/logout", dependencies=[Depends(check_origin)])
async def logout(request: Request, response: Response):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM web_sessions WHERE token_hash=$1",
            digest(request.cookies.get(SESSION_COOKIE, "")),
        )
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "signed_out"}


@router.get("/app/api/me")
async def personal_stats(group: str | None = None, person: dict = Depends(signed_in_person)):
    now = datetime.now(EASTERN)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        groups = await conn.fetch(
            """SELECT DISTINCT g.group_id,g.name FROM groups g JOIN beers b ON b.group_id=g.group_id
               JOIN users u ON u.id=b.user_id WHERE u.person_id=$1 ORDER BY g.group_id""",
            person["id"],
        )
        if group is not None and group not in {g["group_id"] for g in groups}:
            raise HTTPException(404, "No personal history for this group")
        scope = "FROM beers b JOIN users u ON u.id=b.user_id WHERE u.person_id=$1 AND ($2::text IS NULL OR b.group_id=$2)"
        totals = await conn.fetchrow(
            """SELECT coalesce(sum(quantity),0) AS drinks,coalesce(sum(split_the_g),0) AS splits,
               coalesce(sum(quantity) FILTER(WHERE logged_at >= $3),0) AS this_week,
               coalesce(sum(quantity) FILTER(WHERE logged_at >= $3-interval '7 days'
                   AND logged_at < $3),0) AS last_week """
            + scope,
            person["id"],
            group,
            week_start,
        )
        breakdown = await conn.fetch(
            "SELECT drink_type,coalesce(sum(quantity),0) AS drinks "
            + scope
            + " GROUP BY drink_type ORDER BY drinks DESC,drink_type",
            person["id"],
            group,
        )
        trend = await conn.fetch(
            "SELECT date_trunc('week',logged_at AT TIME ZONE 'America/New_York')::date AS week,"
            + "coalesce(sum(quantity),0) AS drinks "
            + scope
            + " AND logged_at >= $3 GROUP BY week ORDER BY week",
            person["id"],
            group,
            week_start - timedelta(weeks=7),
        )
        activity = await conn.fetch(
            "SELECT b.id,b.quantity,b.drink_type,b.split_the_g,b.logged_at "
            + scope
            + " ORDER BY b.logged_at DESC,b.id DESC LIMIT 30",
            person["id"],
            group,
        )
    return {
        "person": {"name": person["display_name"], "email": person["email"]},
        "groups": [dict(g) for g in groups],
        "totals": dict(totals),
        "breakdown": [dict(r) for r in breakdown],
        "trend": [dict(r) for r in trend],
        "activity": [dict(r) for r in activity],
        "week_start": week_start.date(),
    }
