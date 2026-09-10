"""Local regression checks: a transport operation must never own the business lock."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket

from app.main import create_app


@pytest.mark.parametrize("path", ["/", "/live", "/tenants", "/history"])
def test_management_pages_disallow_embedding(tmp_path, path):
    with TestClient(create_app(tmp_path / "headers.sqlite3")) as client:
        response = client.get(path)
        assert response.status_code == 200
        assert (
            response.headers.get("Content-Security-Policy") == "frame-ancestors 'none'"
        )
        assert response.headers.get("X-Frame-Options") == "DENY"


def test_replaced_connection_closes_after_releasing_business_lock(
    tmp_path, monkeypatch
):
    app = create_app(tmp_path / "replacement.sqlite3")
    observed = []
    original = WebSocket.close

    async def checked_close(socket, code=1000, reason=None):
        if code == 4001:
            engine = app.state.engine
            observed.append(
                (
                    engine.lock.locked(),
                    engine.clients[("100001", "same")].socket is socket,
                )
            )
        await original(socket, code=code, reason=reason)

    monkeypatch.setattr(WebSocket, "close", checked_close)
    with TestClient(app) as client:
        assert (
            client.post("/api/tenants", json={"tenant_id": "100001"}).status_code == 201
        )
        path = "/ws/client?tenant_id=100001&client_id=same"
        with client.websocket_connect(path) as old:
            assert old.receive_json()["type"] == "round.state"
            with client.websocket_connect(path) as replacement:
                assert replacement.receive_json()["type"] == "round.state"
                # A normal ping traverses the real replacement handler after close scheduling.
                replacement.send_json(
                    {
                        "protocol_version": 1,
                        "type": "ping",
                        "request_id": "replacement-ping",
                        "tenant_id": "100001",
                        "client_id": "same",
                        "round_version": None,
                        "payload": {},
                    }
                )
                while replacement.receive_json().get("type") != "pong":
                    pass
                assert observed == [(False, False)]
