"""Invite-only first-party access. Never infer ownership from a name or membership.

Admins approve the person/email binding; the person proves mailbox ownership with
a short-lived code bound to the browser which requested it. Legacy GroupMe records
stay authoritative; app-created entries use the transactional compatibility adapter.
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
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from .config import settings
from .database import get_pool
from .activity import NewGroup, NewDrink, EditDrink, UndoDrink, run_command, writable_workspaces
from .activity import Command
from . import app_groups

router = APIRouter()
STATIC = Path(__file__).with_name("static")
SESSION_COOKIE = "beerbot_session"
CHALLENGE_COOKIE = "beerbot_challenge"
EASTERN = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

# Workspaces are the tenant, not the gateway or the person's identity. Missing
# classification preserves existing legacy visibility; explicitly non-production
# workspaces never enter personal app history, even via a direct group filter.
PRODUCTION_WORKSPACE = "coalesce(w.settings->>'environment','production')='production'"
PERSONAL_HISTORY_SCOPE = (
    "FROM personal_activity b WHERE b.person_id=$1 "
    "AND ($2::text IS NULL OR b.group_key=$2) AND b.environment='production'"
)


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
    person_id: str | None = Field(None, min_length=1, max_length=200)
    name: str | None = Field(None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def one_identity(self):
        if bool(self.person_id) == bool(self.name and self.name.strip()):
            raise ValueError("Provide an existing person_id OR a name for a new native person")
        return self


class CodeInput(BaseModel):
    code: str = Field(pattern=r"^\d{8}$", max_length=8)


class FriendInvitation(Command, EmailInput):
    pass


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
            person_id = data.person_id
            if person_id is None:
                person_id = "person:app:" + str(uuid4())
                await conn.execute(
                    "INSERT INTO people(id,display_name) VALUES($1,$2)",
                    person_id,
                    data.name.strip(),
                )
            person = await conn.fetchrow("SELECT * FROM people WHERE id=$1 FOR UPDATE", person_id)
            if (
                not person
                or person["canonical_person_id"]
                or person["status"] not in ("claimed", "provisional")
            ):
                raise HTTPException(409, "Person is unavailable for invitation")
            if await conn.fetchval("SELECT 1 FROM accounts WHERE person_id=$1", person_id):
                raise HTTPException(409, "Person already has an account; no binding was changed")
            account_id = "account:" + secrets.token_hex(16)
            await conn.execute(
                "INSERT INTO accounts(id,person_id,status) VALUES($1,$2,'pending')",
                account_id,
                person_id,
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
    if name not in ("app.css", "app.js", "groups.js"):
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
            """SELECT p.id,p.display_name,e.email,a.id AS account_id FROM web_sessions s
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
            """SELECT DISTINCT group_key AS group_id,group_name AS name FROM personal_activity
               WHERE person_id=$1 AND environment='production'
               UNION SELECT coalesce(g.group_id,w.id),coalesce(g.name,w.name)
               FROM app_workspace_access a JOIN workspaces w ON w.id=a.workspace_id
               LEFT JOIN groups g ON g.workspace_id=w.id
               WHERE a.account_id=$2 AND a.active
               AND coalesce(w.settings->>'environment','production')='production'
               ORDER BY group_id""",
            person["id"],
            person.get("account_id", ""),
        )
        if group is not None and group not in {g["group_id"] for g in groups}:
            raise HTTPException(404, "No personal history for this group")
        scope = PERSONAL_HISTORY_SCOPE
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
            "SELECT b.id,b.app_entry_id,b.revision,b.created_by_account_id,b.workspace_id,"
            "b.quantity,b.drink_type,b.split_the_g,b.logged_at "
            + scope
            + " ORDER BY b.logged_at DESC,b.id DESC LIMIT 30",
            person["id"],
            group,
        )
        writable = await writable_workspaces(conn, person.get("account_id", ""))
        writable_ids = {w["id"] for w in writable}
        entries = []
        for row in activity:
            entry = dict(row)
            entry["editable"] = bool(
                settings.app_activity_enabled
                and row["app_entry_id"]
                and row["created_by_account_id"] == person.get("account_id")
                and row["workspace_id"] in writable_ids
            )
            entry.pop("created_by_account_id")
            entries.append(entry)
    return {
        "person": {"name": person["display_name"], "email": person["email"]},
        "groups": [dict(g) for g in groups],
        "totals": dict(totals),
        "breakdown": [dict(r) for r in breakdown],
        "trend": [dict(r) for r in trend],
        "activity": entries,
        "writable_workspaces": writable,
        "logging_enabled": settings.app_activity_enabled,
        "week_start": week_start.date(),
    }


@router.post("/app/api/groups", dependencies=[Depends(check_origin)])
async def create_app_group(data: NewGroup, person: dict = Depends(signed_in_person)):
    return await run_command(person, "group", data)


@router.post("/app/api/activity", dependencies=[Depends(check_origin)])
async def create_app_drink(data: NewDrink, person: dict = Depends(signed_in_person)):
    return await run_command(person, "create", data)


@router.post("/app/api/activity/{entry_id}/edit", dependencies=[Depends(check_origin)])
async def edit_app_drink(entry_id: UUID, data: EditDrink, person: dict = Depends(signed_in_person)):
    return await run_command(person, "edit", data, entry_id)


@router.post("/app/api/activity/{entry_id}/undo", dependencies=[Depends(check_origin)])
async def undo_app_drink(entry_id: UUID, data: UndoDrink, person: dict = Depends(signed_in_person)):
    return await run_command(person, "undo", data, entry_id)


@router.get("/app/api/groups")
async def app_group_list(person: dict = Depends(signed_in_person)):
    return await app_groups.list_app_groups(person)


@router.get("/app/api/groups/{workspace_id}")
async def app_group_details(workspace_id: str, person: dict = Depends(signed_in_person)):
    return await app_groups.group_details(person, workspace_id)


@router.post("/app/api/groups/{workspace_id}/rename", dependencies=[Depends(check_origin)])
async def rename_app_group(
    workspace_id: str, data: app_groups.RenameGroup, person: dict = Depends(signed_in_person)
):
    return await app_groups.group_command(person, "rename", data, workspace_id)


@router.post("/app/api/groups/{workspace_id}/invite", dependencies=[Depends(check_origin)])
async def invite_app_friend(
    workspace_id: str, data: FriendInvitation, person: dict = Depends(signed_in_person)
):
    return await app_groups.group_command(person, "invite", data, workspace_id)


@router.post(
    "/app/api/groups/{workspace_id}/invitations/{invitation_id}/revoke",
    dependencies=[Depends(check_origin)],
)
async def revoke_app_invitation(
    workspace_id: str, invitation_id: UUID, data: Command, person: dict = Depends(signed_in_person)
):
    return await app_groups.group_command(person, "revoke", data, workspace_id, invitation_id)


@router.post("/app/api/groups/{workspace_id}/remove", dependencies=[Depends(check_origin)])
async def remove_app_member(
    workspace_id: str, data: app_groups.MemberCommand, person: dict = Depends(signed_in_person)
):
    return await app_groups.group_command(person, "remove", data, workspace_id)


@router.post("/app/api/groups/{workspace_id}/transfer", dependencies=[Depends(check_origin)])
async def transfer_app_group(
    workspace_id: str, data: app_groups.MemberCommand, person: dict = Depends(signed_in_person)
):
    return await app_groups.group_command(person, "transfer", data, workspace_id)


@router.post("/app/api/groups/{workspace_id}/leave", dependencies=[Depends(check_origin)])
async def leave_app_group(
    workspace_id: str, data: Command, person: dict = Depends(signed_in_person)
):
    return await app_groups.group_command(person, "leave", data, workspace_id)


@router.get("/app/api/invitations/{invitation_id}")
async def invitation_preview(invitation_id: UUID):
    return await app_groups.preview_invitation(invitation_id)


@router.post("/app/api/invitations/{invitation_id}/prepare", dependencies=[Depends(check_origin)])
async def prepare_friend_account(invitation_id: UUID, data: EmailInput):
    if not email_configured():
        raise HTTPException(503, "Email sign-in is not available yet")
    return await app_groups.prepare_invitation(invitation_id, data.email)


@router.post("/app/api/invitations/{invitation_id}/accept", dependencies=[Depends(check_origin)])
async def accept_friend_invitation(
    invitation_id: UUID, data: app_groups.JoinGroup, person: dict = Depends(signed_in_person)
):
    return await app_groups.group_command(person, "accept", data, invitation_id=invitation_id)
