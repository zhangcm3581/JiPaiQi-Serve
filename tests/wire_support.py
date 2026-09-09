"""Real TCP/HTTP/WebSocket test clients; never touches the operator's live server."""

import json
import os
import random
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]


def shuffled_deck(seed=42):
    # Independent oracle: two standard 52-card decks, no production constants.
    cards = [
        {"rank": rank, "suit": suit}
        for suit in "shcd"
        for rank in ["A", *map(str, range(2, 11)), "J", "Q", "K"]
        for _ in range(2)
    ]
    random.Random(seed).shuffle(cards)
    return cards


def expected_result(deck):
    return Counter((card["suit"], card["rank"]) for card in deck[91:])


def result_counts(cards):
    assert len({(c["suit"], c["rank"]) for c in cards}) == len(cards)
    assert all(type(c["count"]) is int and 1 <= c["count"] <= 2 for c in cards)
    assert sum(c["count"] for c in cards) == 13
    return Counter({(c["suit"], c["rank"]): c["count"] for c in cards})


def ok(reply):
    assert reply["type"] == "ack", reply
    return reply


def error(reply, code):
    assert reply["type"] == "error", reply
    assert reply["payload"]["code"] == code, reply


class WireClient:
    def __init__(self, server, tenant=None, cid=None):
        self.server, self.tenant, self.cid = server, tenant, cid
        path = (
            f"/ws/client?tenant_id={tenant}&client_id={cid}"
            if tenant is not None
            else "/ws/admin"
        )
        self.socket = connect(
            server.url.replace("http://", "ws://") + path,
            open_timeout=5,
            close_timeout=1,
            max_size=2**20,
            proxy=None,
        )
        self.messages, self.sent = [], []
        self.condition = threading.Condition()
        self.closed = False
        self.reader_error = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for raw in self.socket:
                message = json.loads(raw)
                with self.condition:
                    self.messages.append(message)
                    self.condition.notify_all()
        except Exception as exc:
            self.reader_error = str(exc)
        finally:
            with self.condition:
                self.closed = True
                self.condition.notify_all()

    def wait(self, kind=None, predicate=None, after=0, timeout=5):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for value in self.messages[after:]:
                    if (kind is None or value["type"] == kind) and (
                        predicate is None or predicate(value)
                    ):
                        return value
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.closed:
                    raise AssertionError(
                        f"Missing {kind}; closed={self.closed}; error={self.reader_error}; "
                        f"last messages={self.messages[-3:]}"
                    )
                self.condition.wait(remaining)

    def message(self, kind, version=1, payload=None, rid=None):
        return {
            "protocol_version": 1,
            "type": kind,
            "tenant_id": self.tenant,
            "client_id": self.cid,
            "round_version": version,
            "request_id": rid or uuid.uuid4().hex,
            "payload": payload or {},
        }

    def send(self, message):
        self.sent.append(message)
        self.socket.send(json.dumps(message))

    def call_message(self, message):
        mark = len(self.messages)
        self.send(message)
        return self.wait(
            predicate=lambda v: v.get("reply_to") == message["request_id"], after=mark
        )

    def call(self, kind, version=1, payload=None, rid=None):
        return self.call_message(self.message(kind, version, payload, rid))

    def join(self, version=1, previous=None, event=None):
        return self.call(
            "round.join",
            version,
            {
                "start_event_id": event or uuid.uuid4().hex,
                "sync_basis": "initial_start" if previous is None else "end_then_start",
                "previous_round_version": previous,
            },
        )

    def submit(self, cards, version=1, rid=None):
        return self.call("hand.submit", version, {"cards": cards}, rid)

    def result(self, deck, version=1):
        value = self.wait("round.result", lambda v: v["round_version"] == version)
        assert value["tenant_id"] == self.tenant
        assert value["payload"]["remaining_count"] == 13
        assert result_counts(value["payload"]["cards"]) == expected_result(deck)
        return value

    def close(self):
        self.socket.close()
        self.reader.join(timeout=3)


class RealServer:
    def __init__(self, folder):
        self.folder = folder
        self.database = folder / "scenario.sqlite3"
        self.log_path = folder / "server.log"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.http = httpx.Client(base_url=self.url, timeout=10, trust_env=False)
        self.clients, self.evidence = [], []
        self.process = None
        self.log = None
        self.start()

    def start(self):
        self.log = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--workers",
                "1",
                "--ws-max-size",
                "65536",
                "--no-access-log",
            ],
            cwd=ROOT,
            env=dict(os.environ, JPQ_DB_PATH=str(self.database)),
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert self.process.poll() is None, self.log_path.read_text()
            try:
                if self.http.get("/health").json() == {"status": "ok"}:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.025)
        raise AssertionError(self.log_path.read_text())

    def stop(self, crash=False):
        if self.process and self.process.poll() is None:
            self.process.kill() if crash else self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.log:
            self.log.close()

    def restart(self, crash=False):
        self.stop(crash)
        self.start()

    def api(self, method, path, body=None, status=200):
        response = self.http.request(method, path, json=body)
        assert response.status_code == status, response.text
        return response.json()

    def tenant(self, tenant="900001", **settings):
        self.api("POST", "/api/tenants", {"tenant_id": tenant, **settings}, 201)
        return tenant

    def client(self, tenant="900001", cid="device_001", accepted=True):
        peer = WireClient(self, tenant, cid)
        self.clients.append(peer)
        peer.wait("round.state" if accepted else "error")
        return peer

    def admin(self, tenant="900001"):
        peer = WireClient(self)
        self.clients.append(peer)
        peer.wait("admin.snapshot")
        peer.send({"type": "subscribe", "tenant_id": tenant})
        peer.wait("admin.round")
        return peer

    def seven(self, tenant="900001", version=1):
        peers = [self.client(tenant, f"device_{i + 1:03}") for i in range(7)]
        for peer in peers:
            ok(peer.join(version))
        return peers

    def detail(self, tenant="900001", version="current"):
        return self.api("GET", f"/api/tenants/{tenant}/rounds/{version}")

    def note(self, **values):
        self.evidence.append(values)

    def integrity(self):
        with sqlite3.connect(self.database) as db:
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert not db.execute(
                "SELECT tenant_id,version,client_id,count(*) FROM hands "
                "GROUP BY tenant_id,version,client_id HAVING count(*)>1"
            ).fetchall()

    def cleanup(self):
        for peer in self.clients:
            peer.close()
        self.stop()
        self.http.close()
