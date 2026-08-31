"""Tests for shadow global identity helpers and resolution."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch

from src.beerbot.identity import (
    groupme_external_identity_id,
    groupme_person_id,
    workspace_membership_id,
)
from src.beerbot.repositories import IdentityRepository


class FakeConnection:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, statement: str, *args):
        return self.row


class FakePool:
    def __init__(self, row):
        self.connection = FakeConnection(row)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def test_groupme_shadow_identity_ids_are_stable():
    person_id = groupme_person_id("user-1")

    assert person_id == "person:groupme:user-1"
    assert groupme_external_identity_id("user-1") == "identity:groupme:user-1"
    assert (
        workspace_membership_id("groupme:group-1", person_id)
        == "membership:groupme:group-1:person:groupme:user-1"
    )


async def test_resolve_returns_person_identity_and_membership():
    now = datetime.now(UTC)
    row = {
        "person_id": "person:groupme:user-1",
        "person_display_name": "Alice",
        "person_avatar_url": None,
        "person_status": "provisional",
        "canonical_person_id": None,
        "person_settings": json.dumps({}),
        "person_created_at": now,
        "person_updated_at": now,
        "identity_id": "identity:groupme:user-1",
        "gateway_type": "groupme",
        "issuer_key": "groupme",
        "subject_key": "user-1",
        "identity_display_name": "Alice",
        "identity_avatar_url": None,
        "assurance": "gateway_asserted",
        "identity_status": "active",
        "identity_metadata": json.dumps({}),
        "first_seen_at": now,
        "last_seen_at": now,
        "membership_id": "membership:groupme:group-1:person:groupme:user-1",
        "workspace_id": "groupme:group-1",
        "membership_display_name": "Alice",
        "membership_role": "member",
        "membership_status": "active",
        "membership_settings": json.dumps({}),
        "joined_at": now,
        "membership_updated_at": now,
    }

    with patch("src.beerbot.repositories.get_pool", return_value=FakePool(row)):
        context = await IdentityRepository().resolve(
            "groupme", "groupme", "user-1", "groupme:group-1"
        )

    assert context is not None
    assert context.person.id == "person:groupme:user-1"
    assert context.external_identity.subject_key == "user-1"
    assert context.membership.workspace_id == "groupme:group-1"


async def test_resolve_returns_none_without_membership():
    with patch("src.beerbot.repositories.get_pool", return_value=FakePool(None)):
        context = await IdentityRepository().resolve(
            "groupme", "groupme", "unknown", "groupme:group-1"
        )

    assert context is None
