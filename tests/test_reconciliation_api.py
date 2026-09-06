"""Authentication and execution boundaries of reconciliation endpoints."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.beerbot.main import app, settings
from src.beerbot.reconciliation import ReconciliationBusy


def test_admin_required_before_identity_access(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "test-admin")
    with patch("src.beerbot.main.reconcile_identities", new_callable=AsyncMock) as reconcile:
        with TestClient(app) as client:
            for method, path in [("get", "parity"), ("post", "reconcile")]:
                response = getattr(client, method)(f"/admin/identities/{path}")
                assert response.status_code == 401
        reconcile.assert_not_called()


def test_preview_cannot_apply_and_pages_are_validated(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "test-admin")
    with patch("src.beerbot.main.reconcile_identities", new_callable=AsyncMock) as reconcile:
        reconcile.return_value = {"mode": "preview"}
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer test-admin"}
            response = client.get("/admin/identities/parity?after_id=10&limit=20", headers=headers)
            assert response.status_code == 200
            reconcile.assert_awaited_once_with(after_id=10, limit=20)
            response = client.post("/admin/identities/reconcile?limit=501", headers=headers)
            assert response.status_code == 422
            reconcile.assert_awaited_once()


def test_busy_reconciliation_returns_conflict(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "test-admin")
    with patch("src.beerbot.main.reconcile_identities", new_callable=AsyncMock) as reconcile:
        reconcile.side_effect = ReconciliationBusy()
        with TestClient(app) as client:
            response = client.post(
                "/admin/identities/reconcile", headers={"Authorization": "Bearer test-admin"}
            )
        assert response.status_code == 409
        reconcile.assert_awaited_once_with(apply=True, after_id=0, limit=100)
