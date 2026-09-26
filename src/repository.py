from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES, UNCONTROLLED_FIRE_STATES


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
                CREATE TABLE IF NOT EXISTS segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    marker_prefix TEXT NOT NULL,
                    start_num INTEGER NOT NULL,
                    end_num INTEGER NOT NULL,
                    start_marker TEXT NOT NULL,
                    end_marker TEXT NOT NULL,
                    fire_status TEXT NOT NULL,
                    gust_level REAL NOT NULL DEFAULT 0,
                    observed_at TEXT NOT NULL,
                    field_refs TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS segment_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    field_ref TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, field_ref)
                );
                CREATE TABLE IF NOT EXISTS segment_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    conflicting_item_id INTEGER NOT NULL,
                    conflicting_item_title TEXT NOT NULL,
                    field_ref TEXT NOT NULL,
                    start_marker TEXT NOT NULL,
                    end_marker TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','resolved')),
                    detail TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
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

    @staticmethod
    def _parse_observed(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _segment_view(row: Dict[str, Any]) -> Dict[str, Any]:
        view = dict(row)
        refs = json.loads(view.pop("field_refs"))
        view["field_refs"] = refs
        view["merged"] = len(refs) > 1
        view["length"] = view["end_num"] - view["start_num"]
        return view

    def register_segment(self, item_id: int, field_ref: str, marker_prefix: str,
                         start_num: int, end_num: int, start_marker: str,
                         end_marker: str, fire_status: str, gust_level: float,
                         observed_at: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock:
            row = self.conn.execute(
                "SELECT result FROM segment_reports WHERE item_id=? AND field_ref=?",
                (item_id, field_ref),
            ).fetchone()
            if row is not None:
                return {"outcome": "replay", "segment": json.loads(row["result"])}
            item = self.get_item(item_id)
            if item["status"] == "closed":
                raise ConflictError("事件已关闭，不能登记火线片段")
            overlap = self.conn.execute(
                """SELECT s.item_id, i.title FROM segments s
                   JOIN items i ON i.id=s.item_id
                   WHERE s.item_id!=? AND s.marker_prefix=? AND i.status!='closed'
                   AND s.start_num<=? AND ?<=s.end_num LIMIT 1""",
                (item_id, marker_prefix, end_num, start_num),
            ).fetchone()
            if overlap is not None:
                detail = (f"片段{start_marker}~{end_marker}与未关闭事件"
                          f"#{overlap['item_id']}({overlap['title']})的火线重叠")
                with self.conn:
                    self.conn.execute(
                        """INSERT INTO segment_conflicts(item_id, conflicting_item_id,
                           conflicting_item_title, field_ref, start_marker, end_marker,
                           status, detail, created_by, created_at)
                           VALUES(?,?,?,?,?,?,'pending',?,?,?)""",
                        (item_id, overlap["item_id"], overlap["title"], field_ref,
                         start_marker, end_marker, detail, actor, now),
                    )
                return {"outcome": "conflict", "message": detail + "，已登记待核",
                        "conflicting_item_id": overlap["item_id"]}
            connected = [dict(r) for r in self.conn.execute(
                """SELECT * FROM segments WHERE item_id=? AND marker_prefix=?
                   AND start_num<=? AND ?<=end_num+1 ORDER BY id""",
                (item_id, marker_prefix, end_num + 1, start_num),
            ).fetchall()]
            starts = [(start_num, start_marker)] + [(p["start_num"], p["start_marker"]) for p in connected]
            ends = [(end_num, end_marker)] + [(p["end_num"], p["end_marker"]) for p in connected]
            merged_start = min(starts, key=lambda pair: pair[0])
            merged_end = max(ends, key=lambda pair: pair[0])
            observations = [(observed_at, fire_status, gust_level)] + [
                (p["observed_at"], p["fire_status"], p["gust_level"]) for p in connected]
            latest = max(observations, key=lambda o: self._parse_observed(o[0]))
            refs = sorted({field_ref} | {ref for p in connected for ref in json.loads(p["field_refs"])})
            with self.conn:
                if connected:
                    keep = min(p["id"] for p in connected)
                    drops = [p["id"] for p in connected if p["id"] != keep]
                    self.conn.execute(
                        """UPDATE segments SET start_num=?, end_num=?, start_marker=?,
                           end_marker=?, fire_status=?, gust_level=?, observed_at=?,
                           field_refs=?, updated_at=? WHERE id=?""",
                        (merged_start[0], merged_end[0], merged_start[1], merged_end[1],
                         latest[1], latest[2], latest[0],
                         json.dumps(refs, ensure_ascii=False), now, keep),
                    )
                    if drops:
                        marks = ",".join("?" * len(drops))
                        self.conn.execute(f"DELETE FROM segments WHERE id IN ({marks})", drops)
                    segment_id = keep
                else:
                    cur = self.conn.execute(
                        """INSERT INTO segments(item_id, marker_prefix, start_num, end_num,
                           start_marker, end_marker, fire_status, gust_level, observed_at,
                           field_refs, created_by, created_at, updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (item_id, marker_prefix, merged_start[0], merged_end[0],
                         merged_start[1], merged_end[1], latest[1], latest[2], latest[0],
                         json.dumps(refs, ensure_ascii=False), actor, now, now),
                    )
                    segment_id = int(cur.lastrowid)
                result = {
                    "id": segment_id, "item_id": item_id, "field_ref": field_ref,
                    "start_marker": merged_start[1], "end_marker": merged_end[1],
                    "fire_status": latest[1], "gust_level": latest[2],
                    "observed_at": latest[0], "length": merged_end[0] - merged_start[0],
                    "merged": len(refs) > 1, "field_refs": refs,
                    "created_by": actor, "created_at": now,
                }
                self.conn.execute(
                    """INSERT INTO segment_reports(item_id, field_ref, result, created_by,
                       created_at) VALUES(?,?,?,?,?)""",
                    (item_id, field_ref,
                     json.dumps(result, ensure_ascii=False, sort_keys=True), actor, now),
                )
            return {"outcome": "created", "segment": result}

    def list_segments(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM segments WHERE item_id=? ORDER BY marker_prefix, start_num, id",
                (item_id,),
            ).fetchall()
        return [self._segment_view(dict(row)) for row in rows]

    def segment_stats(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT start_num, end_num, fire_status, gust_level FROM segments WHERE item_id=?",
                (item_id,),
            ).fetchall()
        open_rows = [r for r in rows if r["fire_status"] in UNCONTROLLED_FIRE_STATES]
        return {
            "segments": len(rows),
            "uncontrolled_segments": len(open_rows),
            "uncontrolled_length": sum(r["end_num"] - r["start_num"] for r in open_rows),
            "max_gust_level": max((r["gust_level"] for r in open_rows), default=0.0),
        }

    def list_segment_conflicts(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM segment_conflicts
                   WHERE item_id=? OR conflicting_item_id=? ORDER BY id DESC""",
                (item_id, item_id),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def close(self) -> None:
        with self._lock:
            self.conn.close()
