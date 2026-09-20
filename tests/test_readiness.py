from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from src.beerbot import main


def test_frequent_readiness_does_not_query_database_and_deep_probe_wakes_workers(monkeypatch):
    pool = MagicMock()
    connection = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(main.beer_agent, "client", MagicMock())
    with TestClient(main.app) as client:
        assert client.get("/ready").status_code == 200
        main.get_pool.assert_not_awaited()
        assert client.get("/ready/db").status_code == 200
        runtime = main.app.state.queue_runtime
        assert all(event.is_set() for event in runtime.events.values())
        connection.fetchval.side_effect = RuntimeError("database unavailable")
        assert client.get("/ready/db").status_code == 503
        assert client.get("/ready").status_code == 503  # known dependency failure
        connection.fetchval.side_effect = None
        assert client.get("/ready/db").status_code == 200
        main.app.state.message_workers = []
        assert client.get("/ready").status_code == 503
