"""Native app commands and a narrow, transactional legacy compatibility adapter.

No inferred membership grants access. No fake gateway identities or chat sends.
GroupMe keeps using beers; mirrored app entries read that authoritative row.
"""

import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from .config import settings
from .database import get_pool
from .models import DrinkType


class Command(BaseModel):
    request_id: UUID


class NewGroup(Command):
    name: str = Field(min_length=1, max_length=80)


class DrinkValues(BaseModel):
    quantity: int = Field(1, ge=1, le=100, strict=True)
    drink_type: DrinkType = DrinkType.BEER
    split_the_g: int = Field(0, ge=0, le=100, strict=True)

    @model_validator(mode="after")
    def splits_fit(self):
        if self.split_the_g > self.quantity:
            raise ValueError("Splits cannot exceed the number of drinks")
        return self


class NewDrink(Command, DrinkValues):
    workspace_id: str = Field(min_length=1, max_length=200)


class EditDrink(Command, DrinkValues):
    revision: str = Field(pattern=r"^[a-f0-9]{32}$")


class UndoDrink(Command):
    revision: str = Field(pattern=r"^[a-f0-9]{32}$")


class AccessGrant(BaseModel):
    account_id: str = Field(min_length=1, max_length=200)
    role: Literal["member", "owner"] = "member"
    active: bool = True


async def grant_access(workspace_id: str, grant: AccessGrant) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        if not await conn.fetchval("SELECT 1 FROM workspaces WHERE id=$1", workspace_id):
            raise HTTPException(404, "Workspace not found")
        if not await conn.fetchval("SELECT 1 FROM accounts WHERE id=$1", grant.account_id):
            raise HTTPException(404, "Account not found")
        await conn.execute(
            """INSERT INTO app_workspace_access(account_id,workspace_id,role,active)
               VALUES($1,$2,$3,$4) ON CONFLICT(account_id,workspace_id)
               DO UPDATE SET role=excluded.role,active=excluded.active,granted_at=now()""",
            grant.account_id,
            workspace_id,
            grant.role,
            grant.active,
        )
    return {"workspace_id": workspace_id, "active": grant.active, "role": grant.role}


async def writable_workspaces(conn, account_id: str) -> list[dict]:
    rows = await conn.fetch(
        """SELECT w.id,w.name,coalesce(w.settings->>'activity_mode','groupme') AS activity_mode
           FROM app_workspace_access a JOIN workspaces w ON w.id=a.workspace_id
           WHERE a.account_id=$1 AND a.active
           AND coalesce(w.settings->>'environment','production')='production' ORDER BY w.name,w.id""",
        account_id,
    )
    return [dict(r) for r in rows]


async def authorize_workspace(conn, account_id: str, workspace_id: str):
    # Group management locks workspace before membership; use the same order.
    workspace = await conn.fetchrow(
        """SELECT id,settings->>'activity_mode' AS activity_mode FROM workspaces
           WHERE id=$1 AND coalesce(settings->>'environment','production')='production'
           FOR SHARE""",
        workspace_id,
    )
    access = await conn.fetchrow(
        """SELECT account_id FROM app_workspace_access WHERE account_id=$1
           AND workspace_id=$2 AND active FOR SHARE""",
        account_id,
        workspace_id,
    )
    if not workspace or not access:
        raise HTTPException(403, "You do not have app logging access to this group")
    return workspace


async def run_command(person: dict, action: str, command: Command, entry_id: UUID | None = None):
    if not settings.app_activity_enabled:
        raise HTTPException(503, "App logging is not enabled")
    payload = {"action": action, "entry_id": str(entry_id), **command.model_dump(mode="json")}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='3s'")
        await conn.execute("SET LOCAL statement_timeout='10s'")
        account_id = person["account_id"]
        # Serializes this account's retries/edits and synchronizes account revocation.
        account = await conn.fetchrow(
            """SELECT a.id FROM accounts a JOIN people p ON p.id=a.person_id
               JOIN account_emails e ON e.account_id=a.id WHERE a.id=$1 AND a.person_id=$2
               AND a.status='active' AND e.verified_at IS NOT NULL
               AND p.status IN ('claimed','provisional') AND p.canonical_person_id IS NULL
               FOR UPDATE OF a FOR SHARE OF p""",
            account_id,
            person["id"],
        )
        if not account:
            raise HTTPException(401, "Sign in again to continue")
        existing = await conn.fetchrow(
            "SELECT fingerprint,result FROM app_commands WHERE account_id=$1 AND request_id=$2",
            account_id,
            command.request_id,
        )
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise HTTPException(409, "This request ID was already used for a different action")
            return json.loads(existing["result"])
        if await conn.fetchval(
            "SELECT count(*)>=120 FROM app_commands WHERE account_id=$1 AND created_at>now()-interval '1 hour'",
            account_id,
        ):
            raise HTTPException(429, "Too many changes. Please try again later.")

        if action == "group":
            name = command.name.strip()
            if not name:
                raise HTTPException(422, "Give your group a name")
            if await conn.fetchval(
                "SELECT count(*)>=20 FROM app_workspace_access WHERE account_id=$1 AND role='owner'",
                account_id,
            ):
                raise HTTPException(409, "Group creation limit reached")
            workspace_id = "app-group:" + str(uuid4())
            await conn.execute(
                'INSERT INTO workspaces(id,name,settings) VALUES($1,$2,\'{"environment":"production","activity_mode":"native"}\')',
                workspace_id,
                name,
            )
            await conn.execute(
                "INSERT INTO app_workspace_access(account_id,workspace_id,role) VALUES($1,$2,'owner')",
                account_id,
                workspace_id,
            )
            result = {"workspace_id": workspace_id, "name": name}
        elif action == "create":
            workspace = await authorize_workspace(conn, account_id, command.workspace_id)
            groups = await conn.fetch(
                "SELECT group_id FROM groups WHERE workspace_id=$1 FOR SHARE", command.workspace_id
            )
            legacy_id = None
            if workspace["activity_mode"] == "native":
                if groups:
                    raise HTTPException(
                        409, "Native group gateway mapping requires administrator review"
                    )
            else:
                # Multi-route projection needs an explicit destination policy; never duplicate.
                users = await conn.fetch(
                    "SELECT id FROM users WHERE person_id=$1 FOR SHARE", person["id"]
                )
                if len(groups) != 1 or len(users) != 1:
                    raise HTTPException(
                        409, "This group needs an administrator to configure its identity mapping"
                    )
                legacy_id = await conn.fetchval(
                    """INSERT INTO beers(user_id,group_id,quantity,drink_type,split_the_g)
                       VALUES($1,$2,$3,$4,$5) RETURNING id""",
                    users[0]["id"],
                    groups[0]["group_id"],
                    command.quantity,
                    command.drink_type.value,
                    command.split_the_g,
                )
            new_id = uuid4()
            await conn.execute(
                """INSERT INTO app_activity(id,person_id,workspace_id,created_by_account_id,
                   legacy_beer_id,quantity,drink_type,split_the_g)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8)""",
                new_id,
                person["id"],
                command.workspace_id,
                account_id,
                legacy_id,
                command.quantity,
                command.drink_type.value,
                command.split_the_g,
            )
            result = {
                "entry_id": str(new_id),
                "status": "created",
                "mirrored_to_groupme": legacy_id is not None,
            }
        else:
            row = await conn.fetchrow(
                "SELECT * FROM app_activity WHERE id=$1 AND person_id=$2 AND created_by_account_id=$3 AND deleted_at IS NULL",
                entry_id,
                person["id"],
                account_id,
            )
            if not row:
                raise HTTPException(404, "Entry not found; it may already have been undone")
            await authorize_workspace(conn, account_id, row["workspace_id"])
            # Same lock order as GroupMe's DELETE + FK cascade: beers, then app_activity.
            if row["legacy_beer_id"] is not None:
                # GroupMe first upserts the user, then updates/deletes their beer.
                # Lock that identity before the beer so concurrent edits cannot
                # deadlock with the unchanged GroupMe transaction.
                await conn.fetchrow(
                    """SELECT u.id FROM users u JOIN beers b ON b.user_id=u.id
                       WHERE b.id=$1 FOR SHARE OF u""",
                    row["legacy_beer_id"],
                )
                legacy = await conn.fetchrow(
                    """SELECT b.*,u.person_id AS current_person_id,g.workspace_id AS current_workspace_id
                       FROM beers b JOIN users u ON u.id=b.user_id JOIN groups g ON g.group_id=b.group_id
                       WHERE b.id=$1 FOR UPDATE OF b FOR SHARE OF u,g""",
                    row["legacy_beer_id"],
                )
                if not legacy:
                    raise HTTPException(404, "Entry already removed in GroupMe")
                if (
                    legacy["current_person_id"] != person["id"]
                    or legacy["current_workspace_id"] != row["workspace_id"]
                ):
                    raise HTTPException(
                        409,
                        "The entry's identity or group mapping changed; ask an administrator to review it",
                    )
            await conn.fetchrow("SELECT id FROM app_activity WHERE id=$1 FOR UPDATE", entry_id)
            current = await conn.fetchrow(
                "SELECT * FROM personal_activity WHERE app_entry_id=$1", entry_id
            )
            if not current or current["revision"] != command.revision:
                raise HTTPException(
                    409, "This entry changed. Refresh and review it before trying again."
                )
            if action == "undo":
                if row["legacy_beer_id"] is not None:
                    await conn.execute("DELETE FROM beers WHERE id=$1", row["legacy_beer_id"])
                else:
                    await conn.execute(
                        "UPDATE app_activity SET deleted_at=now(),version=version+1 WHERE id=$1",
                        entry_id,
                    )
            elif action == "edit":
                if row["legacy_beer_id"] is not None:
                    await conn.execute(
                        "UPDATE beers SET quantity=$2,drink_type=$3,split_the_g=$4 WHERE id=$1",
                        row["legacy_beer_id"],
                        command.quantity,
                        command.drink_type.value,
                        command.split_the_g,
                    )
                await conn.execute(
                    "UPDATE app_activity SET quantity=$2,drink_type=$3,split_the_g=$4,version=version+1 WHERE id=$1",
                    entry_id,
                    command.quantity,
                    command.drink_type.value,
                    command.split_the_g,
                )
            else:
                raise HTTPException(400, "Unknown activity command")
            result = {
                "entry_id": str(entry_id),
                "status": "undone" if action == "undo" else "updated",
            }
        # Durable receipt survives a GroupMe deletion of the mirrored row. Retrying
        # a completed create can therefore never resurrect an undone drink.
        await conn.execute(
            "INSERT INTO app_commands(account_id,request_id,fingerprint,result) VALUES($1,$2,$3,$4::jsonb)",
            account_id,
            command.request_id,
            fingerprint,
            json.dumps(result),
        )
        return result
