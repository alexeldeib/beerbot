"""Deterministic identifiers for shadow global identity records."""


def groupme_person_id(groupme_user_id: str) -> str:
    return f"person:groupme:{groupme_user_id}"


def groupme_external_identity_id(groupme_user_id: str) -> str:
    return f"identity:groupme:{groupme_user_id}"


def workspace_membership_id(workspace_id: str, person_id: str) -> str:
    return f"membership:{workspace_id}:{person_id}"
