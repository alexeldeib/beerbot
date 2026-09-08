"""App-only group lifecycle. Invitation IDs are locators, not authentication.

Membership always requires a verified matching email, a live invitation, and a
currently authorized inviter. Nothing here changes legacy GroupMe membership.
"""

import hashlib
import json
from uuid import UUID, uuid4

import asyncpg
from fastapi import HTTPException
from pydantic import Field

from .activity import Command
from .config import settings
from .database import get_pool


class RenameGroup(Command):
    name: str = Field(min_length=1, max_length=80)


class MemberCommand(Command):
    account_id: str = Field(min_length=1, max_length=200)


class JoinGroup(Command):
    name: str | None = Field(None, min_length=1, max_length=100)


def enabled():
    if not settings.app_activity_enabled:
        raise HTTPException(503, "App groups are not enabled")


async def native_workspace(conn, workspace_id: str, account_id: str, owner=False):
    workspace = await conn.fetchrow(
        """SELECT id,name FROM workspaces w WHERE w.id=$1
           AND w.settings->>'activity_mode'='native'
           AND coalesce(w.settings->>'environment','production')='production'
           AND NOT EXISTS(SELECT 1 FROM groups g WHERE g.workspace_id=w.id) FOR UPDATE""",
        workspace_id,
    )
    role = await conn.fetchval(
        "SELECT role FROM app_workspace_access WHERE workspace_id=$1 AND account_id=$2 AND active",
        workspace_id,
        account_id,
    )
    if not workspace or not role or (owner and role != "owner"):
        raise HTTPException(403, "You do not have permission to manage this app group")
    return {**dict(workspace), "role": role}


async def lock_account(conn, person):
    row = await conn.fetchrow(
        """SELECT a.id,a.settings,e.email FROM accounts a JOIN people p ON p.id=a.person_id
           JOIN account_emails e ON e.account_id=a.id WHERE a.id=$1 AND a.person_id=$2
           AND a.status='active' AND e.verified_at IS NOT NULL
           AND p.status IN ('claimed','provisional') AND p.canonical_person_id IS NULL
           FOR UPDATE OF a FOR SHARE OF p""",
        person["account_id"],
        person["id"],
    )
    if not row:
        raise HTTPException(401, "Sign in again to continue")
    return row


async def live_invitation(conn, invitation_id: UUID):
    workspace_id = await conn.fetchval(
        "SELECT workspace_id FROM app_group_invitations WHERE id=$1", invitation_id
    )
    # Workspace-first ordering matches owner mutations and serializes acceptance
    # with remove/leave/transfer/revoke. The ID is never sufficient to join.
    workspace = await conn.fetchrow(
        """SELECT id,name FROM workspaces w WHERE id=$1
           AND settings->>'activity_mode'='native'
           AND coalesce(settings->>'environment','production')='production'
           AND NOT EXISTS(SELECT 1 FROM groups g WHERE g.workspace_id=w.id) FOR UPDATE""",
        workspace_id,
    )
    invitation = await conn.fetchrow(
        """SELECT i.*,p.display_name AS inviter FROM app_group_invitations i
           JOIN app_workspace_access m ON m.account_id=i.created_by_account_id AND m.workspace_id=i.workspace_id
           JOIN accounts a ON a.id=m.account_id JOIN people p ON p.id=a.person_id
           JOIN account_emails e ON e.account_id=a.id
           WHERE i.id=$1 AND i.revoked_at IS NULL AND i.expires_at>now()
           AND m.active AND m.role='owner' AND a.status='active' AND e.verified_at IS NOT NULL
           AND p.status IN ('claimed','provisional') AND p.canonical_person_id IS NULL
           FOR UPDATE OF i""",
        invitation_id,
    )
    if not workspace or not invitation:
        raise HTTPException(404, "Invitation unavailable, expired, or revoked")
    return {**dict(invitation), "group_name": workspace["name"]}


async def preview_invitation(invitation_id: UUID):
    enabled()
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await live_invitation(conn, invitation_id)
        if row["accepted_at"]:
            raise HTTPException(409, "This invitation has already been used")
        return {"group_name": row["group_name"], "inviter": row["inviter"]}


async def prepare_invitation(invitation_id: UUID, email: str):
    """Create a pending native account, without trusting an unverified name."""
    enabled()
    pool = await get_pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            row = await live_invitation(conn, invitation_id)
            if row["email"] != email or row["accepted_at"]:
                raise HTTPException(403, "Use the email address this invitation was made for")
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,728399))", email)
            account = await conn.fetchrow(
                "SELECT a.id,a.status,e.verified_at FROM account_emails e JOIN accounts a ON a.id=e.account_id WHERE e.email=$1",
                email,
            )
            if not account:
                person_id, account_id = "person:app:" + str(uuid4()), "account:" + str(uuid4())
                await conn.execute(
                    "INSERT INTO people(id,display_name) VALUES($1,'New member')", person_id
                )
                await conn.execute(
                    """INSERT INTO accounts(id,person_id,status,settings)
                       VALUES($1,$2,'pending','{"needs_name":true}')""",
                    account_id,
                    person_id,
                )
                await conn.execute(
                    "INSERT INTO account_emails(account_id,email) VALUES($1,$2)", account_id, email
                )
            elif account["status"] not in ("active", "pending"):
                raise HTTPException(403, "This account is unavailable")
            elif account["status"] == "pending" and account["verified_at"] is None:
                # Renew proof eligibility, never alter an existing person/email binding.
                await conn.execute(
                    "UPDATE account_emails SET invite_expires_at=greatest(invite_expires_at,$2) WHERE account_id=$1",
                    account["id"],
                    row["expires_at"],
                )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "Account setup is already in progress; please try again") from None
    return {"status": "verify_email"}


async def list_app_groups(person: dict):
    enabled()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT w.id,w.name,m.role FROM app_workspace_access m JOIN workspaces w ON w.id=m.workspace_id
               WHERE m.account_id=$1 AND m.active AND w.settings->>'activity_mode'='native'
               AND coalesce(w.settings->>'environment','production')='production'
               AND NOT EXISTS(SELECT 1 FROM groups g WHERE g.workspace_id=w.id) ORDER BY w.name,w.id""",
            person["account_id"],
        )
        needs_name = await conn.fetchval(
            "SELECT coalesce((settings->>'needs_name')::boolean,false) FROM accounts WHERE id=$1",
            person["account_id"],
        )
    return {"groups": [dict(r) for r in rows], "needs_name": needs_name}


async def group_details(person: dict, workspace_id: str):
    enabled()
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        group = await native_workspace(conn, workspace_id, person["account_id"])
        members = await conn.fetch(
            """SELECT m.account_id,m.role,p.display_name FROM app_workspace_access m
               JOIN accounts a ON a.id=m.account_id JOIN people p ON p.id=a.person_id
               WHERE m.workspace_id=$1 AND m.active ORDER BY m.role,p.display_name,m.account_id""",
            workspace_id,
        )
        invites = []
        if group["role"] == "owner":
            invites = [
                dict(r)
                for r in await conn.fetch(
                    """SELECT id,email,expires_at FROM app_group_invitations WHERE workspace_id=$1
                   AND revoked_at IS NULL AND accepted_at IS NULL AND expires_at>now()
                   ORDER BY created_at DESC LIMIT 50""",
                    workspace_id,
                )
            ]
    return {
        **group,
        "members": [
            {**dict(r), "is_you": r["account_id"] == person["account_id"]} for r in members
        ],
        "invitations": invites,
    }


async def group_command(
    person: dict,
    action: str,
    command: Command,
    workspace_id: str | None = None,
    invitation_id: UUID | None = None,
):
    enabled()
    payload = {
        "action": "group_" + action,
        "workspace_id": workspace_id,
        "invitation_id": str(invitation_id),
        **command.model_dump(mode="json"),
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='3s'")
        await conn.execute("SET LOCAL statement_timeout='10s'")
        account = await lock_account(conn, person)
        existing = await conn.fetchrow(
            "SELECT fingerprint,result FROM app_commands WHERE account_id=$1 AND request_id=$2",
            account["id"],
            command.request_id,
        )
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise HTTPException(409, "This request ID was used for another action")
            return json.loads(existing["result"])
        if await conn.fetchval(
            "SELECT count(*)>=120 FROM app_commands WHERE account_id=$1 AND created_at>now()-interval '1 hour'",
            account["id"],
        ):
            raise HTTPException(429, "Too many changes; please try again later")
        if action == "accept":
            invite = await live_invitation(conn, invitation_id)
            workspace_id = invite["workspace_id"]
            if invite["email"] != account["email"]:
                raise HTTPException(403, "Sign in using the email this invitation was made for")
            if invite["accepted_at"]:
                # A consumed invitation never reactivates someone removed later.
                raise HTTPException(409, "This invitation has already been used")
            if json.loads(account["settings"]).get("needs_name") is True:
                name = (command.name or "").strip()
                if not name:
                    raise HTTPException(422, "Tell your group what to call you")
                await conn.execute(
                    "UPDATE people SET display_name=$2,updated_at=now() WHERE id=$1",
                    person["id"],
                    name,
                )
                await conn.execute(
                    "UPDATE accounts SET settings=settings-'needs_name',updated_at=now() WHERE id=$1",
                    account["id"],
                )
            await conn.execute(
                """INSERT INTO app_workspace_access(account_id,workspace_id,role) VALUES($1,$2,'member')
                   ON CONFLICT(account_id,workspace_id) DO UPDATE SET
                   role=CASE WHEN app_workspace_access.active THEN app_workspace_access.role ELSE 'member' END,
                   active=true,granted_at=now()""",
                account["id"],
                workspace_id,
            )
            await conn.execute(
                "UPDATE app_group_invitations SET accepted_at=now(),accepted_by_account_id=$2 WHERE id=$1",
                invitation_id,
                account["id"],
            )
            result = {"status": "joined", "workspace_id": workspace_id}
        else:
            await native_workspace(conn, workspace_id, account["id"], owner=action != "leave")
            if action == "rename":
                name = command.name.strip()
                if not name:
                    raise HTTPException(422, "Give your group a name")
                await conn.execute(
                    "UPDATE workspaces SET name=$2,updated_at=now() WHERE id=$1", workspace_id, name
                )
                result = {"status": "renamed"}
            elif action == "invite":
                if await conn.fetchval(
                    """SELECT 1 FROM app_workspace_access m JOIN account_emails e ON e.account_id=m.account_id
                       WHERE m.workspace_id=$1 AND m.active AND e.email=$2""",
                    workspace_id,
                    command.email,
                ):
                    raise HTTPException(409, "That person is already a member")
                if await conn.fetchval(
                    "SELECT count(*)>=50 FROM app_group_invitations WHERE workspace_id=$1 AND created_at>now()-interval '1 day'",
                    workspace_id,
                ):
                    raise HTTPException(429, "Invitation limit reached for today")
                await conn.execute(
                    "UPDATE app_group_invitations SET revoked_at=now() WHERE workspace_id=$1 AND email=$2 AND accepted_at IS NULL AND revoked_at IS NULL",
                    workspace_id,
                    command.email,
                )
                new_id = uuid4()
                await conn.execute(
                    "INSERT INTO app_group_invitations(id,workspace_id,created_by_account_id,email) VALUES($1,$2,$3,$4)",
                    new_id,
                    workspace_id,
                    account["id"],
                    command.email,
                )
                result = {
                    "status": "invited",
                    "invitation_id": str(new_id),
                    "url": settings.web_origin.rstrip("/") + "/app#invite=" + str(new_id),
                }
            elif action == "revoke":
                changed = await conn.fetchval(
                    "UPDATE app_group_invitations SET revoked_at=now() WHERE id=$1 AND workspace_id=$2 AND accepted_at IS NULL RETURNING id",
                    invitation_id,
                    workspace_id,
                )
                if not changed:
                    raise HTTPException(404, "Pending invitation not found")
                result = {"status": "revoked"}
            elif action in ("remove", "leave", "transfer"):
                target = account["id"] if action == "leave" else command.account_id
                role = await conn.fetchval(
                    "SELECT role FROM app_workspace_access WHERE account_id=$1 AND workspace_id=$2 AND active",
                    target,
                    workspace_id,
                )
                if not role:
                    raise HTTPException(404, "Member not found")
                if role == "owner":
                    raise HTTPException(
                        409, "An owner must transfer ownership before leaving or being removed"
                    )
                if action == "transfer":
                    if target == account["id"]:
                        raise HTTPException(409, "Choose another member")
                    if not await conn.fetchval(
                        "SELECT 1 FROM accounts a JOIN account_emails e ON e.account_id=a.id JOIN people p ON p.id=a.person_id WHERE a.id=$1 AND a.status='active' AND e.verified_at IS NOT NULL AND p.canonical_person_id IS NULL AND p.status IN ('claimed','provisional')",
                        target,
                    ):
                        raise HTTPException(409, "That member's account is unavailable")
                    if await conn.fetchval(
                        "SELECT count(*)>=20 FROM app_workspace_access WHERE account_id=$1 AND role='owner'",
                        target,
                    ):
                        raise HTTPException(409, "That member has reached the owned-group limit")
                    await conn.execute(
                        "UPDATE app_workspace_access SET role='owner' WHERE account_id=$1 AND workspace_id=$2",
                        target,
                        workspace_id,
                    )
                    await conn.execute(
                        "UPDATE app_workspace_access SET role='member' WHERE account_id=$1 AND workspace_id=$2",
                        account["id"],
                        workspace_id,
                    )
                    await conn.execute(
                        "UPDATE app_group_invitations SET revoked_at=now() WHERE workspace_id=$1 AND accepted_at IS NULL AND revoked_at IS NULL",
                        workspace_id,
                    )
                else:
                    await conn.execute(
                        "UPDATE app_workspace_access SET active=false WHERE account_id=$1 AND workspace_id=$2",
                        target,
                        workspace_id,
                    )
                result = {
                    "status": {"remove": "removed", "leave": "left", "transfer": "transferred"}[
                        action
                    ]
                }
            else:
                raise HTTPException(400, "Unknown group action")
        await conn.execute(
            "INSERT INTO app_group_events(workspace_id,account_id,action,details) VALUES($1,$2,$3,$4::jsonb)",
            workspace_id,
            account["id"],
            action,
            json.dumps(payload),
        )
        await conn.execute(
            "INSERT INTO app_commands(account_id,request_id,fingerprint,result) VALUES($1,$2,$3,$4::jsonb)",
            account["id"],
            command.request_id,
            fingerprint,
            json.dumps(result),
        )
        return result
