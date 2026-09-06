"""Bounded, explicit reconciliation of legacy users into shadow identities.

Historical participation creates an observed membership, never authorization.
Existing links, profile fields, roles, and lifecycle states are preserved.
"""

import logging

from .database import get_pool
from .identity import groupme_external_identity_id, groupme_person_id, workspace_membership_id

logger = logging.getLogger(__name__)
LOCK_KEY = 728419302


class ReconciliationBusy(Exception):
    """Another reconciliation is already running."""


async def reconcile_identities(*, apply: bool = False, after_id: int = 0, limit: int = 100) -> dict:
    """Inspect one keyset page; optionally insert missing records atomically.

    A new pass starts at zero. Resume with next_after_id until it is null.
    Reports contain counts and cursors, not names or external identity values.
    """
    if after_id < 0 or not 1 <= limit <= 500:
        raise ValueError("Invalid reconciliation page")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="serializable", readonly=not apply):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            await conn.execute("SET LOCAL lock_timeout = '500ms'")
            if apply and not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", LOCK_KEY):
                raise ReconciliationBusy()
            users = await conn.fetch(
                "SELECT * FROM users WHERE id > $1 ORDER BY id LIMIT $2", after_id, limit + 1
            )
            more = len(users) > limit
            users = users[:limit]
            result = dict(
                mode="apply" if apply else "preview",
                scanned=len(users),
                missing_people=0,
                missing_identities=0,
                missing_bridges=0,
                missing_memberships=0,
                conflicts=0,
                blocked=0,
                unmapped_groups=0,
                next_after_id=users[-1]["id"] if more else None,
            )
            for user in users:
                identity = await conn.fetchrow(
                    """SELECT * FROM external_identities
                       WHERE gateway_type='groupme' AND issuer_key='groupme'
                         AND subject_key=$1""",
                    user["groupme_user_id"],
                )
                if identity and user["person_id"] and identity["person_id"] != user["person_id"]:
                    result["conflicts"] += 1
                    continue
                person_id = (
                    identity["person_id"] if identity else user["person_id"]
                ) or groupme_person_id(user["groupme_user_id"])
                person = await conn.fetchrow("SELECT * FROM people WHERE id=$1", person_id)
                if (identity and identity["status"] != "active") or (
                    person
                    and (
                        person["status"] not in ("provisional", "claimed")
                        or person["canonical_person_id"]
                    )
                ):
                    result["blocked"] += 1
                    continue
                # Avoid a deterministic ID collision with a differently linked identity.
                if not identity and await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM external_identities WHERE id=$1)",
                    groupme_external_identity_id(user["groupme_user_id"]),
                ):
                    result["conflicts"] += 1
                    continue
                result["missing_people"] += int(person is None)
                result["missing_identities"] += int(identity is None)
                result["missing_bridges"] += int(user["person_id"] is None)
                if apply:
                    if not person:
                        await conn.execute(
                            """INSERT INTO people(id,display_name,avatar_url,status)
                               VALUES($1,$2,$3,'provisional')""",
                            person_id,
                            user["name"],
                            user["avatar_url"],
                        )
                    if not identity:
                        await conn.execute(
                            """INSERT INTO external_identities
                               (id,gateway_type,issuer_key,subject_key,person_id,
                                display_name,avatar_url,assurance)
                               VALUES($1,'groupme','groupme',$2,$3,$4,$5,'provisional')""",
                            groupme_external_identity_id(user["groupme_user_id"]),
                            user["groupme_user_id"],
                            person_id,
                            user["name"],
                            user["avatar_url"],
                        )
                    if not user["person_id"]:
                        await conn.execute(
                            "UPDATE users SET person_id=$1 WHERE id=$2 AND person_id IS NULL",
                            person_id,
                            user["id"],
                        )
                groups = await conn.fetch(
                    """SELECT DISTINCT g.workspace_id FROM
                       (SELECT group_id FROM beers WHERE user_id=$1
                        UNION SELECT group_id FROM user_debts WHERE user_id=$1) participation
                       LEFT JOIN groups g USING(group_id)""",
                    user["id"],
                )
                for group in groups:
                    workspace_id = group["workspace_id"]
                    if not workspace_id:
                        result["unmapped_groups"] += 1
                        continue
                    exists = await conn.fetchval(
                        """SELECT EXISTS(SELECT 1 FROM workspace_memberships
                           WHERE workspace_id=$1 AND person_id=$2)""",
                        workspace_id,
                        person_id,
                    )
                    if exists:
                        continue
                    result["missing_memberships"] += 1
                    if apply:
                        await conn.execute(
                            """INSERT INTO workspace_memberships
                               (id,workspace_id,person_id,display_name,status)
                               VALUES($1,$2,$3,$4,'observed')""",
                            workspace_membership_id(workspace_id, person_id),
                            workspace_id,
                            person_id,
                            user["name"],
                        )
        logger.info("Identity reconciliation: %s", result)
        return result
