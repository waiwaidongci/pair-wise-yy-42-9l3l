from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_fire_status, normalize_severity, parse_marker,
                     require_number, require_text, require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, MAX_GUST_LEVEL,
                    RECORD_ROLES, SEGMENT_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    segment_closure_blockers, segment_deadline_hours,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def register_segment(self, item_id: int, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, SEGMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        field_ref = require_text(payload.get("field_ref"), "field_ref", 100)
        start_prefix, start_num, start_marker = parse_marker(
            payload.get("start_marker"), "start_marker")
        end_prefix, end_num, end_marker = parse_marker(
            payload.get("end_marker"), "end_marker")
        if start_prefix != end_prefix:
            raise ValidationError("起止界桩必须在同一桩线上")
        if start_num > end_num:
            raise ValidationError("起点界桩不能大于止点界桩")
        fire_status = normalize_fire_status(payload.get("fire_status"))
        gust_level = require_number(payload.get("gust_level", 0), "gust_level")
        if gust_level > MAX_GUST_LEVEL:
            raise ValidationError(f"gust_level不能超过{MAX_GUST_LEVEL}")
        if float(gust_level) != int(gust_level):
            raise ValidationError("gust_level必须是整数等级")
        gust_level = int(gust_level)
        observed_at = require_timestamp(payload.get("observed_at"), "observed_at")
        outcome = self.repository.register_segment(
            item_id, field_ref, start_prefix, start_num, end_num, start_marker,
            end_marker, fire_status, gust_level, observed_at, actor)
        if outcome["outcome"] == "conflict":
            self.repository.append_audit("segment_conflict", ENTITY, item_id, actor, {
                "field_ref": field_ref,
                "conflicting_item_id": outcome["conflicting_item_id"],
            })
            raise ConflictError(outcome["message"])
        if outcome["outcome"] == "created":
            segment = outcome["segment"]
            self.repository.append_audit("segment", ENTITY, item_id, actor, {
                "segment_id": segment["id"], "field_ref": field_ref,
                "fire_status": fire_status, "merged": segment["merged"],
            })
        return outcome["segment"]

    def list_segments(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return {
            "segments": self.repository.list_segments(item_id),
            "conflicts": self.repository.list_segment_conflicts(item_id),
        }

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        stats = self.repository.segment_stats(item_id)
        blockers += segment_closure_blockers(target, stats["uncontrolled_segments"])
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        stats = self.repository.segment_stats(item["id"])
        if stats["segments"]:
            result["deadline_hours"] = segment_deadline_hours(
                item["severity"], stats["uncontrolled_length"], stats["max_gust_level"])
        else:
            result["deadline_hours"] = response_deadline_hours(
                item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["uncontrolled_length"] = stats["uncontrolled_length"]
        result["max_gust_level"] = stats["max_gust_level"]
        result["open_segment_count"] = stats["uncontrolled_segments"]
        return result
