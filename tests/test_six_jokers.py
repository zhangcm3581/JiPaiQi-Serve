import uuid
from collections import Counter

import pytest
from conftest import request

from app.domain import DomainError, deck_for
from app.protocol import parse_message


def submit(store, cid, cards, mode="six_jokers", version=1):
    store.register_client("100001", cid)
    return store.process(
        "100001",
        cid,
        parse_message(
            request(
                "hand.submit",
                cid,
                version=version,
                payload={
                    "cards": cards,
                    "mode": mode,
                    "start_event_id": uuid.uuid4().hex,
                },
            )
        ),
    )


def cards_for(mode):
    return [
        {"suit": s, "rank": r} for (s, r), n in deck_for(mode).items() for _ in range(n)
    ]


def test_six_jokers_remaining_and_round_mode(store):
    cards = cards_for("six_jokers")
    assert len(cards) == 104
    for i in range(7):
        submit(store, str(i), cards[i * 13 : i * 13 + 13])
    detail = store.detail("100001")
    assert detail["mode"] == "six_jokers"
    assert sum(c["count"] for c in detail["result"]) == 13
    assert Counter(
        {(c["suit"], c["rank"]): c["count"] for c in detail["result"]}
    ) == Counter((c["suit"], c["rank"]) for c in cards[91:])
    assert {c["rank"]: c["count"] for c in detail["result"] if c["suit"] == "j"} == {
        "small": 3,
        "big": 3,
    }


def test_mixed_modes_rollback(store):
    cards = cards_for("six_jokers")
    submit(store, "a", cards[:13])
    with pytest.raises(DomainError, match="本轮模式不同"):
        submit(store, "b", cards[13:26], "standard")
    assert store.detail("100001")["received_count"] == 1


def test_invalid_first_hand_does_not_lock_mode(store):
    cards = cards_for("standard")
    with pytest.raises(DomainError):
        submit(store, "a", cards[:13])  # duplicated reduced spades
    assert store.detail("100001")["mode"] is None
    submit(store, "a", cards[:13], "standard")


def test_jokers_not_allowed_in_standard(store):
    cards = cards_for("six_jokers")[-13:]
    with pytest.raises(DomainError):
        submit(store, "a", cards, "standard")


def test_collective_reduced_spade_overflow(store):
    cards = cards_for("six_jokers")
    submit(store, "a", cards[:13])
    other = cards[13:26]
    other[0] = {"suit": "s", "rank": "2"}
    with pytest.raises(DomainError):
        submit(store, "b", other)
    assert store.detail("100001")["received_count"] == 1


def test_three_captured_rounds_reconstruct_each_missing_player(tmp_path):
    import json
    from pathlib import Path

    from app.domain import Store

    rounds = json.loads(
        (Path(__file__).parent / "fixtures/six-joker-replay.json").read_text()
    )
    assert len(rounds) == 3

    def decode(values):
        return [
            {"suit": "j", "rank": "small" if v == 14 else "big"}
            if v in (14, 15)
            else {
                "suit": ["s", "h", "c", "d"][(v >> 4) - 1],
                "rank": {14: "A", 13: "K", 12: "Q", 11: "J"}.get(v & 15, str(v & 15)),
            }
            for v in values
        ]

    for r, hands in enumerate(rounds):
        for omitted in range(8):
            store = Store(tmp_path / f"{r}-{omitted}.db")
            store.create_tenant("100001")
            for i, hand in enumerate(hands):
                if i != omitted:
                    submit(store, str(i), decode(hand))
            result = store.detail("100001")["result"]
            assert Counter(
                {(c["suit"], c["rank"]): c["count"] for c in result}
            ) == Counter((c["suit"], c["rank"]) for c in decode(hands[omitted]))


@pytest.mark.parametrize("mode", ["six_jokers", "two_jokers"])
def test_real_wire_six_jokers_and_switch_to_standard(tmp_path, mode):
    from wire_support import RealServer, ok

    server = RealServer(tmp_path)
    try:
        tenant = server.tenant()
        clients = [server.client(tenant, str(i)) for i in range(7)]
        assert (
            "deck.mode/1" in clients[0].wait("round.state")["payload"]["capabilities"]
        )
        deck = cards_for(mode)
        for i, c in enumerate(clients):
            ok(
                c.call(
                    "hand.submit",
                    1,
                    {
                        "cards": deck[i * 13 : i * 13 + 13],
                        "mode": mode,
                        "start_event_id": f"six-{i}",
                    },
                )
            )
        for c in clients:
            result = c.wait("round.result")["payload"]
            assert result["mode"] == mode
            assert sum(x["count"] for x in result["cards"] if x["suit"] == "j") == (
                6 if mode == "six_jokers" else 2
            )
        ok(clients[0].call("round.compare"))
        for c in clients:
            c.wait("round.phase", lambda x: x["payload"]["phase"] == "comparing")
        ok(clients[0].call("round.end"))
        for c in clients:
            state = c.wait("round.state", lambda x: x["round_version"] == 2)
            assert state["payload"]["mode"] is None
        # Legacy standard clients omit mode entirely.
        deck = cards_for("standard")
        for i, c in enumerate(clients):
            ok(
                c.call(
                    "hand.submit",
                    2,
                    {
                        "cards": deck[i * 13 : i * 13 + 13],
                        "start_event_id": f"normal-{i}",
                    },
                )
            )
        for c in clients:
            assert (
                c.wait("round.result", lambda x: x["round_version"] == 2)["payload"][
                    "mode"
                ]
                == "standard"
            )
        server.integrity()
    finally:
        server.cleanup()


def test_two_joker_captured_round_all_missing_players(tmp_path):
    import json
    from pathlib import Path

    from app.domain import Store

    raw = json.loads(
        (Path(__file__).parent / "fixtures/two-joker-replay.json").read_text()
    )[0]
    hands = [
        [
            {"suit": "j", "rank": "small" if v == 14 else "big"}
            if v in (14, 15)
            else {
                "suit": "shcd"[(v >> 4) - 1],
                "rank": {14: "A", 13: "K", 12: "Q", 11: "J"}.get(v & 15, str(v & 15)),
            }
            for v in h
        ]
        for h in raw
    ]
    for omitted in range(8):
        store = Store(tmp_path / f"two-{omitted}.db")
        store.create_tenant("100001")
        for i, hand in enumerate(hands):
            if i != omitted:
                submit(store, str(i), hand, "two_jokers")
        result = store.detail("100001")["result"]
        assert Counter({(x["suit"], x["rank"]): x["count"] for x in result}) == Counter(
            (x["suit"], x["rank"]) for x in hands[omitted]
        )


def test_two_joker_limits_and_mode_isolation(store):
    from app.domain import normalize_hand

    deck = deck_for("two_jokers")
    assert (
        sum(deck.values()) == 104
        and deck[("j", "big")] == 1
        and deck[("j", "small")] == 1
    )
    assert deck[("s", "2")] == 1 and deck[("s", "3")] == 1 and deck[("s", "4")] == 2
    hand = cards_for("two_jokers")[-13:]
    bad = hand.copy()
    bad[0] = {"suit": "j", "rank": "big"}
    with pytest.raises(DomainError):
        normalize_hand(bad, "two_jokers")
    submit(store, "one", hand, "two_jokers")
    with pytest.raises(DomainError, match="本轮模式不同"):
        submit(store, "two", cards_for("six_jokers")[:13], "six_jokers")
