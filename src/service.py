from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_fire_status,
                     normalize_observed_at, normalize_severity, require_int,
                     require_number, require_text, GUST_MAX, GUST_MIN)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, FIRELINE_ENTITY,
                    RECORD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, fireline_deadline_hours, merge_segments,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)

FIRELINE_ROLES=set(['field_commander','incident_commander'])
FIRE_STATUS_UPDATE_ROLES=set(['field_commander'])
CONFLICT_RESOLVE_ROLES=set(['incident_commander'])
CONFLICT_LIST_ROLES=VIEW_ROLES


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

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if target in ("closed",) and self.repository.burning_segment_count(item_id) > 0:
            blockers = blockers + ["仍有燃烧中的火线片段，无法关闭"]
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        result = self.enrich(updated)
        summary = self.fireline_summary(item_id)
        result["fireline"] = summary
        if summary["segment_count"] > 0:
            result["deadline_hours"] = summary["fireline_deadline_hours"]
        return result

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        result = self.enrich(self.repository.get_item(item_id))
        result["fireline"] = self.fireline_summary(item_id)
        if result["fireline"]["segment_count"] > 0:
            result["deadline_hours"] = result["fireline"]["fireline_deadline_hours"]
        return result

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        items = []
        for item in self.repository.list_items(status):
            enriched = self.enrich(item)
            summary = self.fireline_summary(item["id"])
            compact = {k: v for k, v in summary.items()
                       if k in ("segment_count", "uncontrolled_length",
                                "gust_level", "fireline_deadline_hours",
                                "burning_segments", "pending_conflicts")}
            enriched["fireline"] = compact
            if compact["segment_count"] > 0:
                enriched["deadline_hours"] = compact["fireline_deadline_hours"]
            items.append(enriched)
        return items

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 火线片段归并 ----
    def register_fire_segment(self, item_id: int, payload: Dict[str, Any],
                              actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, FIRELINE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("事件已关闭，不能登记火线片段")
        site_code = require_text(payload.get("site_code"), "site_code", 100)
        start_marker = require_int(payload.get("start_marker"), "start_marker")
        end_marker = require_int(payload.get("end_marker"), "end_marker")
        if end_marker <= start_marker:
            from .domain import ValidationError
            raise ValidationError("end_marker必须大于start_marker")
        fire_status = normalize_fire_status(payload.get("fire_status"))
        gust_level = require_int(payload.get("gust_level"), "gust_level", GUST_MIN, GUST_MAX)
        observed_at = normalize_observed_at(payload.get("observed_at"))

        result = self.repository.register_fire_segment_if_clear(
            item_id, site_code, start_marker, end_marker, fire_status,
            gust_level, observed_at, actor)

        # 幂等：同一事件、同一现场编号重放，返回第一次结果
        if result["kind"] == "replay":
            return self._fireline_view(item_id, segment=result["segment"], replayed=True)

        # 片段已归到别的未关闭火线 → 退回并说明冲突事件，登记待核冲突
        if result["kind"] in ("site_code_taken", "segment_overlap"):
            owner = result["owner"]
            reason = result["kind"]
            conflict = self.repository.add_segment_conflict(
                reason, "incoming", item_id, owner["item_id"], site_code,
                start_marker, end_marker, owner["start_marker"], owner["end_marker"], actor)
            if conflict is not None:
                self.repository.append_audit("segment_conflict", FIRELINE_ENTITY, item_id, actor, {
                    "conflict_id": conflict["id"], "reason": reason,
                    "site_code": site_code, "owner_item_id": owner["item_id"]})
            if reason == "site_code_taken":
                message = f"现场编号{site_code}已归属于未关闭事件(事件{owner['item_id']})，片段退回待核"
            else:
                message = (f"界桩{start_marker}-{end_marker}与未关闭事件(事件{owner['item_id']})的片段"
                           f"{owner['site_code']}({owner['start_marker']}-{owner['end_marker']})重叠，片段退回待核")
            raise ConflictError(message, {
                "reason": reason, "site_code": site_code,
                "owner_item_id": owner["item_id"],
                "conflict_id": conflict["id"] if conflict else None,
                "submitted_range": [start_marker, end_marker],
                "owner_range": [owner["start_marker"], owner["end_marker"]]})

        segment = result["segment"]
        self.repository.append_audit("fire_segment_register", FIRELINE_ENTITY, item_id, actor, {
            "segment_id": segment["id"], "site_code": site_code,
            "start_marker": start_marker, "end_marker": end_marker,
            "fire_status": fire_status, "gust_level": gust_level})
        return self._fireline_view(item_id, segment=segment, replayed=False)

    def update_fire_segment(self, item_id: int, segment_id: int,
                            payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, FIRE_STATUS_UPDATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("事件已关闭，不能更新火线片段")
        fire_status = normalize_fire_status(payload.get("fire_status"))
        gust_raw = payload.get("gust_level")
        gust_level = require_int(gust_raw, "gust_level", GUST_MIN, GUST_MAX) if gust_raw is not None else None
        observed_at = normalize_observed_at(payload.get("observed_at"))
        owned = self.repository.list_fire_segments(item_id)
        if not any(s["id"] == segment_id for s in owned):
            from .domain import NotFoundError
            raise NotFoundError("火线片段不属于该事件")
        segment = self.repository.set_fire_segment_status(
            segment_id, fire_status, gust_level, observed_at, actor)
        self.repository.append_audit("fire_segment_update", FIRELINE_ENTITY, item_id, actor, {
            "segment_id": segment_id, "fire_status": fire_status})
        return self._fireline_view(item_id, segment=segment, replayed=False)

    def fireline_summary(self, item_id: int) -> Dict[str, Any]:
        self.repository.get_item(item_id)
        segments = self.repository.list_fire_segments(item_id)
        groups = merge_segments(segments)
        uncontrolled = sum(g["length"] for g in groups if g["fire_status"] == "burning")
        gust = max((g["gust_level"] for g in groups), default=0)
        return {
            "segment_count": len(segments),
            "merged_groups": groups,
            "uncontrolled_length": uncontrolled,
            "gust_level": gust,
            "fireline_deadline_hours": fireline_deadline_hours(uncontrolled, gust),
            "burning_segments": self.repository.burning_segment_count(item_id),
            "pending_conflicts": len(self.repository.list_segment_conflicts(item_id, "pending")),
        }

    def list_firelines(self, role: str, item_id: Optional[int] = None) -> Dict[str, Any]:
        self._view(role)
        if item_id is not None:
            return {"item_id": item_id, **self.fireline_summary(item_id)}
        items = self.repository.list_items()
        return {"firelines": [
            {"item_id": it["id"], "title": it["title"], "status": it["status"],
             **{k: v for k, v in self.fireline_summary(it["id"]).items() if k != "merged_groups"}}
            for it in items]}

    def list_conflicts(self, role: str, item_id: Optional[int] = None,
                       status: Optional[str] = None) -> list:
        ensure_role(role, CONFLICT_LIST_ROLES)
        return self.repository.list_segment_conflicts(item_id, status)

    def resolve_conflict(self, conflict_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CONFLICT_RESOLVE_ROLES)
        actor = require_text(actor, "actor", 100)
        conflict = self.repository.resolve_segment_conflict(conflict_id)
        self.repository.append_audit("segment_conflict_resolve", FIRELINE_ENTITY,
                                     conflict["reporter_item_id"], actor,
                                     {"conflict_id": conflict_id})
        return conflict

    def _fireline_view(self, item_id: int, segment: Dict[str, Any],
                       replayed: bool) -> Dict[str, Any]:
        summary = self.fireline_summary(item_id)
        return {"segment": segment, "replayed": replayed,
                "merged_group": self._group_of(summary["merged_groups"], segment["site_code"]),
                "uncontrolled_length": summary["uncontrolled_length"],
                "gust_level": summary["gust_level"],
                "fireline_deadline_hours": summary["fireline_deadline_hours"],
                "pending_conflicts": summary["pending_conflicts"]}

    @staticmethod
    def _group_of(groups: list, site_code: str) -> Optional[Dict[str, Any]]:
        for group in groups:
            if site_code in group["site_codes"]:
                return group
        return None

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
