"""End-to-end acceptance scenarios against isolated, real Uvicorn processes."""

import copy
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from wire_support import (
    RealServer,
    error,
    expected_result,
    ok,
    result_counts,
    shuffled_deck,
)


def scenario(title, category, steps, expected):
    return pytest.mark.scenario(
        title=title, category=category, steps=steps, expected=expected
    )


@pytest.fixture
def server(tmp_path, request, record_property):
    marker = request.node.get_closest_marker("scenario")
    for key, value in marker.kwargs.items():
        record_property(key, value)
    service = RealServer(tmp_path)
    try:
        yield service
    finally:
        service.cleanup()
        record_property("evidence", json.dumps(service.evidence, ensure_ascii=False))
        record_property("wire_sent", sum(len(c.sent) for c in service.clients))
        record_property("wire_received", sum(len(c.messages) for c in service.clients))
        # Persist isolated wire evidence outside the application's database.
        destination = request.config.rootpath / "reports/traces"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / (request.node.name + ".json")).write_text(
            json.dumps(
                [
                    {
                        "tenant": c.tenant,
                        "client": c.cid,
                        "sent": c.sent,
                        "received": c.messages,
                    }
                    for c in service.clients
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        log = service.log_path.read_text()
        record_property("server_errors", log.count("ERROR:"))
        (destination / (request.node.name + ".log")).write_text(log)
        service.integrity()
        assert "ERROR:" not in log, log


@scenario(
    "七端乱序上报、结果及后台同步",
    "收发与计算",
    "7条连接乱序发送各13张；订阅管理端；核对七端结果及91张输入。",
    "第7份前无结果；收齐后七端和后台结果一致，版本不提前推进，耗时小于2秒。",
)
def test_wire_seven_out_of_order(server):
    server.tenant()
    admin = server.admin()
    peers = server.seven()
    deck = shuffled_deck()
    order = [5, 1, 6, 2, 0, 4, 3]
    for position, n in enumerate(order):
        began = time.perf_counter()
        ok(peers[n].submit(deck[n * 13 : n * 13 + 13]))
        state = server.detail()
        assert state["received_count"] == position + 1
        assert (state["result"] is None) == (position < 6)
    for peer in peers:
        peer.result(deck)
    latency = (time.perf_counter() - began) * 1000
    assert latency < 2000
    state = admin.wait("admin.round", lambda v: v["payload"]["state"] == "ready")[
        "payload"
    ]
    assert state["round_version"] == 1
    assert result_counts(state["result"]) == expected_result(deck)
    for n, peer in enumerate(peers):
        device = next(c for c in state["devices"] if c["client_id"] == peer.cid)
        assert sorted(map(json.dumps, device["cards"])) == sorted(
            map(json.dumps, deck[n * 13 : n * 13 + 13])
        )
        snapshot = peer.call("round.get", None)["payload"]
        assert "cards" not in snapshot and "devices" not in snapshot
    server.note(
        result_latency_ms=round(latency, 2),
        received_hands=7,
        received_cards=91,
        remaining=13,
        version=1,
    )


@scenario(
    "第七端启动较慢",
    "漏报与延迟",
    "先连接6端并提交；等待1秒；第7端随后连接、绑定并提交。",
    "等待期间保持6/7且没有结果，迟到的同版本数据仍能完成计算。",
)
def test_wire_late_seventh(server):
    server.tenant()
    deck = shuffled_deck(3)
    peers = []
    for n in range(6):
        peer = server.client(cid=f"d{n}")
        peers.append(peer)
        ok(peer.join())
        ok(peer.submit(deck[n * 13 : n * 13 + 13]))
    deadline = server.detail()["deadline_at_ms"]
    time.sleep(1)
    assert all(not any(v["type"] == "round.result" for v in p.messages) for p in peers)
    last = server.client(cid="d6")
    ok(last.join())
    ok(last.submit(deck[78:91]))
    for peer in peers + [last]:
        peer.result(deck)
    assert server.detail()["deadline_at_ms"] == deadline
    server.note(before=6, after=7, deadline_extended=False)


@scenario(
    "漏开一端，六份结束后迟到旧牌",
    "漏报与延迟",
    "只连接6端，上报6份后报告结束；第7端再提交旧版本手牌。",
    "不推算确定13张；结束只推进一次；旧牌被拒绝，新版本保持空白。",
)
def test_wire_missing_client_then_end(server):
    server.tenant()
    deck = shuffled_deck()
    peers = []
    for n in range(6):
        peer = server.client(cid=f"d{n}")
        peers.append(peer)
        ok(peer.join())
        ok(peer.submit(deck[n * 13 : n * 13 + 13]))
    before = server.detail()
    assert before["unregistered_count"] == 1 and before["result"] is None
    assert ok(peers[0].call("round.end"))["payload"]["next_round_version"] == 2
    late = server.client(cid="d6")
    error(late.submit(deck[78:91]), "ROUND_CLOSED")
    assert server.detail()["received_count"] == 0
    old = server.detail(version=1)
    assert old["received_count"] == 6 and old["result"] is None
    assert old["close_reason"] == "game_end"
    server.note(
        history_count=6, result=None, current_version=2, late_error="ROUND_CLOSED"
    )


@scenario(
    "无人报告结束时自动超时",
    "版本与超时",
    "超时设为测试允许的10秒；仅join时不启动计时；一份有效手牌后等待定时器推送。",
    "10秒从第一份有效手牌开始；无需HTTP轮询触发；只推进一次且不生成结果。",
)
def test_wire_timeout_push(server):
    server.tenant(timeout_seconds=10)
    peer = server.client()
    ok(peer.join())
    assert server.detail()["deadline_at_ms"] is None
    ok(peer.submit(shuffled_deck()[:13]))
    state = server.detail()
    assert state["deadline_at_ms"] - state["first_received_at_ms"] == 10000
    next_state = peer.wait("round.state", lambda v: v["round_version"] == 2, timeout=12)
    assert next_state["payload"]["state"] == "waiting"
    old = server.detail(version=1)
    assert old["close_reason"] == "timeout" and old["received_count"] == 1
    error(peer.call("round.end"), "ROUND_CLOSED")
    assert server.detail()["round_version"] == 2
    server.note(
        test_timeout_seconds=10,
        production_default_seconds=180,
        automatic_push_version=2,
    )


@scenario(
    "重复请求、换序重传与内容冲突",
    "去重与校验",
    "同ID重发、换新ID重排相同13张、同ID修改内容、同端提交不同手牌。",
    "合法重复均不重复扣牌；冲突分别拒绝；份数及截止时间不变。",
)
def test_wire_duplicate_and_conflict(server):
    server.tenant()
    peer = server.client()
    ok(peer.join())
    deck = shuffled_deck()
    message = peer.message("hand.submit", payload={"cards": deck[:13]})
    first = ok(peer.call_message(message))
    before = server.detail()
    assert peer.call_message(message) == first
    assert ok(peer.submit(list(reversed(deck[:13]))))["payload"]["duplicate"]
    changed = copy.deepcopy(message)
    changed["payload"]["cards"] = deck[13:26]
    error(peer.call_message(changed), "REQUEST_CONFLICT")
    error(peer.submit(deck[13:26]), "HAND_CONFLICT")
    assert server.detail() == before
    server.note(
        stored_hands=1,
        deadline_unchanged=True,
        errors=["REQUEST_CONFLICT", "HAND_CONFLICT"],
    )


@scenario(
    "双副牌同手牌可重复，第三副拒绝",
    "去重与校验",
    "不同设备上传完全相同13张黑桃；第三设备再上传同一份，然后改传红桃。",
    "前两份按设备分别记入；第三份超出两副牌容量时整笔回滚，可改正重试。",
)
def test_wire_same_hand_different_devices(server):
    server.tenant()
    peers = server.seven()
    cards = [
        {"suit": "s", "rank": r} for r in ["A", *map(str, range(2, 11)), "J", "Q", "K"]
    ]
    ok(peers[0].submit(cards))
    ok(peers[1].submit(cards))
    before = server.detail()
    error(peers[2].submit(cards), "DECK_OVERFLOW")
    assert server.detail() == before
    ok(peers[2].submit([{**c, "suit": "h"} for c in cards]))
    assert server.detail()["received_count"] == 3
    server.note(identical_hands_accepted=2, rejected_third_copy=True, corrected_count=3)


@scenario(
    "畸形消息与手牌严格校验",
    "去重与校验",
    "依次发送非JSON、数组、空值、12/14张、非法点数花色、缺花色、额外字段、布尔协议版本和超大版本。",
    "每次返回可识别错误且连接可继续ping；错误数据零入库，计时尚未开始。",
)
def test_wire_invalid_messages(server):
    server.tenant()
    peer = server.client()
    ok(peer.join())
    deck = shuffled_deck()
    invalid = [
        deck[:12],
        deck[:14],
        [{"rank": "A", "suit": "x"}] * 13,
        [{"rank": "1", "suit": "s"}] * 13,
        [{"rank": "A"}] * 13,
        [{"rank": "A", "suit": "s", "extra": 1}] * 13,
    ]
    for cards in invalid:
        error(peer.submit(cards), "INVALID_MESSAGE")
    for raw in ["{", "[]", "null"]:
        mark = len(peer.messages)
        peer.socket.send(raw)
        error(peer.wait("error", after=mark), "INVALID_MESSAGE")
    bad = peer.message("ping", None)
    bad["protocol_version"] = True
    error(peer.call_message(bad), "INVALID_MESSAGE")
    error(peer.call("round.get", 10**30), "INVALID_MESSAGE")
    assert peer.call("ping", None)["type"] == "pong"
    response = server.api("GET", f"/api/tenants/900001/rounds/{10**30}", status=422)
    assert response["code"] == "INVALID_MESSAGE"
    state = server.detail()
    assert state["received_count"] == 0 and state["deadline_at_ms"] is None
    server.note(rejected_cases=12, stored_hands=0, connection_alive=True)


@scenario(
    "多租户同版本同设备ID不串牌",
    "租户隔离",
    "两个租户使用相同7个设备ID、版本1及相同请求ID，交错上报不同牌组；后台切换订阅。",
    "各自收到自己的13张结果，后台仅推送订阅租户，身份伪造被拒绝。",
)
def test_wire_tenant_isolation(server):
    a, b = server.tenant("001001"), server.tenant("100001")
    admin = server.admin(a)
    pa, pb = server.seven(a), server.seven(b)
    da, db = shuffled_deck(1), shuffled_deck(2)
    for n in range(7):
        ok(pa[n].submit(da[n * 13 : n * 13 + 13], rid="shared-request"))
        ok(pb[n].submit(db[n * 13 : n * 13 + 13], rid="shared-request"))
    for p in pa:
        p.result(da)
    for p in pb:
        p.result(db)
    assert all(v["tenant_id"] == a for v in pa[0].messages)
    assert all(
        v["tenant_id"] == a for v in admin.messages if v["type"] == "admin.round"
    )
    bad = pa[0].message("round.get", None)
    bad["tenant_id"] = b
    error(pa[0].call_message(bad), "IDENTITY_MISMATCH")
    mark = len(admin.messages)
    admin.send({"type": "subscribe", "tenant_id": b})
    snapshot = admin.wait("admin.round", after=mark)
    assert snapshot["tenant_id"] == b
    assert result_counts(snapshot["payload"]["result"]) == expected_result(db)
    server.note(tenants=[a, b], same_version=1, shared_ids_isolated=True)


@scenario(
    "发送后断线，重连补确认和已锁定结果",
    "断线与恢复",
    "首端发送后不等待ack即断线；重连原ID重传；收齐后再次重连。",
    "确认可重放但只入库一份；登记仍为7端；重连自动补推正确已锁定结果。",
)
def test_wire_disconnect_and_replay(server):
    server.tenant()
    peers = server.seven()
    deck = shuffled_deck()
    message = peers[0].message("hand.submit", payload={"cards": deck[:13]})
    peers[0].send(message)
    peers[0].close()
    peers[0] = server.client()
    original = ok(peers[0].call_message(message))
    assert peers[0].call_message(message) == original
    assert server.detail()["received_count"] == 1
    for n in range(1, 7):
        ok(peers[n].submit(deck[n * 13 : n * 13 + 13]))
    peers[0].close()
    reconnect = server.client()
    reconnect.result(deck)
    assert server.detail()["unregistered_count"] == 0
    server.note(registered_clients=7, first_hand_stored_once=True, result_replayed=True)


@pytest.mark.parametrize("crash", [False, True], ids=["graceful", "killed"])
@scenario(
    "六份手牌后服务重启恢复",
    "断线与恢复",
    "收6份后正常停止或强杀独立服务；同一SQLite重启；原设备重连，第7份补齐。",
    "版本、截止时间及原ack保留；结果仍等于牌组余集；SQLite完整性通过。",
)
def test_wire_restart_after_six(server, crash):
    server.tenant()
    peers = server.seven()
    deck = shuffled_deck(17)
    first = peers[0].message("hand.submit", payload={"cards": deck[:13]})
    ack = ok(peers[0].call_message(first))
    for n in range(1, 6):
        ok(peers[n].submit(deck[n * 13 : n * 13 + 13]))
    before = server.detail()
    server.restart(crash=crash)
    peers = [server.client(cid=f"device_{n + 1:03}") for n in range(7)]
    assert server.detail()["deadline_at_ms"] == before["deadline_at_ms"]
    assert peers[0].call_message(first) == ack
    ok(peers[6].submit(deck[78:91]))
    for p in peers:
        p.result(deck)
    server.note(
        restart="SIGKILL" if crash else "SIGTERM",
        preserved_count=6,
        deadline_preserved=True,
    )


@scenario(
    "七端同时结束只推进一次",
    "并发与时序",
    "7端绑定后同时发送各自的结束消息，再重放其中成功的结束请求。",
    "恰好1次结束成功，其余返回已关闭；重放不推进新轮次。",
)
def test_wire_concurrent_end(server):
    server.tenant()
    peers = server.seven()
    messages = [p.message("round.end") for p in peers]
    with ThreadPoolExecutor(max_workers=7) as pool:
        replies = list(
            pool.map(lambda item: item[0].call_message(item[1]), zip(peers, messages))
        )
    assert sum(r["type"] == "ack" for r in replies) == 1
    for p, m, reply in zip(peers, messages, replies):
        if reply["type"] == "ack":
            assert p.call_message(m) == reply
        else:
            error(reply, "ROUND_CLOSED")
    assert server.detail()["round_version"] == 2
    server.note(successful_ends=1, rejected_ends=6, current_version=2)


@scenario(
    "第七份与结束消息竞争",
    "并发与时序",
    "重复12轮：收6份后让第7份与结束同时到达；核对关闭快照及推送顺序。",
    "只允许6份无结果或7份有正确结果；新版本零手牌，不得将旧结果推送到新版本之后。",
)
def test_wire_seventh_races_end(server):
    server.tenant()
    peers = server.seven()
    completed = 0
    for version in range(1, 13):
        if version > 1:
            for p in peers:
                ok(p.join(version, version - 1))
        deck = shuffled_deck(version)
        for n in range(6):
            ok(peers[n].submit(deck[n * 13 : n * 13 + 13], version))
        with ThreadPoolExecutor(max_workers=2) as pool:
            last = pool.submit(peers[6].submit, deck[78:91], version)
            end = pool.submit(peers[0].call, "round.end", version)
            last_reply, end_reply = last.result(), end.result()
        ok(end_reply)
        old = server.detail(version=version)
        assert old["state"] == "closed" and old["received_count"] in (6, 7)
        if old["received_count"] == 7:
            ok(last_reply)
            assert result_counts(old["result"]) == expected_result(deck)
            completed += 1
        else:
            error(last_reply, "ROUND_CLOSED")
            assert old["result"] is None
        assert server.detail()["received_count"] == 0
        for p in peers:
            p.wait("round.state", lambda v: v["round_version"] == version + 1)
            current_seen = False
            for m in p.messages:
                if m["type"] == "round.state" and m["round_version"] == version + 1:
                    current_seen = True
                assert not (
                    current_seen
                    and m["type"] == "round.result"
                    and m["round_version"] == version
                )
    server.note(
        race_rounds=12,
        complete_before_close=completed,
        incomplete_before_close=12 - completed,
        next_version=13,
    )


@scenario(
    "服务端版本变更不能给旧牌重新编号",
    "版本与超时",
    "旧版结束后直接给手牌换版本提交；重复新局事件；后台跳至版本8后客户端补报。",
    "未绑定、复用新局事件、伪造上次绑定均拒绝；旧ID可凭新事件跨轮重新加入。",
)
def test_wire_old_cards_and_skipped_round(server):
    server.tenant()
    p = server.client()
    ok(p.join(event="original-start"))
    ok(p.submit(shuffled_deck()[:13]))
    ok(p.call("round.end"))
    error(p.submit(shuffled_deck()[:13], 2), "NOT_JOINED")
    error(p.join(2, 1, event="original-start"), "START_EVENT_CONFLICT")
    ok(p.join(2, 1))
    server.api("PATCH", "/api/tenants/900001", {"round_version": 8})
    error(p.join(8, 1), "SYNC_REQUIRED")
    error(p.submit(shuffled_deck()[:13], 8), "NOT_JOINED")
    ok(p.join(8, 2))
    assert server.detail()["received_count"] == 0
    server.note(
        current_version=8,
        errors=["NOT_JOINED", "START_EVENT_CONFLICT", "SYNC_REQUIRED"],
    )


@scenario(
    "连接替换、第八端与未绑定结束",
    "设备与生命周期",
    "同设备再次连接；尝试第8设备；未绑定端发送手牌和结束；原连接退场。",
    "同ID只占一席，新连接可用；第8端可登记；未绑定结束不推进版本。",
)
def test_wire_device_membership(server):
    server.tenant()
    first = server.client()
    replacement = server.client()
    assert replacement.call("ping", None)["type"] == "pong"
    error(replacement.call("round.end"), "NOT_JOINED")
    error(replacement.submit(shuffled_deck()[:13]), "NOT_JOINED")
    for n in range(2, 8):
        server.client(cid=f"device_{n:03}")
    extra = server.client(cid="device_008")
    assert extra.call("ping", None)["type"] == "pong"
    assert len(server.detail()["devices"]) == 8
    assert server.detail()["round_version"] == 1
    first.reader.join(timeout=2)
    assert first.closed
    server.note(
        registered=8,
        replacement_code=first.socket.close_code,
        extra_accepted=True,
    )


@scenario(
    "暂停租户、改配置与历史只读",
    "设备与生命周期",
    "收一份后修改备注和超时配置，再停用租户、继续上报、重新启用并开始下一局。",
    "当前截止时间不被改动；停用关闭本轮且拒绝写入；启用不复用旧版本和历史手牌。",
)
def test_wire_admin_changes(server):
    server.tenant()
    p = server.client()
    ok(p.join())
    deck = shuffled_deck()
    ok(p.submit(deck[:13]))
    deadline = server.detail()["deadline_at_ms"]
    server.api(
        "PATCH", "/api/tenants/900001", {"note": "修改备注", "timeout_seconds": 30}
    )
    assert server.detail()["deadline_at_ms"] == deadline
    server.api("PATCH", "/api/tenants/900001", {"enabled": False})
    error(p.submit(deck[:13], 2), "TENANT_DISABLED")
    old = server.detail(version=1)
    server.api("PATCH", "/api/tenants/900001", {"enabled": True})
    ok(p.join(2, 1))
    ok(p.submit(deck[13:26], 2))
    current = server.detail()
    assert current["deadline_at_ms"] - current["first_received_at_ms"] == 30000
    assert server.detail(version=1) == old | {"enabled": True}
    server.note(
        old_deadline_preserved=True,
        new_timeout_seconds=30,
        history_hands_preserved=True,
    )


@scenario(
    "连续随机发牌20局及历史守恒",
    "连续运行",
    "固定随机种子生成20副洗牌；每局随机上报顺序并重试一份；七端校验结果后结束。",
    "每局91+13=104，任何花色点数均不超过2；20份历史不变，下一版为21。",
)
def test_wire_twenty_rounds(server):
    server.tenant()
    peers = server.seven()
    histories = []
    latencies = []
    for version in range(1, 21):
        if version > 1:
            for p in peers:
                ok(p.join(version, version - 1))
        deck = shuffled_deck(100 + version)
        order = list(range(7))
        random.Random(version).shuffle(order)
        for n in order:
            began = time.perf_counter()
            ok(peers[n].submit(deck[n * 13 : n * 13 + 13], version))
        for p in peers:
            p.result(deck, version)
        latencies.append((time.perf_counter() - began) * 1000)
        assert ok(peers[0].submit(deck[:13], version))["payload"]["duplicate"]
        ok(peers[0].call("round.end", version))
        histories.append(server.detail(version=version))
    for version, snapshot in enumerate(histories, 1):
        assert server.detail(version=version) == snapshot
    assert server.detail()["round_version"] == 21
    assert max(latencies) < 2000
    server.note(
        rounds=20,
        uploads=140,
        received_result_checks=140,
        max_result_latency_ms=round(max(latencies), 2),
        min_result_latency_ms=round(min(latencies), 2),
    )


@scenario(
    "八租户56端同时上报",
    "并发与时序",
    "8个独立租户各7条连接并行提交；每份上报均经过HTTP/WS服务和SQLite事务。",
    "56端均收到本租户正确结果；各租户91张不串牌；最后一份到七端结果小于2秒。",
)
def test_wire_multi_tenant_burst(server):
    groups = []
    for n in range(8):
        tenant = server.tenant(str(800001 + n))
        groups.append((server.seven(tenant), shuffled_deck(n)))

    def complete(group):
        peers, deck = group
        for n, p in enumerate(peers):
            began = time.perf_counter()
            ok(p.submit(deck[n * 13 : n * 13 + 13]))
        for p in peers:
            p.result(deck)
        return (time.perf_counter() - began) * 1000

    with ThreadPoolExecutor(max_workers=8) as pool:
        latencies = list(pool.map(complete, groups))
    assert max(latencies) < 2000
    assert sum(server.detail(str(800001 + n))["received_count"] for n in range(8)) == 56
    server.note(tenants=8, clients=56, max_result_latency_ms=round(max(latencies), 2))


@scenario(
    "同一设备并发重传20次",
    "并发与时序",
    "同一条连接同时重传同request_id的13张数据20次；再查询份数。",
    "20次均收到同一个已持久化ack，只保存1份13张，不重复扣牌。",
)
def test_wire_concurrent_duplicate_upload(server):
    server.tenant()
    peer = server.client()
    ok(peer.join())
    message = peer.message("hand.submit", payload={"cards": shuffled_deck()[:13]})
    with ThreadPoolExecutor(max_workers=20) as pool:
        replies = list(pool.map(lambda _: peer.call_message(message), range(20)))
    assert all(reply == replies[0] for reply in replies)
    ok(replies[0])
    assert server.detail()["received_count"] == 1
    assert peer.call("ping", None)["type"] == "pong"
    server.note(concurrent_retries=20, stored_hands=1, identical_acks=True)


@scenario(
    "45秒空闲回收与15秒应用心跳",
    "断线与恢复",
    "一端不发送任何业务消息，另一端每15秒ping；按真实45秒时间等待。",
    "空闲端断开且在线数减少；有应用心跳的连接继续可用，设备登记不受影响。",
)
def test_wire_heartbeat_and_idle_timeout(server):
    server.tenant()
    idle = server.client(cid="idle")
    active = server.client(cid="active")
    finished = threading.Event()
    replies, errors = [], []

    def heartbeat():
        while not finished.wait(15):
            try:
                replies.append(active.call("ping", None))
            except Exception as exc:
                errors.append(str(exc))
                return

    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    began = time.monotonic()
    try:
        idle.reader.join(timeout=48)
        assert idle.closed
        assert 43 <= time.monotonic() - began <= 49
        assert not errors and len(replies) >= 2
        assert all(r["type"] == "pong" for r in replies)
        assert active.call("ping", None)["type"] == "pong"
        detail = server.detail()
        assert sum(c["online"] for c in detail["devices"]) == 1
        assert detail["received_count"] == 0
        server.note(
            idle_seconds=round(time.monotonic() - began, 2),
            heartbeat_replies=len(replies),
            online_clients=1,
        )
    finally:
        finished.set()
        worker.join(timeout=6)


@pytest.mark.parametrize("admin", [False, True], ids=["client", "admin"])
@scenario(
    "二进制消息关闭且允许重新连接",
    "去重与校验",
    "客户端或管理端向JSON文本接口发送二进制帧；随后新建合法连接。",
    "错误连接按1003关闭，服务保持正常，新连接仍能查询和收发。",
)
def test_wire_binary_frame(server, admin):
    server.tenant()
    peer = server.admin() if admin else server.client()
    peer.socket.send(b"not-json-text")
    peer.reader.join(timeout=3)
    assert peer.closed and peer.socket.close_code == 1003
    fresh = server.client(cid="fresh")
    assert fresh.call("ping", None)["type"] == "pong"
    server.note(
        connection="admin" if admin else "client",
        close_code=1003,
        new_connection_alive=True,
    )


@scenario(
    "超大WebSocket帧限制",
    "去重与校验",
    "发送大于64KiB的消息到真实Uvicorn协议栈，再用新连接查询。",
    "超限连接按1009关闭；数据库不变；服务继续响应。",
)
def test_wire_oversized_frame(server):
    server.tenant()
    peer = server.client()
    peer.socket.send("x" * 65537)
    peer.reader.join(timeout=3)
    assert peer.closed and peer.socket.close_code == 1009
    fresh = server.client(cid="fresh")
    assert fresh.call("ping", None)["type"] == "pong"
    assert server.detail()["received_count"] == 0
    server.note(bytes_sent=65537, close_code=1009, stored_hands=0)


@scenario("离线01不占本轮名额", "设备与生命周期", "01连接后离线，02到08上报。", "任意七个不同客户端收齐并收到13张结果。")
def test_wire_offline_identity_does_not_block_seven_other_clients(server):
    server.tenant()
    old = server.client(cid="01")
    old.socket.close()
    peers = [server.client(cid=f"{i:02}") for i in range(2, 9)]
    deck = shuffled_deck()
    for i, peer in enumerate(peers):
        ok(peer.join())
        ok(peer.submit(deck[i*13:(i+1)*13]))
    for peer in peers:
        assert peer.wait("round.result")["payload"]["remaining_count"] == 13
    assert server.detail()["missing_client_ids"] == []


@scenario("单次提交与定向结果", "一次上报", "20个ID连接，任意7个直接提交；07换12再换回；重复提交与旧轮次重试。", "无round.join；每轮13张准确结果只发给提交者；四轮正常推进。")
def test_wire_atomic_roster_and_targeted_results(server):
    server.tenant()
    peers = {i: server.client(cid=f"emu_{i:02}") for i in range(1,21)}
    deck = shuffled_deck()
    groups = [list(range(1,8)), [1,2,3,4,5,6,12], [1,2,3,4,5,6,12], list(range(1,8))]
    for version, group in enumerate(groups,1):
        requests=[]
        for slot,number in enumerate(group):
            p=peers[number]
            m=p.message("hand.submit",version,{"start_event_id":f"game_{version}_{number}","cards":deck[slot*13:(slot+1)*13]})
            requests.append(m)
            ok(p.call_message(m))
        for number in group:
            result=peers[number].wait("round.result",lambda m:m["round_version"]==version)
            assert result_counts(result["payload"]["cards"])==expected_result(deck)
            assert result["payload"]["client_id"]==peers[number].cid
            assert result["payload"]["start_event_id"]==f"game_{version}_{number}"
        observer=peers[20]
        assert observer.call("round.get",version)["type"]=="round.state"
        assert not any(m["type"]=="round.result" for m in observer.messages)
        ok(peers[group[0]].call("round.end",version))
        # Even after the round closes, a retry of the original accepted hand returns its ack.
        ok(peers[group[0]].call_message(requests[0]))
        for number in group[1:]:
            error(peers[number].call("round.end",version),"ROUND_CLOSED")
        assert server.detail()["round_version"]==version+1
    assert not any(m["type"]=="round.join" for p in peers.values() for m in p.sent)
    server.note(rounds=4,registered=20,participant_count=7,all_results_exact=True)


@scenario("断线丢确认后的单次提交重试", "一次上报", "服务器已保存后断开连接，同ID重连重发原请求。", "返回原确认且只计1份；拒绝内容修改和旧轮改标。")
def test_wire_atomic_reconnect_idempotency(server):
    server.tenant();p=server.client(cid="reconnect_A");deck=shuffled_deck()
    m=p.message("hand.submit",1,{"start_event_id":"once","cards":deck[:13]})
    accepted=p.call_message(m);ok(accepted);p.socket.close()
    replacement=server.client(cid="reconnect_A")
    assert replacement.call_message(m)==accepted
    assert server.detail()["received_count"]==1
    changed=copy.deepcopy(m);changed["payload"]["cards"]=deck[13:26]
    error(replacement.call_message(changed),"REQUEST_CONFLICT")
    ok(replacement.call("round.end",1))
    changed=copy.deepcopy(m);changed["request_id"]="new_req";changed["round_version"]=2
    error(replacement.call_message(changed),"START_EVENT_CONFLICT")
    assert server.detail()["received_count"]==0
