"""Regression cases for externally supplied version boundaries."""

import pytest
from conftest import request
from fastapi.testclient import TestClient
from test_api import receive, send

from app.main import create_app


@pytest.mark.parametrize("version", [0, -1, 2147483647, 10**30])
def test_http_version_boundary_returns_validation_error(tmp_path, version):
    with TestClient(create_app(tmp_path / "boundary.sqlite3")) as client:
        client.post("/api/tenants", json={"tenant_id": "100001"})
        response = client.get(f"/api/tenants/100001/rounds/{version}")
        assert response.status_code == 422
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize("version", [0, -1, 2147483647, 10**30])
def test_ws_version_boundary_rejects_without_disconnecting(tmp_path, version):
    with TestClient(create_app(tmp_path / "boundary.sqlite3")) as client:
        client.post("/api/tenants", json={"tenant_id": "100001"})
        with client.websocket_connect(
            "/ws/client?tenant_id=100001&client_id=device_001"
        ) as ws:
            receive(ws, "round.state")
            reply = send(ws, request("round.get", version=version))
            assert reply["type"] == "error"
            assert reply["payload"]["code"] == "INVALID_MESSAGE"
            assert send(ws, request("ping", version=None))["type"] == "pong"
