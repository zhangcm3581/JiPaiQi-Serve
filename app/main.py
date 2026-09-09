"""HTTP administration, native-client WebSockets, and admin live snapshots."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .domain import DomainError, Store, identifier, now_ms, require
from .protocol import CloseRequest, TenantCreate, TenantUpdate, parse_message

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("jpq")


def wire(kind, tenant_id=None, version=None, payload=None, reply_to=None):
    value = {
        "protocol_version": 1,
        "type": kind,
        "tenant_id": tenant_id,
        "round_version": version,
        "server_time_ms": now_ms(),
        "payload": payload or {},
    }
    if reply_to:
        value["reply_to"] = reply_to
    return value


def same_origin(headers):
    origin = headers.get("origin")
    if not origin:
        return True
    try:
        return urlsplit(origin).netloc == headers.get("host") and urlsplit(
            origin
        ).scheme in ("http", "https")
    except ValueError:
        return False


async def receive_text(ws):
    message = await asyncio.wait_for(ws.receive(), 45)
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    if message.get("text") is None:
        await ws.close(code=1003, reason="仅接受JSON文本消息")
        raise WebSocketDisconnect(1003)
    return message["text"]


@dataclass(eq=False)
class Peer:
    socket: WebSocket
    tenant_id: str | None = None
    client_id: str | None = None
    subscription: str | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=32))
    closing: bool = False

    def offer(self, data):
        if self.closing:
            return
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            self.closing = True
            asyncio.create_task(self.socket.close(code=1013, reason="请重连同步状态"))

    async def send_loop(self):
        while True:
            value = await self.queue.get()
            try:
                await asyncio.wait_for(self.socket.send_json(value), timeout=5)
            except (RuntimeError, OSError, asyncio.TimeoutError, WebSocketDisconnect):
                self.closing = True
                with suppress(Exception):
                    await self.socket.close(code=1013)
                return


class Engine:
    def __init__(self, path):
        self.store = Store(path)
        self.lock = asyncio.Lock()
        self.clients = {}
        self.admins = set()

    async def db(self, method, *args, **kwargs):
        # SQLite work never blocks the ASGI event loop; callers hold the engine lock.
        return await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)

    async def detail(self, tenant_id, version=None):
        detail = await self.db("detail", tenant_id, version)
        for client in detail["devices"]:
            peer = self.clients.get((tenant_id, client["client_id"]))
            client["online"] = (
                bool(peer and not peer.closing)
                if detail["state"] != "closed"
                else False
            )
        return detail

    async def overview(self):
        tenants = await self.db("tenants")
        for t in tenants:
            d = await self.detail(t["tenant_id"])
            t.update(
                state=d["state"],
                received_count=d["received_count"],
                online_count=sum(c["online"] for c in d["devices"]),
                registered_count=len(d["devices"]),
            )
            t["enabled"] = bool(t["enabled"])
        summary = await self.db("summary")
        summary.update(
            tenant_count=len(tenants),
            enabled_count=sum(t["enabled"] for t in tenants),
            online_count=sum(t["online_count"] for t in tenants),
            registered_count=sum(t["registered_count"] for t in tenants),
        )
        return {"tenants": tenants, "summary": summary}

    def client_snapshot(self, peer, d, reply_to=None):
        fields = [
            "enabled",
            "state",
            "received_count",
            "expected_count",
            "missing_client_ids",
            "unregistered_count",
            "first_received_at_ms",
            "deadline_at_ms",
            "close_reason",
        ]
        state = {k: d[k] for k in fields}
        state["bound_round_version"] = next(
            (
                c["bound_round_version"]
                for c in d["devices"]
                if c["client_id"] == peer.client_id
            ),
            None,
        )
        peer.offer(
            wire("round.state", d["tenant_id"], d["round_version"], state, reply_to)
        )
        if d["result"] is not None and d["state"] == "ready":
            peer.offer(
                wire(
                    "round.result",
                    d["tenant_id"],
                    d["round_version"],
                    {"remaining_count": 13, "cards": d["result"]},
                )
            )

    async def publish(self, affected=None):
        if self.admins:
            snapshot = await self.overview()
            for peer in list(self.admins):
                peer.offer(wire("admin.snapshot", payload=snapshot))
        tenant_ids = set(affected or [])
        tenant_ids.update(
            p.subscription
            for p in self.admins
            if p.subscription and (affected is None or p.subscription in affected)
        )
        for tenant_id in tenant_ids:
            d = await self.detail(tenant_id)
            for peer in list(self.clients.values()):
                if peer.tenant_id == tenant_id:
                    self.client_snapshot(peer, d)
            for peer in list(self.admins):
                if peer.subscription == tenant_id:
                    peer.offer(wire("admin.round", tenant_id, d["round_version"], d))

    async def expire(self):
        changed = await self.db("expire")
        if changed:
            await self.publish(changed)


def create_app(db_path=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = Engine(
            db_path or os.environ.get("JPQ_DB_PATH", "data/jpq.sqlite3")
        )
        engine = app.state.engine
        async with engine.lock:
            await engine.expire()

        async def timer():
            while True:
                await asyncio.sleep(0.5)
                try:
                    async with engine.lock:
                        await engine.expire()
                except Exception:
                    log.exception("检查轮次超时失败")

        task = asyncio.create_task(timer())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        for peer in list(engine.clients.values()) + list(engine.admins):
            with suppress(Exception):
                await peer.socket.close(code=1001)

    app = FastAPI(title="JiPaiQi Serve", version="1.0.0", lifespan=lifespan)
    templates = Jinja2Templates(directory=ROOT / "templates")
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def limit_and_origin(request: Request, call_next):
        if request.method in ("POST", "PATCH", "DELETE", "PUT"):
            if not same_origin(request.headers):
                return JSONResponse(
                    {"code": "ORIGIN_REJECTED", "message": "不允许跨站写入"},
                    status_code=403,
                )
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    return JSONResponse(
                        {"code": "TOO_LARGE", "message": "请求过大"}, status_code=413
                    )
            request._body = bytes(body)
        response = await call_next(request)
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse(
            {"code": error.code, "message": error.message}, status_code=error.status
        )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    @app.get("/live")
    @app.get("/tenants")
    @app.get("/history")
    async def page(request: Request):
        page_name = request.url.path.strip("/") or "overview"
        return templates.TemplateResponse(
            request=request, name=page_name + ".html", context={"page": page_name}
        )

    @app.get("/api/tenants")
    async def tenants(request: Request):
        e = request.app.state.engine
        async with e.lock:
            await e.expire()
            return (await e.overview())["tenants"]

    @app.get("/api/summary")
    async def summary(request: Request):
        e = request.app.state.engine
        async with e.lock:
            return (await e.overview())["summary"]

    @app.post("/api/tenants", status_code=201)
    async def create_tenant(body: TenantCreate, request: Request):
        e = request.app.state.engine
        async with e.lock:
            result = await e.db("create_tenant", **body.model_dump())
            await e.publish([body.tenant_id])
            return result

    @app.patch("/api/tenants/{tenant_id}")
    async def edit_tenant(tenant_id: str, body: TenantUpdate, request: Request):
        e = request.app.state.engine
        changes = body.model_dump(exclude_unset=True)
        require(
            all(v is not None for v in changes.values()),
            "INVALID_MESSAGE",
            "字段不能为null",
            422,
        )
        async with e.lock:
            await e.expire()
            result = await e.db("update_tenant", tenant_id, changes)
            await e.publish([tenant_id])
            return result

    @app.get("/api/tenants/{tenant_id}/rounds/current")
    async def current(tenant_id: str, request: Request):
        e = request.app.state.engine
        async with e.lock:
            await e.expire()
            return await e.detail(tenant_id)

    @app.get("/api/tenants/{tenant_id}/rounds/{version}")
    async def detail(tenant_id: str, version: int, request: Request):
        e = request.app.state.engine
        async with e.lock:
            return await e.detail(tenant_id, version)

    @app.post("/api/tenants/{tenant_id}/close")
    async def close_round(tenant_id: str, body: CloseRequest, request: Request):
        e = request.app.state.engine
        async with e.lock:
            await e.expire()
            result = await e.db("close_round", tenant_id, body.round_version)
            await e.publish([tenant_id])
            return result

    @app.delete("/api/tenants/{tenant_id}/clients/{client_id}")
    async def remove_client(tenant_id: str, client_id: str, request: Request):
        e = request.app.state.engine
        async with e.lock:
            require(
                (tenant_id, client_id) not in e.clients,
                "CLIENT_ONLINE",
                "请先断开该设备",
            )
            await e.db("delete_client", tenant_id, client_id)
            await e.publish([tenant_id])
            return {"ok": True}

    @app.get("/api/rounds")
    async def history(
        request: Request,
        tenant_id: str | None = None,
        reason: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        e = request.app.state.engine
        require(
            reason in (None, "game_end", "timeout", "manual", "version_changed"),
            "INVALID_MESSAGE",
            "未知关闭原因",
            422,
        )
        async with e.lock:
            return await e.db("history", tenant_id, reason, limit, offset)

    @app.websocket("/ws/admin")
    async def admin_socket(ws: WebSocket):
        if not same_origin(ws.headers):
            await ws.close(code=1008)
            return
        await ws.accept()
        e, peer = ws.app.state.engine, Peer(ws)
        sender = asyncio.create_task(peer.send_loop())
        try:
            async with e.lock:
                e.admins.add(peer)
                await e.expire()
                peer.offer(wire("admin.snapshot", payload=await e.overview()))
            while True:
                text = await receive_text(ws)
                require(len(text.encode()) <= 65536, "INVALID_MESSAGE", "消息过大")
                try:
                    value = json.loads(text)
                    if value.get("type") == "ping":
                        peer.offer(wire("pong"))
                        continue
                    require(
                        value.get("type") == "subscribe",
                        "INVALID_MESSAGE",
                        "未知管理消息",
                    )
                    tenant_id = value.get("tenant_id")
                    async with e.lock:
                        if tenant_id is None:
                            peer.subscription = None
                        else:
                            identifier(tenant_id, True)
                            d = await e.detail(tenant_id)
                            peer.subscription = tenant_id
                            peer.offer(
                                wire("admin.round", tenant_id, d["round_version"], d)
                            )
                except (ValueError, AttributeError, DomainError) as ex:
                    peer.offer(
                        wire(
                            "error",
                            payload={
                                "code": getattr(ex, "code", "INVALID_MESSAGE"),
                                "message": str(ex),
                            },
                        )
                    )
        except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError, DomainError):
            pass
        finally:
            async with e.lock:
                e.admins.discard(peer)
            sender.cancel()
            with suppress(asyncio.CancelledError):
                await sender
            with suppress(Exception):
                await ws.close()

    @app.websocket("/ws/client")
    async def client_socket(ws: WebSocket):
        if not same_origin(ws.headers):
            await ws.close(code=1008)
            return
        await ws.accept()
        tenant_id, client_id = (
            ws.query_params.get("tenant_id"),
            ws.query_params.get("client_id"),
        )
        e, peer = ws.app.state.engine, Peer(ws, tenant_id, client_id)
        sender = asyncio.create_task(peer.send_loop())
        key = (tenant_id, client_id)
        try:
            async with e.lock:
                await e.expire()
                await e.db("register_client", tenant_id, client_id)
                old = e.clients.get(key)
                if old:
                    old.closing = True
                    with suppress(Exception):
                        await old.socket.close(code=4001, reason="已由新连接替换")
                e.clients[key] = peer
                await e.publish([tenant_id])
            while True:
                raw = await receive_text(ws)
                request_id = None
                try:
                    require(
                        len(raw.encode()) <= 65536,
                        "INVALID_MESSAGE",
                        "消息超过64KiB",
                        422,
                    )
                    value = json.loads(raw)
                    require(
                        isinstance(value, dict),
                        "INVALID_MESSAGE",
                        "消息必须是JSON对象",
                        422,
                    )
                    request_id = (
                        value.get("request_id")
                        if isinstance(value.get("request_id"), str)
                        else None
                    )
                    message = parse_message(value)
                    require(
                        message["tenant_id"] == tenant_id
                        and message["client_id"] == client_id,
                        "IDENTITY_MISMATCH",
                        "消息与连接的租户或客户端不一致",
                    )
                    async with e.lock:
                        require(
                            e.clients.get(key) is peer and not peer.closing,
                            "CONNECTION_REPLACED",
                            "此连接已失效",
                        )
                        await e.expire()
                        if message["type"] == "ping":
                            peer.offer(wire("pong", tenant_id, reply_to=request_id))
                        elif message["type"] == "round.get":
                            d = await e.detail(tenant_id, message["round_version"])
                            e.client_snapshot(peer, d, request_id)
                        else:
                            reply = await e.db("process", tenant_id, client_id, message)
                            peer.offer(reply)
                            await e.publish([tenant_id])
                except (ValueError, ValidationError, DomainError) as ex:
                    code = getattr(ex, "code", "INVALID_MESSAGE")
                    description = (
                        ex.message
                        if isinstance(ex, DomainError)
                        else "消息字段或数据格式错误"
                    )
                    peer.offer(
                        wire(
                            "error",
                            tenant_id,
                            payload={"code": code, "message": description},
                            reply_to=request_id,
                        )
                    )
        except DomainError as ex:
            # No concurrent queued writes exist yet when initial registration fails.
            await ws.send_json(
                wire(
                    "error", tenant_id, payload={"code": ex.code, "message": ex.message}
                )
            )
        except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
            pass
        finally:
            async with e.lock:
                if e.clients.get(key) is peer:
                    del e.clients[key]
                    await e.publish([tenant_id])
            sender.cancel()
            with suppress(asyncio.CancelledError):
                await sender
            with suppress(Exception):
                await ws.close()

    return app


app = create_app()
