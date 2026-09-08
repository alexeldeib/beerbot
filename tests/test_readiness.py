from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from src.beerbot import main


def test_readiness_checks_database_and_workers(monkeypatch):
    pool = MagicMock()
    connection = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(main.beer_agent, "client", MagicMock())
    with TestClient(main.app) as client:
        assert client.get("/ready").status_code == 200
        connection.fetchval.side_effect = RuntimeError("database unavailable")
        assert client.get("/ready").status_code == 503
        connection.fetchval.side_effect = None
        main.app.state.message_workers = []
        assert client.get("/ready").status_code == 503
