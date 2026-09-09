import random
import uuid

import pytest

from app.domain import RANKS, SUITS, Store


def request(
    action,
    client="device_001",
    tenant="100001",
    version=1,
    payload=None,
    request_id=None,
):
    return {
        "protocol_version": 1,
        "type": action,
        "request_id": request_id or uuid.uuid4().hex,
        "tenant_id": tenant,
        "client_id": client,
        "round_version": version,
        "payload": payload or {},
    }


def join(
    store, client="device_001", tenant="100001", version=1, previous=None, event=None
):
    store.register_client(tenant, client)
    message = request(
        "round.join",
        client,
        tenant,
        version,
        {
            "start_event_id": event or uuid.uuid4().hex,
            "sync_basis": "initial_start" if previous is None else "end_then_start",
            "previous_round_version": previous,
        },
    )
    store.process(tenant, client, message)
    return message


@pytest.fixture
def deck():
    cards = [
        {"suit": suit, "rank": rank}
        for _ in range(2)
        for suit in SUITS
        for rank in RANKS
    ]
    random.Random(42).shuffle(cards)
    return cards


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "test.sqlite3")
    result.create_tenant("100001")
    return result
