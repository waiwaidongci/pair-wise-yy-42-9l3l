from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message,details=None):
        super().__init__(message); self.message=message; self.details=details
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; ROLES=['field_commander', 'incident_commander', 'logistics', 'viewer']
FIRE_STATUSES=['burning','controlled']; GUST_MIN=0; GUST_MAX=12; OPEN_STATES=set(STATES[:-1])
CONFLICT_SITE_CODE='site_code_taken'; CONFLICT_OVERLAP='segment_overlap'; CONFLICT_DIRECTIONS=['incoming','outgoing']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def require_int(value,field,minimum=None,maximum=None):
    if isinstance(value,bool) or not isinstance(value,int):
        raise ValidationError(f"{field}必须是整数")
    if minimum is not None and value<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    if maximum is not None and value>maximum: raise ValidationError(f"{field}不能大于{maximum}")
    return value
def normalize_fire_status(value):
    if value not in FIRE_STATUSES: raise ValidationError("fire_status必须是burning或controlled")
    return value
def normalize_observed_at(value):
    value=require_text(value,"observed_at",40)
    try: datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError as exc: raise ValidationError("observed_at必须是ISO-8601时间") from exc
    return value
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
