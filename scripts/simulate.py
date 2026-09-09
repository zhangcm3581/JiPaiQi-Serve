#!/usr/bin/env python3
"""Send seven real WebSocket hands to an explicitly chosen test tenant."""

import argparse
import asyncio
import json
import random
import time
import uuid
from collections import Counter
from contextlib import AsyncExitStack, suppress
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import websockets

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["s", "h", "c", "d"]


def http(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=10) as response:
        return json.load(response)


class Device:
    def __init__(self, socket, tenant, cid):
        self.socket, self.tenant, self.cid = socket, tenant, cid
        self.pending = {}
        self.state = None
        self.initialized = asyncio.Event()
        self.result = None
        self.result_event = asyncio.Event()

    async def read(self):
        try:
            async for raw in self.socket:
                value = json.loads(raw)
                if value.get("reply_to") in self.pending:
                    future = self.pending[value["reply_to"]]
                    if not future.done():
                        future.set_result(value)
                if value["type"] == "round.state":
                    self.state = value
                    self.initialized.set()
                if value["type"] == "round.result":
                    self.result = value
                    self.result_event.set()
                if value["type"] == "error" and not value.get("reply_to"):
                    raise RuntimeError(value["payload"])
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("WebSocket disconnected"))

    async def call(self, kind, version=None, payload=None):
        rid = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        message = {
            "protocol_version": 1,
            "type": kind,
            "tenant_id": self.tenant,
            "client_id": self.cid,
            "request_id": rid,
            "round_version": version,
            "payload": payload or {},
        }
        try:
            await self.socket.send(json.dumps(message))
            reply = await asyncio.wait_for(future, 10)
            if reply["type"] == "error":
                raise RuntimeError(reply["payload"])
            return reply
        finally:
            self.pending.pop(rid, None)

    async def heartbeat(self):
        while True:
            await asyncio.sleep(15)
            await self.call("ping")


async def simulate(args):
    base = args.url.rstrip("/")
    if not args.tenant.isascii() or not args.tenant.isdigit():
        raise ValueError("--tenant 必须是数字字符串")
    try:
        http(
            base,
            "/api/tenants",
            {"tenant_id": args.tenant, "note": "联调测试 · 十三水双副牌"},
        )
    except HTTPError as error:
        if error.code != 409 or json.load(error).get("code") != "TENANT_EXISTS":
            raise
    current = http(base, f"/api/tenants/{args.tenant}/rounds/current")
    if current["state"] != "waiting" or not current["enabled"]:
        raise RuntimeError("测试租户须处于已启用、等待新局状态；脚本不会关闭现有轮次。")
    if any(not d["client_id"].startswith("sim_device_") for d in current["devices"]):
        raise RuntimeError("租户已有其他客户端，请指定独立测试租户。")
    wsbase = ("wss://" if base.startswith("https://") else "ws://") + base.split(
        "://", 1
    )[1]
    tasks = []
    async with AsyncExitStack() as stack:
        try:
            devices = []
            for n in range(7):
                cid = f"sim_device_{n + 1:03}"
                ws = await stack.enter_async_context(
                    websockets.connect(
                        f"{wsbase}/ws/client?tenant_id={args.tenant}&client_id={cid}",
                        max_size=65536,
                    )
                )
                device = Device(ws, args.tenant, cid)
                tasks += [
                    asyncio.create_task(device.read()),
                    asyncio.create_task(device.heartbeat()),
                ]
                await asyncio.wait_for(device.initialized.wait(), 10)
                devices.append(device)
            for run in range(args.rounds):
                state = await devices[0].call("round.get")
                version = state["round_version"]
                deck = [
                    {"suit": s, "rank": r}
                    for _ in range(2)
                    for s in SUITS
                    for r in RANKS
                ]
                random.Random(args.seed + run).shuffle(deck)
                for device in devices:
                    state = await device.call("round.get")
                    previous = state["payload"]["bound_round_version"]
                    device.result_event.clear()
                    await device.call(
                        "round.join",
                        version,
                        {
                            "start_event_id": uuid.uuid4().hex,
                            "sync_basis": "initial_start"
                            if previous is None
                            else "end_then_start",
                            "previous_round_version": previous,
                        },
                    )
                for n, device in enumerate(devices):
                    if n == 6 and args.delay_last:
                        print(
                            f"租户 {args.tenant} / 版本 {version}: 已收到 6/7，{args.delay_last:g} 秒后提交最后一份",
                            flush=True,
                        )
                        await asyncio.sleep(args.delay_last)
                    began = time.perf_counter()
                    await device.call(
                        "hand.submit", version, {"cards": deck[n * 13 : (n + 1) * 13]}
                    )
                await asyncio.wait_for(
                    asyncio.gather(*(d.result_event.wait() for d in devices)), 5
                )
                latency = (time.perf_counter() - began) * 1000
                expected = Counter((c["suit"], c["rank"]) for c in deck[91:])
                for device in devices:
                    assert device.result["round_version"] == version
                    assert {
                        (c["suit"], c["rank"]): c["count"]
                        for c in device.result["payload"]["cards"]
                    } == expected
                print(
                    f"租户 {args.tenant} / 版本 {version}: 7份、91张校验通过，剩余13张；全部客户端收到结果 {latency:.1f} ms",
                    flush=True,
                )
                if args.hold_seconds:
                    print(
                        f"保持结果 {args.hold_seconds:g} 秒，可在后台查看。", flush=True
                    )
                    await asyncio.sleep(args.hold_seconds)
                if run < args.rounds - 1 or args.end:
                    reply = await devices[0].call("round.end", version)
                    print(
                        f"已结束，下一版本 {reply['payload']['next_round_version']}",
                        flush=True,
                    )
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8768")
    parser.add_argument("--tenant", required=True, help="独立测试租户 ID；不存在则创建")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--delay-last", type=float, default=0, help="第7份延迟秒数")
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=0,
        help="结果保持时间；不要超过本轮截止时间",
    )
    parser.add_argument(
        "--end", action="store_true", help="最后一轮主动结束；否则保留结果直到超时"
    )
    args = parser.parse_args()
    if (
        args.rounds < 1
        or min(args.delay_last, args.hold_seconds) < 0
        or args.delay_last + args.hold_seconds >= 170
    ):
        parser.error("rounds至少1；等待时长非负且合计小于170秒")
    asyncio.run(simulate(args))
