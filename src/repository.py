from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fire_segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    site_code TEXT NOT NULL,
                    start_marker INTEGER NOT NULL,
                    end_marker INTEGER NOT NULL,
                    fire_status TEXT NOT NULL CHECK(fire_status IN ('burning','controlled')),
                    gust_level INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_fire_segments_incident_code
                    ON fire_segments(item_id, site_code);
                CREATE TABLE IF NOT EXISTS segment_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reason TEXT NOT NULL CHECK(reason IN ('site_code_taken','segment_overlap')),
                    direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
                    reporter_item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    owner_item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    site_code TEXT NOT NULL,
                    start_marker INTEGER NOT NULL,
                    end_marker INTEGER NOT NULL,
                    other_start_marker INTEGER,
                    other_end_marker INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','resolved')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_segment_conflicts_open
                    ON segment_conflicts(reason, reporter_item_id, owner_item_id,
                        site_code, start_marker, end_marker)
                    WHERE status='pending';
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---- 火线片段归并 ----
    def register_fire_segment_if_clear(self, item_id: int, site_code: str,
                                       start_marker: int, end_marker: int,
                                       fire_status: str, gust_level: int,
                                       observed_at: str, actor: str) -> Dict[str, Any]:
        """单事务完成幂等/跨事件冲突判定与插入，返回带kind的结果。"""
        now = utc_now()
        with self._lock, self.conn:
            item = self.conn.execute(
                "SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            replay = self.conn.execute(
                "SELECT * FROM fire_segments WHERE item_id=? AND site_code=?",
                (item_id, site_code)).fetchone()
            if replay is not None:
                return {"kind": "replay", "segment": dict(replay)}
            owner = self.conn.execute(
                """SELECT s.*, i.status AS item_status FROM fire_segments s
                   JOIN items i ON i.id=s.item_id
                   WHERE s.site_code=? AND s.item_id<>? AND i.status<>'closed' LIMIT 1""",
                (site_code, item_id)).fetchone()
            if owner is not None:
                return {"kind": "site_code_taken", "owner": dict(owner)}
            overlap = self.conn.execute(
                """SELECT s.*, i.status AS item_status FROM fire_segments s
                   JOIN items i ON i.id=s.item_id
                   WHERE s.item_id<>? AND i.status<>'closed'
                     AND s.start_marker < ? AND s.end_marker > ?
                   ORDER BY s.id LIMIT 1""",
                (item_id, end_marker, start_marker)).fetchone()
            if overlap is not None:
                return {"kind": "segment_overlap", "owner": dict(overlap)}
            cur = self.conn.execute(
                """INSERT INTO fire_segments(item_id, site_code, start_marker, end_marker,
                   fire_status, gust_level, observed_at, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (item_id, site_code, start_marker, end_marker, fire_status,
                 gust_level, observed_at, actor, now))
            row = self.conn.execute(
                "SELECT * FROM fire_segments WHERE id=?", (int(cur.lastrowid),)).fetchone()
            return {"kind": "inserted", "segment": dict(row), "item_status": item["status"]}

    def list_fire_segments(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM fire_segments WHERE item_id=? ORDER BY id",
                (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def set_fire_segment_status(self, segment_id: int, fire_status: str,
                                gust_level: Optional[int], observed_at: str,
                                actor: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE fire_segments SET fire_status=?,
                       gust_level=COALESCE(?, gust_level), observed_at=?
                   WHERE id=?""",
                (fire_status, gust_level, observed_at, segment_id))
            if cur.rowcount == 0:
                raise NotFoundError("火线片段不存在")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM fire_segments WHERE id=?", (segment_id,)).fetchone()
        return dict(row)

    def burning_segment_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM fire_segments WHERE item_id=? AND fire_status='burning'",
                (item_id,)).fetchone()
        return int(row["n"])

    def add_segment_conflict(self, reason: str, direction: str, reporter_item_id: int,
                             owner_item_id: int, site_code: str, start_marker: int,
                             end_marker: int, other_start: Optional[int],
                             other_end: Optional[int], actor: str) -> Optional[Dict[str, Any]]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO segment_conflicts(reason, direction, reporter_item_id,
                       owner_item_id, site_code, start_marker, end_marker,
                       other_start_marker, other_end_marker, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (reason, direction, reporter_item_id, owner_item_id, site_code,
                     start_marker, end_marker, other_start, other_end, actor, now))
                conflict_id = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM segment_conflicts WHERE id=?", (conflict_id,)).fetchone()
        return dict(row)

    def list_segment_conflicts(self, item_id: Optional[int] = None,
                               status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM segment_conflicts"
        clauses: List[str] = []
        params: List[Any] = []
        if item_id is not None:
            clauses.append("(reporter_item_id=? OR owner_item_id=?)")
            params.extend([item_id, item_id])
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def resolve_segment_conflict(self, conflict_id: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE segment_conflicts SET status='resolved', resolved_at=?
                   WHERE id=? AND status='pending'""",
                (now, conflict_id))
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM segment_conflicts WHERE id=?", (conflict_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("冲突事件不存在")
                raise ConflictError("冲突事件已核销")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM segment_conflicts WHERE id=?", (conflict_id,)).fetchone()
        return dict(row)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
