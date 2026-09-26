from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
# 火线片段归并
FIRELINE_ENTITY='火线片段'; GUST_SCALE=12
def fireline_deadline_hours(uncontrolled_length,gust_level):
    """归并后按未控制长度和阵风重算响应时限（小时，1~72）。"""
    length=max(0.0,float(uncontrolled_length)); gust=max(0,min(GUST_SCALE,int(gust_level)))
    hours=72.0/(1.0+length/5.0)/(1.0+gust/4.0)
    return max(1,min(72,int(round(hours))))
def merge_segments(segments):
    """把同事件内首尾相接或重叠的片段合成一段；任一段在燃烧则合并段燃烧。"""
    ordered=sorted(segments,key=lambda s:(s["start_marker"],s["end_marker"]))
    merged=[]
    for seg in ordered:
        if not merged or seg["start_marker"]>merged[-1]["end_marker"]:
            merged.append({
                "start_marker":seg["start_marker"],"end_marker":seg["end_marker"],
                "length":int(seg["end_marker"]-seg["start_marker"]),
                "fire_status":seg["fire_status"],"gust_level":seg["gust_level"],
                "observed_at":seg["observed_at"],"site_codes":[seg["site_code"]],
                "merged":False,"segment_count":1,
            })
        else:
            group=merged[-1]
            overlaps_status=group["fire_status"]=='burning' or seg["fire_status"]=='burning'
            group["end_marker"]=max(group["end_marker"],seg["end_marker"])
            group["length"]=int(group["end_marker"]-group["start_marker"])
            group["fire_status"]='burning' if overlaps_status else 'controlled'
            group["gust_level"]=max(group["gust_level"],seg["gust_level"])
            group["observed_at"]=max(group["observed_at"],seg["observed_at"])
            group["site_codes"].append(seg["site_code"])
            group["segment_count"]+=1
            group["merged"]=True
    return merged
