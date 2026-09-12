import sqlite3

import pytest
from conftest import join, request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_api import receive, send

from app.main import create_app

TABLES = ("hands", "starts", "requests", "clients", "rounds")


def seed(store, deck):
    store.create_tenant("100002")
    for tenant in ("100001", "100002"):
        join(store, tenant=tenant)
        store.process(tenant, "device_001", request(
            "hand.submit", tenant=tenant, payload={"cards": deck[:13]}
        ))
        store.close_round(tenant, 1)
        join(store, tenant=tenant, version=2, previous=1)
        store.process(tenant, "device_001", request(
            "hand.submit", tenant=tenant, version=2, payload={"cards": deck[13:26]}
        ))


def snapshot(store, tenant):
    with store.connect() as db:
        return {table: [tuple(row) for row in db.execute(
            f"SELECT * FROM {table} WHERE tenant_id=?", (tenant,)
        )] for table in TABLES}


def test_physical_delete_all_versions_and_isolation(store, deck):
    seed(store, deck)
    other = snapshot(store, "100002")
    assert all(snapshot(store, "100001").values())
    store.delete_tenant("100001")
    assert all(not rows for rows in snapshot(store, "100001").values())
    assert snapshot(store, "100002") == other
    assert [t["tenant_id"] for t in store.tenants()] == ["100002"]
    store.create_tenant("100001")
    assert store.detail("100001")["devices"] == []
    assert store.history("100001")["total"] == 0


def test_delete_rolls_back_on_failure(store, deck):
    seed(store, deck)
    before = snapshot(store, "100001")
    with store.connect(True) as db:
        db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON tenants "
                   "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.delete_tenant("100001")
    assert snapshot(store, "100001") == before
    assert len(store.tenants()) == 2


def test_delete_online_tenant_updates_admin_and_rejects_reconnect(tmp_path):
    with TestClient(create_app(tmp_path / "api.sqlite3")) as client:
        for tenant in ("100001", "100002"):
            assert client.post("/api/tenants", json={"tenant_id": tenant}).status_code == 201
        with client.websocket_connect("/ws/admin") as admin, client.websocket_connect(
            "/ws/client?tenant_id=100001&client_id=device_001"
        ) as ws, client.websocket_connect(
            "/ws/client?tenant_id=100002&client_id=device_001"
        ) as other:
            receive(ws, "round.state")
            receive(other, "round.state")
            admin.send_json({"type": "subscribe", "tenant_id": "100001"})
            receive(admin, "admin.round")
            assert client.delete("/api/tenants/100001").json() == {"ok": True}
            data = receive(admin, "admin.snapshot")
            assert [t["tenant_id"] for t in data["payload"]["tenants"]] == ["100002"]
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    ws.receive_json()
            assert closed.value.code == 4004
            assert send(other, request("ping", tenant="100002", version=None))["type"] == "pong"
            assert client.get("/api/tenants/100001/rounds/current").status_code == 404
            assert client.delete("/api/tenants/100001").status_code == 404
            assert client.get("/api/rounds?tenant_id=100001").json()["total"] == 0
            with client.websocket_connect(
                "/ws/client?tenant_id=100001&client_id=device_001"
            ) as rejected:
                assert receive(rejected, "error")["payload"]["code"] == "TENANT_NOT_FOUND"
            # A later publish must not look up the deleted admin subscription.
            assert client.patch("/api/tenants/100002", json={"note": "still works"}).status_code == 200
