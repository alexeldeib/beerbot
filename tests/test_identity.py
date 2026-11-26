"""Tests for shadow global identity helpers."""

from src.beerbot.identity import (
    groupme_external_identity_id,
    groupme_person_id,
    workspace_membership_id,
)


def test_groupme_shadow_identity_ids_are_stable():
    person_id = groupme_person_id("user-1")

    assert person_id == "person:groupme:user-1"
    assert groupme_external_identity_id("user-1") == "identity:groupme:user-1"
    assert (
        workspace_membership_id("groupme:group-1", person_id)
        == "membership:groupme:group-1:person:groupme:user-1"
    )
