"""Zyn pouch tracking against real PostgreSQL."""

from src.beerbot import database
from src.beerbot.tools import ToolContext, create_tools


def _tools(message_id: str, sender: str = "alice", group: str = "g1"):
    ctx = ToolContext(
        group_id=group,
        message_id=message_id,
        sender_id=sender,
        sender_name=sender.title(),
        sender_avatar_url=None,
        mentioned_users=[],
    )
    return {t.__name__: t for t in create_tools(ctx)}


async def test_log_stats_remove_and_isolation(pg):
    await database.init_db()

    result = await _tools("m1")["log_zyn"](3)
    assert result["logged"] == 3
    # Same message cannot double-log
    assert "error" in await _tools("m1")["log_zyn"](3)
    await _tools("m2")["log_zyn"](2)
    await _tools("m3", sender="bob")["log_zyn"](1)
    await _tools("m4", group="g2")["log_zyn"](9)

    stats = await _tools("m5")["get_zyn_stats"]()
    assert [(e["name"], e["total"], e["today"]) for e in stats["entries"]] == [
        ("Alice", 5, 5),
        ("Bob", 1, 1),
    ]

    assert (await _tools("m6")["remove_zyn"](3))["removed"] == 3
    stats = await _tools("m7")["get_zyn_stats"]()
    assert stats["entries"][0]["total"] == 2
    assert (await _tools("m8")["remove_zyn"](20))["removed"] == 2

    # Drink tables are untouched
    assert await pg.fetchval("SELECT COUNT(*) FROM beers") == 0
