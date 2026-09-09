import time
from collections import Counter
from contextlib import ExitStack

import pytest
from conftest import request
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "api.sqlite3")) as client:
        assert (
            client.post("/api/tenants", json={"tenant_id": "100001"}).status_code == 201
        )
        yield client


def receive(ws, kind=None, reply_to=None):
    for _ in range(100):
        data = ws.receive_json()
        if (kind is None or data["type"] == kind) and (
            reply_to is None or data.get("reply_to") == reply_to
        ):
            return data
    raise AssertionError("Expected WebSocket message missing")


def send(ws, message):
    ws.send_json(message)
    return receive(ws, reply_to=message["request_id"])


def start(ws, cid, version=1, previous=None):
    return send(
        ws,
        request(
            "round.join",
            cid,
            version=version,
            payload={
                "start_event_id": f"start-{version}",
                "sync_basis": "initial_start" if previous is None else "end_then_start",
                "previous_round_version": previous,
            },
        ),
    )


def test_pages_and_real_crud(client):
    for path in [
        "/",
        "/live",
        "/tenants",
        "/history",
        "/static/js/components/cards.js",
        "/static/css/app.css",
    ]:
        assert client.get(path).status_code == 200
    assert (
        client.post(
            "/api/tenants",
            json={"tenant_id": "001001", "note": "备注", "round_version": 13},
        ).status_code
        == 201
    )
    assert (
        client.get("/api/tenants/001001/rounds/current").json()["round_version"] == 13
    )
    assert (
        client.patch("/api/tenants/001001", json={"note": "新备注"}).status_code == 200
    )
    assert client.get("/api/tenants").json()[1]["note"] == "新备注"
    assert (
        client.post("/api/tenants/001001/close", json={"round_version": 13}).status_code
        == 200
    )
    assert (
        client.post("/api/tenants/001001/close", json={"round_version": 13}).status_code
        == 409
    )
    assert client.get("/api/rounds?tenant_id=001001&reason=manual").json()["total"] == 1
    assert client.get("/api/rounds?limit=0").status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"tenant_id": 100002},
        {"tenant_id": "100002", "round_version": True},
        {"tenant_id": "100002", "round_version": "2"},
        {"tenant_id": "a"},
        {"tenant_id": "100002", "unknown": 1},
    ],
)
def test_strict_http_fields(client, body):
    assert client.post("/api/tenants", json=body).status_code == 422


def test_http_limits_and_origin(client):
    assert client.patch("/api/tenants/100001", json={"note": None}).status_code == 422
    assert client.post("/api/tenants", content="x" * 65537).status_code == 413
    assert (
        client.post(
            "/api/tenants",
            json={"tenant_id": "3"},
            headers={"Origin": "http://other.test"},
        ).status_code
        == 403
    )
    assert client.get("/api/tenants").headers["cache-control"] == "no-store"


def test_invalid_admin_message_does_not_break_subscription(client):
    with client.websocket_connect("/ws/admin") as admin:
        receive(admin, "admin.snapshot")
        admin.send_json({"type": "subscribe", "tenant_id": {}})
        assert receive(admin, "error")["payload"]["code"] == "INVALID_MESSAGE"
        admin.send_json({"type": "subscribe", "tenant_id": "100001"})
        assert receive(admin, "admin.round")["tenant_id"] == "100001"


def test_binary_frames_close_cleanly(client):
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/ws/admin") as admin:
        receive(admin, "admin.snapshot")
        admin.send_bytes(b"not-json-text")
        with pytest.raises(WebSocketDisconnect) as closed:
            admin.receive_json()
        assert closed.value.code == 1003


def test_live_seven_clients_result_and_history(client, deck):
    with ExitStack() as stack:
        admin = stack.enter_context(client.websocket_connect("/ws/admin"))
        receive(admin, "admin.snapshot")
        admin.send_json({"type": "subscribe", "tenant_id": "100001"})
        receive(admin, "admin.round")
        devices = []
        for n in range(7):
            cid = f"device_{n + 1:03}"
            ws = stack.enter_context(
                client.websocket_connect(f"/ws/client?tenant_id=100001&client_id={cid}")
            )
            devices.append((cid, ws))
            assert receive(ws, "round.state")["round_version"] == 1
            assert start(ws, cid)["type"] == "ack"
        for n, (cid, ws) in enumerate(devices):
            began = time.perf_counter()
            ack = send(
                ws,
                request(
                    "hand.submit", cid, payload={"cards": deck[n * 13 : (n + 1) * 13]}
                ),
            )
            assert ack["type"] == "ack"
        for cid, ws in devices:
            result = receive(ws, "round.result")
            assert result["payload"]["remaining_count"] == 13
            expected = Counter((c["suit"], c["rank"]) for c in deck[91:])
            assert {
                (c["suit"], c["rank"]): c["count"] for c in result["payload"]["cards"]
            } == expected
        latency = time.perf_counter() - began
        assert latency < 2
        print(f"7 clients: seventh hand to all results {latency * 1000:.1f} ms")
        snapshot = receive(admin, "admin.round")
        while snapshot["payload"]["received_count"] < 7:
            snapshot = receive(admin, "admin.round")
        assert len(snapshot["payload"]["devices"]) == 7
        assert all(len(d["cards"]) == 13 for d in snapshot["payload"]["devices"])
        cid, ws = devices[0]
        state = send(ws, request("round.get", cid, version=None))
        assert "devices" not in state["payload"] and "cards" not in state["payload"]
        end = send(ws, request("round.end", cid))
        assert end["payload"]["next_round_version"] == 2
        current = receive(ws, "round.state")
        assert (
            current["round_version"] == 2 and current["payload"]["received_count"] == 0
        )
    old = client.get("/api/tenants/100001/rounds/1").json()
    assert old["state"] == "closed" and old["result"]
    assert client.get("/api/rounds").json()["items"][0]["has_result"]


def test_ws_reconnect_presence_and_strict_messages(client, deck):
    path = "/ws/client?tenant_id=100001&client_id=device_001"
    with client.websocket_connect(path) as ws:
        receive(ws, "round.state")
        assert client.get("/api/tenants").json()[0]["online_count"] == 1
        assert (
            client.delete("/api/tenants/100001/clients/device_001").status_code == 409
        )
        assert (
            send(ws, request("round.get", tenant="999999", version=None))["payload"][
                "code"
            ]
            == "IDENTITY_MISMATCH"
        )
        bad = request("ping", version=None)
        bad["protocol_version"] = True
        assert send(ws, bad)["payload"]["code"] == "INVALID_MESSAGE"
        assert start(ws, "device_001")["type"] == "ack"
        hand = request("hand.submit", payload={"cards": deck[:13]})
        original = send(ws, hand)
        assert original["type"] == "ack"
    with client.websocket_connect(path) as ws:
        state = receive(ws, "round.state")
        assert (
            state["payload"]["received_count"] == 1
            and state["payload"]["bound_round_version"] == 1
        )
        assert send(ws, hand) == original
        wrong = request("hand.submit", payload={"cards": deck[:12]})
        assert send(ws, wrong)["payload"]["code"] == "INVALID_MESSAGE"
        assert client.get("/api/tenants").json()[0]["registered_count"] == 1


def test_replaced_connection_and_unknown_tenant(client):
    with client.websocket_connect("/ws/client?tenant_id=999&client_id=x") as unknown:
        assert receive(unknown, "error")["payload"]["code"] == "TENANT_NOT_FOUND"
    path = "/ws/client?tenant_id=100001&client_id=same"
    with client.websocket_connect(path) as first:
        receive(first, "round.state")
        with client.websocket_connect(path) as second:
            receive(second, "round.state")
            assert send(second, request("ping", "same", version=None))["type"] == "pong"
            assert client.get("/api/tenants").json()[0]["online_count"] == 1


def test_expired_round_closed_before_message_after_restart(tmp_path, deck):
    from conftest import join

    from app.domain import Store

    path = tmp_path / "recovery.sqlite3"
    store = Store(path, clock=lambda: 1000)
    store.create_tenant("100001")
    join(store)
    store.process(
        "100001", "device_001", request("hand.submit", payload={"cards": deck[:13]})
    )
    with TestClient(create_app(path)) as client:
        assert (
            client.get("/api/tenants/100001/rounds/current").json()["round_version"]
            == 2
        )
        assert (
            client.get("/api/tenants/100001/rounds/1").json()["close_reason"]
            == "timeout"
        )
