import copy
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import join, request

from app.domain import DomainError, Store


def fails(code, fn):
    with pytest.raises(DomainError) as caught:
        fn()
    assert caught.value.code == code


def submit(store, cards, client="device_001", tenant="100001", version=1):
    return store.process(
        tenant,
        client,
        request("hand.submit", client, tenant, version, {"cards": cards}),
    )


def test_tenants_have_independent_versions_and_preserve_zeros(store, deck):
    store.create_tenant("001001", round_version=1)
    join(store)
    submit(store, deck[:13])
    store.process("100001", "device_001", request("round.end"))
    assert store.detail("100001")["round_version"] == 2
    assert store.detail("001001")["round_version"] == 1
    assert store.detail("001001")["received_count"] == 0
    fails("TENANT_EXISTS", lambda: store.create_tenant("001001"))


def test_seven_hands_leave_exact_multiset_and_lock_without_advancing(store, deck):
    for index in range(7):
        cid = f"device_{index + 1:03}"
        join(store, cid)
        submit(store, deck[index * 13 : (index + 1) * 13], cid)
        d = store.detail("100001")
        assert d["received_count"] == index + 1
        assert (d["result"] is None) == (index < 6)
    expected = Counter((c["suit"], c["rank"]) for c in deck[91:])
    assert {(c["suit"], c["rank"]): c["count"] for c in d["result"]} == expected
    assert d["state"] == "ready" and d["round_version"] == 1
    assert sum(c["count"] for c in d["result"]) == 13
    assert d["calculation_ms"] < 2000
    fails("HAND_CONFLICT", lambda: submit(store, deck[13:26]))


@pytest.mark.parametrize("count", [0, 12, 14, 26])
def test_only_thirteen_cards_are_accepted(store, deck, count):
    join(store)
    fails("INVALID_MESSAGE", lambda: submit(store, deck[:count]))
    assert store.detail("100001")["received_count"] == 0
    assert store.detail("100001")["deadline_at_ms"] is None


def test_duplicate_rank_suit_is_preserved_but_third_copy_rejected(store):
    join(store)
    bad = [{"rank": "A", "suit": "s"}] * 13
    fails("DECK_OVERFLOW", lambda: submit(store, bad))
    assert store.detail("100001")["received_count"] == 0


def test_deck_overflow_rolls_back_last_submission(store):
    cards = [
        {"rank": r, "suit": "s"}
        for r in ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
    ]
    for cid in ["one", "two", "three"]:
        join(store, cid)
    submit(store, cards, "one")
    submit(
        store, cards, "two"
    )  # Identical hands from different devices can be valid in two decks.
    before = store.detail("100001")
    fails("DECK_OVERFLOW", lambda: submit(store, cards, "three"))
    assert store.detail("100001") == before


def test_idempotent_ack_survives_restart_and_canonical_hand_order(store, deck):
    join(store)
    message = request("hand.submit", payload={"cards": deck[:13]})
    ack = store.process("100001", "device_001", message)
    reopened = Store(store.path)
    assert reopened.process("100001", "device_001", message) == ack
    assert submit(reopened, list(reversed(deck[:13])))["payload"]["duplicate"]
    changed = copy.deepcopy(message)
    changed["payload"]["cards"] = deck[13:26]
    fails("REQUEST_CONFLICT", lambda: reopened.process("100001", "device_001", changed))
    assert reopened.detail("100001")["received_count"] == 1


def test_end_closes_incomplete_round_once_and_rejects_old_hand(store, deck):
    join(store)
    submit(store, deck[:13])
    end = request("round.end")
    first = store.process("100001", "device_001", end)
    assert store.process("100001", "device_001", end) == first
    assert store.detail("100001")["round_version"] == 2
    fails("ROUND_CLOSED", lambda: submit(store, deck[:13]))
    fails(
        "ROUND_CLOSED",
        lambda: store.process("100001", "device_001", request("round.end")),
    )
    history = store.detail("100001", 1)
    assert history["state"] == "closed" and history["result"] is None
    assert history["close_reason"] == "game_end" and history["received_count"] == 1
    join(store, version=2, previous=1)
    submit(store, deck[13:26], version=2)
    assert store.detail("100001", 1) == history


def test_new_version_cannot_receive_old_or_unbound_data(store, deck):
    original = join(store, event="same-event")
    store.close_round("100001", 1)
    fails("NOT_JOINED", lambda: submit(store, deck[:13], version=2))
    changed = copy.deepcopy(original)
    changed.update(request_id="other-request", round_version=2)
    fails(
        "START_EVENT_CONFLICT", lambda: store.process("100001", "device_001", changed)
    )
    fails("SYNC_REQUIRED", lambda: join(store, version=2))
    join(store, version=2, previous=1)
    store.update_tenant("100001", {"round_version": 8})
    fails("SYNC_REQUIRED", lambda: join(store, version=8, previous=1))
    join(store, version=8, previous=2)


def test_unregistered_or_unjoined_device_cannot_end_round(store):
    fails(
        "CLIENT_NOT_FOUND",
        lambda: store.process("100001", "device_001", request("round.end")),
    )
    store.register_client("100001", "device_001")
    fails(
        "NOT_JOINED",
        lambda: store.process("100001", "device_001", request("round.end")),
    )
    assert store.detail("100001")["round_version"] == 1


def test_timeout_starts_at_first_valid_hand_and_is_fixed_across_restart(store, deck):
    stamp = [1_000_000]
    store.clock = lambda: stamp[0]
    join(store)
    stamp[0] += 500_000
    assert store.expire() == []  # Join alone doesn't start the timer.
    submit(store, deck[:13])
    deadline = stamp[0] + 180_000
    stamp[0] += 120_000
    submit(store, deck[:13])
    assert store.detail("100001")["deadline_at_ms"] == deadline
    reopened = Store(store.path, clock=lambda: stamp[0])
    stamp[0] = deadline
    assert reopened.expire() == ["100001"]
    assert reopened.expire() == []
    assert reopened.detail("100001")["round_version"] == 2
    assert reopened.detail("100001", 1)["close_reason"] == "timeout"


def test_manual_versions_are_forward_only_and_disable_is_persistent(store):
    store.update_tenant("100001", {"round_version": 12, "note": "测试租户"})
    fails("VERSION_REUSED", lambda: store.update_tenant("100001", {"round_version": 1}))
    store.update_tenant("100001", {"round_version": 12})
    assert store.history()["total"] == 1
    store.update_tenant("100001", {"enabled": False})
    assert store.detail("100001")["round_version"] == 13
    fails("TENANT_DISABLED", lambda: store.register_client("100001", "device_001"))
    store.update_tenant("100001", {"enabled": True})
    assert store.detail("100001")["state"] == "waiting"


def test_registration_and_history_survives_reregistration(store, deck):
    for n in range(7):
        store.register_client("100001", f"c{n}")

    join(store, "c0")
    submit(store, deck[:13], "c0")
    fails("ROUND_ACTIVE", lambda: store.delete_client("100001", "c0"))
    store.close_round("100001", 1)
    store.delete_client("100001", "c0")
    store.register_client("100001", "extra")
    old = store.detail("100001", 1)
    assert [d["client_id"] for d in old["devices"]] == [f"c{i}" for i in range(7)]
    assert old["devices"][0]["cards"] is not None


def test_concurrent_retries_and_end_advance_once(store, deck):
    join(store)
    hand = request("hand.submit", payload={"cards": deck[:13]})
    with ThreadPoolExecutor(max_workers=7) as pool:
        replies = list(
            pool.map(lambda _: store.process("100001", "device_001", hand), range(7))
        )
    assert all(reply == replies[0] for reply in replies)
    assert store.detail("100001")["received_count"] == 1
    end = request("round.end")
    with ThreadPoolExecutor(max_workers=7) as pool:
        list(pool.map(lambda _: store.process("100001", "device_001", end), range(7)))
    assert store.detail("100001")["round_version"] == 2


def test_concurrent_seventh_hand_and_end_never_modify_closed_history(store, deck):
    for n in range(7):
        cid = f"c{n}"
        join(store, cid)
        if n < 6:
            submit(store, deck[n * 13 : (n + 1) * 13], cid)

    def attempt(fn):
        try:
            return fn()
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [
            pool.submit(attempt, lambda: submit(store, deck[78:91], "c6")),
            pool.submit(
                attempt,
                lambda: store.process("100001", "c0", request("round.end", "c0")),
            ),
        ]
        [job.result() for job in jobs]
    old = store.detail("100001", 1)
    assert old["state"] == "closed" and old["received_count"] in (6, 7)
    assert bool(old["result"]) == (old["received_count"] == 7)
    assert store.detail("100001")["received_count"] == 0


def test_version_exhaustion_closes_without_wrapping_or_blocking_other_tenants(
    store, deck
):
    from app.domain import MAX_VERSION

    stamp = [1_000_000]
    store.clock = lambda: stamp[0]
    store.create_tenant("999999", round_version=MAX_VERSION)
    join(store, tenant="999999", version=MAX_VERSION)
    submit(store, deck[:13], tenant="999999", version=MAX_VERSION)
    join(store)
    submit(store, deck[:13])
    stamp[0] += 180_000
    assert set(store.expire()) == {"100001", "999999"}
    assert store.detail("100001")["round_version"] == 2
    last = store.detail("999999")
    assert last["state"] == "closed" and last["enabled"] is False
    assert last["round_version"] == MAX_VERSION
    fails("VERSION_EXHAUSTED", lambda: store.update_tenant("999999", {"enabled": True}))
    assert store.expire() == []


def test_daily_summary_uses_shanghai_midnight(store, deck):
    from datetime import datetime

    stamp = [
        int(datetime.fromisoformat("2026-09-08T15:59:00+00:00").timestamp() * 1000)
    ]
    store.clock = lambda: stamp[0]
    for n in range(7):
        cid = f"c{n}"
        join(store, cid)
        submit(store, deck[n * 13 : (n + 1) * 13], cid)
    assert store.summary()["completed_today"] == 1
    stamp[0] += 120_000  # 00:01 in Shanghai; still the same date in UTC.
    assert store.summary()["completed_today"] == 0


def test_offline_registration_does_not_reserve_a_hand_slot(store, deck):
    store.register_client("100001", "01")
    for index in range(7):
        cid = f"{index + 2:02}"
        join(store, cid)
        submit(store, deck[index * 13:(index + 1) * 13], cid)
    detail = store.detail("100001")
    assert detail["state"] == "ready"
    assert detail["received_count"] == 7
    assert detail["missing_client_ids"] == []
    assert sum(c["count"] for c in detail["result"]) == 13
    assert {d["client_id"] for d in detail["devices"] if d["cards"]} == {f"{i:02}" for i in range(2, 9)}
    fails("ROUND_READY", lambda: join(store, "09"))


def test_eighth_joined_client_cannot_add_an_eighth_hand(store, deck):
    for i in range(8):
        join(store, f"arbitrary_{i}")
    for i in range(7):
        submit(store, deck[i * 13:(i + 1) * 13], f"arbitrary_{i}")
    fails("ROUND_READY", lambda: submit(store, deck[91:], "arbitrary_7"))
    assert store.detail("100001")["received_count"] == 7


def test_twenty_id_limit_preserves_existing_reconnects(store):
    for i in range(1, 21):
        store.register_client("100001", f"{i:02}")
    store.register_client("100001", "07")
    fails("CLIENT_LIMIT", lambda: store.register_client("100001", "21"))
    assert len(store.detail("100001")["devices"]) == 20


def test_any_seven_can_swap_07_to_12_and_back_after_skipped_rounds(store, deck):
    for i in range(1, 21):
        store.register_client("100001", f"{i:02}")
    previous = {}
    groups = [list(range(1,8)), [1,2,3,4,5,6,12], [1,2,3,4,5,6,12], list(range(1,8))]
    for version, group in enumerate(groups,1):
        for index, number in enumerate(group):
            cid=f"{number:02}"
            join(store,cid,version=version,previous=previous.get(cid))
            previous[cid]=version
            submit(store,deck[index*13:(index+1)*13],cid,version=version)
        detail=store.detail("100001")
        assert detail["state"] == "ready"
        assert sum(c["count"] for c in detail["result"]) == 13
        assert {d["client_id"] for d in detail["devices"] if d["cards"]} == {f"{i:02}" for i in group}
        store.process("100001","01",request("round.end","01",version=version))
