"""Canonical gateway routing helpers.

Every gateway adapter owns its opaque route-key format. The database only
requires route keys to be stable within a gateway type. This lets multiple
provider routes point at the same workspace without making any provider the
tenant boundary.
"""

GROUPME_GATEWAY_TYPE = "groupme"


def groupme_workspace_id(group_id: str) -> str:
    return f"groupme:{group_id}"


def groupme_connection_id(group_id: str) -> str:
    return f"groupme-bot:{group_id}"


def groupme_route_id(group_id: str) -> str:
    return f"groupme-route:{group_id}"


def groupme_route_key(group_id: str) -> str:
    """GroupMe group IDs are globally unique, so the raw ID is a stable route."""

    return group_id
