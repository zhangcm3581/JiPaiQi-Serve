"""SQLite-backed round state machine. All state transitions commit atomically."""

import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["s", "h", "c", "d"]
MAX_VERSION = 2147483646


def now_ms():
    return int(time.time() * 1000)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DomainError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def require(condition, code, message, status=409):
    if not condition:
        raise DomainError(code, message, status)


def valid_version(value):
    require(
        type(value) is int and 1 <= value <= MAX_VERSION,
        "INVALID_MESSAGE",
        "版本必须是1到2147483646的整数",
        422,
    )


def identifier(value, tenant=False):
    pattern = r"[0-9]{1,32}" if tenant else r"[A-Za-z0-9_-]{1,64}"
    require(
        isinstance(value, str) and re.fullmatch(pattern, value),
        "INVALID_MESSAGE",
        "租户或客户端ID格式错误",
        422,
    )


def normalize_hand(cards):
    require(
        isinstance(cards, list) and len(cards) == 13,
        "INVALID_MESSAGE",
        "必须上传13张完整手牌",
        422,
    )
    for card in cards:
        require(
            isinstance(card, dict)
            and set(card) == {"rank", "suit"}
            and card["rank"] in RANKS
            and card["suit"] in SUITS,
            "INVALID_MESSAGE",
            "牌面仅接受合法rank和suit",
            422,
        )
    counts = Counter((c["suit"], c["rank"]) for c in cards)
    require(max(counts.values()) <= 2, "DECK_OVERFLOW", "同花色同点数超过两张")
    return sorted(cards, key=lambda c: (SUITS.index(c["suit"]), RANKS.index(c["rank"])))


class Store:
    def __init__(self, path, clock=now_ms):
        self.path, self.clock = str(path), clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS tenants(
              id TEXT PRIMARY KEY, note TEXT NOT NULL, version INTEGER NOT NULL,
              timeout_seconds INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
              created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS clients(
              tenant_id TEXT NOT NULL REFERENCES tenants(id), id TEXT NOT NULL,
              last_bound INTEGER, created_at INTEGER NOT NULL,
              PRIMARY KEY(tenant_id,id));
            CREATE TABLE IF NOT EXISTS rounds(
              tenant_id TEXT NOT NULL REFERENCES tenants(id), version INTEGER NOT NULL,
              state TEXT NOT NULL, created_at INTEGER NOT NULL, first_received_at INTEGER,
              deadline_at INTEGER, ready_at INTEGER, closed_at INTEGER, close_reason TEXT,
              result TEXT, calculation_ms REAL, client_snapshot TEXT,
              PRIMARY KEY(tenant_id,version));
            CREATE TABLE IF NOT EXISTS starts(
              tenant_id TEXT NOT NULL, client_id TEXT NOT NULL, event_id TEXT NOT NULL,
              version INTEGER NOT NULL, fingerprint TEXT NOT NULL,
              PRIMARY KEY(tenant_id,client_id,event_id));
            CREATE TABLE IF NOT EXISTS hands(
              tenant_id TEXT NOT NULL, version INTEGER NOT NULL, client_id TEXT NOT NULL,
              cards TEXT NOT NULL, received_at INTEGER NOT NULL,
              PRIMARY KEY(tenant_id,version,client_id),
              FOREIGN KEY(tenant_id,version) REFERENCES rounds(tenant_id,version));
            CREATE TABLE IF NOT EXISTS requests(
              tenant_id TEXT NOT NULL, client_id TEXT NOT NULL, request_id TEXT NOT NULL,
              fingerprint TEXT NOT NULL, reply TEXT NOT NULL,
              PRIMARY KEY(tenant_id,client_id,request_id));
            CREATE INDEX IF NOT EXISTS rounds_closed ON rounds(closed_at DESC);
            """)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def tenant(self, db, tenant_id):
        t = db.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        require(t is not None, "TENANT_NOT_FOUND", "租户不存在", 404)
        return t

    def round(self, db, tenant_id, version):
        valid_version(version)
        r = db.execute(
            "SELECT * FROM rounds WHERE tenant_id=? AND version=?", (tenant_id, version)
        ).fetchone()
        require(r is not None, "ROUND_NOT_FOUND", "版本不存在", 404)
        return r

    def _new_round(self, db, tenant_id, version):
        db.execute(
            "INSERT INTO rounds(tenant_id,version,state,created_at) VALUES(?,?,?,?)",
            (tenant_id, version, "waiting", self.clock()),
        )

    def create_tenant(self, tenant_id, note="", round_version=1, timeout_seconds=180):
        identifier(tenant_id, True)
        self._validate_settings(note, round_version, timeout_seconds)
        with self.connect(True) as db:
            require(
                not db.execute(
                    "SELECT 1 FROM tenants WHERE id=?", (tenant_id,)
                ).fetchone(),
                "TENANT_EXISTS",
                "租户ID已存在",
            )
            db.execute(
                "INSERT INTO tenants VALUES(?,?,?,?,?,?)",
                (tenant_id, note, round_version, timeout_seconds, 1, self.clock()),
            )
            self._new_round(db, tenant_id, round_version)
        return {"tenant_id": tenant_id, "round_version": round_version}

    def _validate_settings(self, note, version, timeout):
        valid_version(version)
        require(
            isinstance(note, str) and len(note) <= 300,
            "INVALID_MESSAGE",
            "备注不能超过300字",
            422,
        )
        require(
            type(timeout) is int and 10 <= timeout <= 3600,
            "INVALID_MESSAGE",
            "超时须为10到3600秒的整数",
            422,
        )

    def _close(self, db, t, reason, target=None):
        r = self.round(db, t["id"], t["version"])
        require(r["state"] != "closed", "ROUND_CLOSED", "本轮已关闭")
        version = target if target is not None else t["version"] + 1
        if version <= MAX_VERSION:
            valid_version(version)
        members = [
            x["id"]
            for x in db.execute(
                "SELECT id FROM clients WHERE tenant_id=? ORDER BY created_at,id",
                (t["id"],),
            )
        ]
        db.execute(
            "UPDATE rounds SET state=?,closed_at=?,close_reason=?,client_snapshot=? "
            "WHERE tenant_id=? AND version=?",
            ("closed", self.clock(), reason, encode(members), t["id"], t["version"]),
        )
        if version > MAX_VERSION:
            # Never wrap a numeric version onto a previously used round.
            db.execute("UPDATE tenants SET enabled=0 WHERE id=?", (t["id"],))
            return None
        db.execute("UPDATE tenants SET version=? WHERE id=?", (version, t["id"]))
        self._new_round(db, t["id"], version)
        return version

    def update_tenant(self, tenant_id, changes):
        with self.connect(True) as db:
            t = self.tenant(db, tenant_id)
            note = changes.get("note", t["note"])
            version = changes.get("round_version", t["version"])
            timeout = changes.get("timeout_seconds", t["timeout_seconds"])
            enabled = changes.get("enabled", bool(t["enabled"]))
            self._validate_settings(note, version, timeout)
            require(
                type(enabled) is bool, "INVALID_MESSAGE", "enabled必须为布尔值", 422
            )
            require(version >= t["version"], "VERSION_REUSED", "不能回退或复用历史版本")
            require(
                not (
                    enabled
                    and self.round(db, tenant_id, t["version"])["state"] == "closed"
                ),
                "VERSION_EXHAUSTED",
                "版本号已用尽，请创建新租户",
            )
            if version > t["version"]:
                self._close(db, t, "version_changed", version)
            elif t["enabled"] and not enabled:
                self._close(db, t, "manual")
            db.execute(
                "UPDATE tenants SET note=?,timeout_seconds=?,enabled=? WHERE id=?",
                (note, timeout, int(enabled), tenant_id),
            )
        return {"tenant_id": tenant_id}

    def close_round(self, tenant_id, version):
        valid_version(version)
        with self.connect(True) as db:
            t = self.tenant(db, tenant_id)
            require(t["version"] == version, "VERSION_MISMATCH", "版本已变化，请刷新")
            return {"round_version": self._close(db, t, "manual")}

    def expire(self):
        changed = []
        with self.connect(True) as db:
            rows = db.execute(
                "SELECT t.* FROM tenants t JOIN rounds r "
                "ON t.id=r.tenant_id AND t.version=r.version "
                "WHERE r.state!='closed' AND r.deadline_at<=?",
                (self.clock(),),
            ).fetchall()
            for t in rows:
                self._close(db, t, "timeout")
                changed.append(t["id"])
        return changed

    def register_client(self, tenant_id, client_id):
        identifier(tenant_id, True)
        identifier(client_id)
        with self.connect(True) as db:
            t = self.tenant(db, tenant_id)
            require(t["enabled"], "TENANT_DISABLED", "租户已停用")
            c = db.execute(
                "SELECT * FROM clients WHERE tenant_id=? AND id=?",
                (tenant_id, client_id),
            ).fetchone()
            if not c:
                count = db.execute(
                    "SELECT count(*) FROM clients WHERE tenant_id=?", (tenant_id,)
                ).fetchone()[0]
                require(count < 7, "CLIENT_LIMIT", "该租户已登记7个客户端")
                db.execute(
                    "INSERT INTO clients VALUES(?,?,NULL,?)",
                    (tenant_id, client_id, self.clock()),
                )

    def delete_client(self, tenant_id, client_id):
        with self.connect(True) as db:
            t = self.tenant(db, tenant_id)
            require(
                self.round(db, tenant_id, t["version"])["state"] == "waiting",
                "ROUND_ACTIVE",
                "仅等待新局时可移除设备",
            )
            db.execute(
                "DELETE FROM clients WHERE tenant_id=? AND id=?", (tenant_id, client_id)
            )

    def process(self, tenant_id, client_id, message):
        """Acks and their fingerprints are stored in the same transaction as mutations."""
        fingerprint = hashlib.sha256(encode(message).encode()).hexdigest()
        with self.connect(True) as db:
            t = self.tenant(db, tenant_id)
            old = db.execute(
                "SELECT * FROM requests WHERE tenant_id=? AND client_id=? AND request_id=?",
                (tenant_id, client_id, message["request_id"]),
            ).fetchone()
            if old:
                require(
                    old["fingerprint"] == fingerprint,
                    "REQUEST_CONFLICT",
                    "请求ID已用于不同内容",
                )
                return json.loads(old["reply"])
            require(t["enabled"], "TENANT_DISABLED", "租户已停用")
            c = db.execute(
                "SELECT * FROM clients WHERE tenant_id=? AND id=?",
                (tenant_id, client_id),
            ).fetchone()
            require(c is not None, "CLIENT_NOT_FOUND", "设备尚未登记")
            version, payload, action = (
                message["round_version"],
                message["payload"],
                message["type"],
            )
            valid_version(version)
            r = self.round(db, tenant_id, version)
            require(r["state"] != "closed", "ROUND_CLOSED", "本轮已关闭")
            require(version == t["version"], "VERSION_MISMATCH", "版本与服务端不一致")
            require(
                not r["deadline_at"] or r["deadline_at"] > self.clock(),
                "ROUND_CLOSED",
                "本轮已超时，等待同步下一版本",
            )
            ack = {"action": action, "duplicate": False}
            if action == "round.join":
                require(r["state"] != "ready", "ROUND_READY", "本轮结果已锁定")
                start = db.execute(
                    "SELECT * FROM starts WHERE tenant_id=? AND client_id=? AND event_id=?",
                    (tenant_id, client_id, payload["start_event_id"]),
                ).fetchone()
                start_fp = encode({"version": version, **payload})
                if start:
                    require(
                        start["fingerprint"] == start_fp,
                        "START_EVENT_CONFLICT",
                        "同一个新局事件不能绑定其他版本",
                    )
                    ack["duplicate"] = True
                else:
                    require(
                        c["last_bound"] != version,
                        "SYNC_REQUIRED",
                        "本轮已绑定，请重试原新局事件",
                    )
                    if payload["sync_basis"] == "initial_start":
                        require(
                            c["last_bound"] is None
                            and payload["previous_round_version"] is None,
                            "SYNC_REQUIRED",
                            "已有绑定记录，须确认结束到新局的切换",
                        )
                    else:
                        previous = payload["previous_round_version"]
                        require(
                            previous == version - 1 and c["last_bound"] == previous,
                            "SYNC_REQUIRED",
                            "已失步，请等待同步或重新登记设备",
                        )
                    db.execute(
                        "INSERT INTO starts VALUES(?,?,?,?,?)",
                        (
                            tenant_id,
                            client_id,
                            payload["start_event_id"],
                            version,
                            start_fp,
                        ),
                    )
                    db.execute(
                        "UPDATE clients SET last_bound=? WHERE tenant_id=? AND id=?",
                        (version, tenant_id, client_id),
                    )
                db.execute(
                    "UPDATE rounds SET state='collecting' WHERE tenant_id=? AND version=? AND state='waiting'",
                    (tenant_id, version),
                )
                ack["bound_round_version"] = version
            elif action == "hand.submit":
                require(c["last_bound"] == version, "NOT_JOINED", "请先完成新局绑定")
                cards = normalize_hand(payload["cards"])
                old_hand = db.execute(
                    "SELECT cards FROM hands WHERE tenant_id=? AND version=? AND client_id=?",
                    (tenant_id, version, client_id),
                ).fetchone()
                if old_hand:
                    require(
                        old_hand["cards"] == encode(cards),
                        "HAND_CONFLICT",
                        "该设备本轮已提交不同手牌",
                    )
                    ack["duplicate"] = True
                else:
                    require(
                        r["state"] == "collecting", "ROUND_READY", "本轮不再接收手牌"
                    )
                    began = time.perf_counter()
                    all_hands = [
                        json.loads(x["cards"])
                        for x in db.execute(
                            "SELECT cards FROM hands WHERE tenant_id=? AND version=?",
                            (tenant_id, version),
                        )
                    ]
                    counts = Counter(
                        (c["suit"], c["rank"])
                        for hand in all_hands + [cards]
                        for c in hand
                    )
                    require(
                        all(v <= 2 for v in counts.values()),
                        "DECK_OVERFLOW",
                        "累计同花色同点数超过两张",
                    )
                    require(len(all_hands) < 7, "ROUND_READY", "已收齐7份")
                    stamp = self.clock()
                    db.execute(
                        "INSERT INTO hands VALUES(?,?,?,?,?)",
                        (tenant_id, version, client_id, encode(cards), stamp),
                    )
                    if r["first_received_at"] is None:
                        db.execute(
                            "UPDATE rounds SET first_received_at=?,deadline_at=? WHERE tenant_id=? AND version=?",
                            (
                                stamp,
                                stamp + t["timeout_seconds"] * 1000,
                                tenant_id,
                                version,
                            ),
                        )
                    if len(all_hands) == 6:
                        result = [
                            {"suit": s, "rank": rank, "count": 2 - counts[(s, rank)]}
                            for s in SUITS
                            for rank in RANKS
                            if counts[(s, rank)] < 2
                        ]
                        require(
                            sum(c["count"] for c in result) == 13,
                            "DECK_OVERFLOW",
                            "剩余牌数异常",
                        )
                        db.execute(
                            "UPDATE rounds SET state='ready',result=?,ready_at=?,calculation_ms=? "
                            "WHERE tenant_id=? AND version=?",
                            (
                                encode(result),
                                stamp,
                                (time.perf_counter() - began) * 1000,
                                tenant_id,
                                version,
                            ),
                        )
            elif action == "round.end":
                require(
                    c["last_bound"] == version, "NOT_JOINED", "仅已绑定设备可报告结束"
                )
                ack["next_round_version"] = self._close(db, t, "game_end")
            else:
                raise DomainError("INVALID_MESSAGE", "未知业务消息", 422)
            reply = {
                "protocol_version": 1,
                "type": "ack",
                "tenant_id": tenant_id,
                "round_version": version,
                "reply_to": message["request_id"],
                "server_time_ms": self.clock(),
                "payload": ack,
            }
            db.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?)",
                (
                    tenant_id,
                    client_id,
                    message["request_id"],
                    fingerprint,
                    encode(reply),
                ),
            )
            return reply

    def detail(self, tenant_id, version=None):
        with self.connect() as db:
            t = self.tenant(db, tenant_id)
            r = dict(
                self.round(
                    db, tenant_id, version if version is not None else t["version"]
                )
            )
            hands = [
                dict(x)
                for x in db.execute(
                    "SELECT * FROM hands WHERE tenant_id=? AND version=? ORDER BY received_at,client_id",
                    (tenant_id, r["version"]),
                )
            ]
            clients = [
                dict(x)
                for x in db.execute(
                    "SELECT * FROM clients WHERE tenant_id=? ORDER BY created_at,id",
                    (tenant_id,),
                )
            ]
            members = (
                json.loads(r["client_snapshot"])
                if r["client_snapshot"]
                else [c["id"] for c in clients]
            )
            by_id = {h["client_id"]: h for h in hands}
            members = list(dict.fromkeys(members + list(by_id)))
            bound_here = {
                x["client_id"]
                for x in db.execute(
                    "SELECT client_id FROM starts WHERE tenant_id=? AND version=?",
                    (tenant_id, r["version"]),
                )
            }
            devices = []
            for cid in members:
                h = by_id.get(cid)
                devices.append(
                    {
                        "client_id": cid,
                        "cards": json.loads(h["cards"]) if h else None,
                        "received_at_ms": h["received_at"] if h else None,
                        "bound_round_version": (
                            r["version"] if cid in bound_here else None
                        )
                        if r["state"] == "closed"
                        else next(
                            (c["last_bound"] for c in clients if c["id"] == cid), None
                        ),
                    }
                )
            return {
                "tenant_id": tenant_id,
                "round_version": r["version"],
                "state": r["state"],
                "note": t["note"],
                "enabled": bool(t["enabled"]),
                "devices": devices,
                "received_count": len(hands),
                "expected_count": 7,
                "missing_client_ids": [cid for cid in members if cid not in by_id],
                "unregistered_count": max(0, 7 - len(members)),
                "created_at_ms": r["created_at"],
                "first_received_at_ms": r["first_received_at"],
                "deadline_at_ms": r["deadline_at"],
                "closed_at_ms": r["closed_at"],
                "close_reason": r["close_reason"],
                "result": json.loads(r["result"]) if r["result"] else None,
                "calculation_ms": r["calculation_ms"],
            }

    def tenants(self):
        with self.connect() as db:
            return [
                dict(x)
                for x in db.execute(
                    "SELECT id AS tenant_id,note,version AS round_version,timeout_seconds,enabled "
                    "FROM tenants ORDER BY created_at,id"
                )
            ]

    def history(self, tenant_id=None, reason=None, limit=20, offset=0):
        clauses, args = ["r.state='closed'"], []
        if tenant_id:
            clauses.append("r.tenant_id=?")
            args.append(tenant_id)
        if reason:
            clauses.append("r.close_reason=?")
            args.append(reason)
        where = " AND ".join(clauses)
        with self.connect() as db:
            total = db.execute(
                "SELECT count(*) FROM rounds r WHERE " + where, args
            ).fetchone()[0]
            items = [
                dict(x)
                for x in db.execute(
                    "SELECT r.tenant_id,r.version AS round_version,r.created_at AS created_at_ms,"
                    "r.closed_at AS closed_at_ms,r.close_reason,r.result,r.calculation_ms,"
                    "(SELECT count(*) FROM hands h WHERE h.tenant_id=r.tenant_id AND h.version=r.version) AS received_count "
                    "FROM rounds r WHERE "
                    + where
                    + " ORDER BY r.closed_at DESC,r.tenant_id,r.version DESC LIMIT ? OFFSET ?",
                    args + [limit, offset],
                )
            ]
            for x in items:
                x["has_result"] = x.pop("result") is not None
            return {"items": items, "total": total, "limit": limit, "offset": offset}

    def summary(self):
        stamp = self.clock()
        start = datetime.fromtimestamp(stamp / 1000, ZoneInfo("Asia/Shanghai")).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        with self.connect() as db:
            result = db.execute(
                "SELECT count(*) AS completed_today,avg(calculation_ms) AS average_calculation_ms FROM rounds "
                "WHERE result IS NOT NULL AND ready_at>=? AND ready_at<=?",
                (int(start.timestamp() * 1000), stamp),
            ).fetchone()
            return dict(result)
