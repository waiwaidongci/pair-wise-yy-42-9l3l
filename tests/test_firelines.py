import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import fireline_deadline_hours, merge_segments, STATES, TRANSITION_ROLES

FC='field_commander'; IC='incident_commander'
def seg_payload(site_code, start, end, fire_status='burning', gust=3, observed='2026-09-26T10:00:00+00:00'):
    return {"site_code":site_code,"start_marker":start,"end_marker":end,
            "fire_status":fire_status,"gust_level":gust,"observed_at":observed}

class FirelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.repo=Repository(str(Path(self.tmp.name)/"test.db"))
        self.service=Service(self.repo)
        self.a=self.service.create_item({"title":"火线事件A","description":"a","severity":'high',
            "quantity":1,"threshold":10,"external_ref":"FA-1"},"c",FC)
        self.b=self.service.create_item({"title":"火线事件B","description":"b","severity":'high',
            "quantity":1,"threshold":10,"external_ref":"FB-1"},"c",FC)
    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()
    def _close(self, item):
        current=item
        for target in STATES[1:]:
            current=self.service.transition(current["id"],target,current["version"],"ic",
                                            TRANSITION_ROLES[target][0])
        return current

    def test_idempotent_replay_returns_first_result(self):
        payload=seg_payload("S-1",0,5)
        first=self.service.register_fire_segment(self.a["id"],payload,"p1",FC)
        self.assertFalse(first["replayed"]); self.assertEqual(first["segment"]["id"],1)
        # 同编号重放，即便字段不同也返回第一次结果
        replay=self.service.register_fire_segment(self.a["id"],
            seg_payload("S-1",9,99,gust=11),"p1",FC)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["segment"]["id"],first["segment"]["id"])
        self.assertEqual(replay["segment"]["end_marker"],5)
        self.assertEqual(len(self.repo.list_fire_segments(self.a["id"])),1)

    def test_adjacent_segments_merge_into_one(self):
        self.service.register_fire_segment(self.a["id"],seg_payload("S-1",0,5),"p1",FC)
        self.service.register_fire_segment(self.a["id"],seg_payload("S-2",5,9,gust=6),"p2",FC)
        view=self.service.list_firelines(FC,self.a["id"])
        self.assertEqual(view["segment_count"],2)
        self.assertEqual(len(view["merged_groups"]),1)
        group=view["merged_groups"][0]
        self.assertEqual((group["start_marker"],group["end_marker"]),(0,9))
        self.assertEqual(group["length"],9)
        self.assertTrue(group["merged"])
        self.assertEqual(group["site_codes"],["S-1","S-2"])
        self.assertEqual(group["gust_level"],6)
        # 列表中也能看出合并段数
        listing=self.service.list_firelines(FC)
        row=next(x for x in listing["firelines"] if x["item_id"]==self.a["id"])
        self.assertEqual(row["segment_count"],2)
        self.assertEqual(row["uncontrolled_length"],9)

    def test_overlapping_ranges_also_union(self):
        self.service.register_fire_segment(self.a["id"],seg_payload("S-1",0,8),"p1",FC)
        self.service.register_fire_segment(self.a["id"],seg_payload("S-2",4,10),"p2",FC)
        groups=self.service.list_firelines(FC,self.a["id"])["merged_groups"]
        self.assertEqual(len(groups),1)
        self.assertEqual((groups[0]["start_marker"],groups[0]["end_marker"]),(0,10))

    def test_site_code_taken_by_other_open_incident(self):
        self.service.register_fire_segment(self.a["id"],seg_payload("S-9",1,4),"p1",FC)
        with self.assertRaises(ConflictError) as ctx:
            self.service.register_fire_segment(self.b["id"],seg_payload("S-9",20,24),"p2",FC)
        self.assertEqual(ctx.exception.details["reason"],"site_code_taken")
        self.assertEqual(ctx.exception.details["owner_item_id"],self.a["id"])
        # 被退回，B事件没有片段
        self.assertEqual(self.service.list_firelines(FC,self.b["id"])["segment_count"],0)
        # 待核冲突可查
        conflicts=self.service.list_conflicts(IC)
        self.assertEqual(len(conflicts),1)
        self.assertEqual(conflicts[0]["status"],"pending")
        self.assertEqual(conflicts[0]["owner_item_id"],self.a["id"])
        # 事件列表能看出待核冲突数
        row=next(x for x in self.service.list_items("viewer") if x["id"]==self.b["id"])
        self.assertEqual(row["fireline"]["pending_conflicts"],1)

    def test_marker_overlap_with_other_open_incident(self):
        self.service.register_fire_segment(self.a["id"],seg_payload("S-1",10,20),"p1",FC)
        with self.assertRaises(ConflictError) as ctx:
            self.service.register_fire_segment(self.b["id"],seg_payload("T-1",15,25),"p2",FC)
        self.assertEqual(ctx.exception.details["reason"],"segment_overlap")
        self.assertEqual(ctx.exception.details["owner_range"],[10,20])
        self.assertEqual(len(self.service.list_conflicts(IC)),1)
        # 重复冲突不重复登记
        with self.assertRaises(ConflictError):
            self.service.register_fire_segment(self.b["id"],seg_payload("T-1",15,25),"p2",FC)
        self.assertEqual(len(self.service.list_conflicts(IC)),1)

    def test_conflict_allowed_after_owner_closed(self):
        self.service.register_fire_segment(self.a["id"],
            seg_payload("S-7",0,3,fire_status='controlled',gust=1),"p1",FC)
        self._close(self.a)
        result=self.service.register_fire_segment(self.b["id"],seg_payload("S-7",0,3),"p2",FC)
        self.assertFalse(result["replayed"])

    def test_burning_segments_block_close(self):
        self.service.register_fire_segment(self.a["id"],seg_payload("S-1",0,5),"p1",FC)
        current=self.service.get_item(self.a["id"],"viewer")
        for target in STATES[1:-1]:
            current=self.service.transition(current["id"],target,current["version"],"ic",
                                            TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"],STATES[-1],current["version"],"ic",IC)
        # 火势转为controlled后可以关闭
        segment=self.repo.list_fire_segments(self.a["id"])[0]
        self.service.update_fire_segment(self.a["id"],segment["id"],
            {"fire_status":"controlled","gust_level":2,
             "observed_at":"2026-09-26T12:00:00+00:00"},"p1",FC)
        current=self.service.get_item(self.a["id"],"viewer")
        current=self.service.transition(current["id"],STATES[-1],current["version"],"ic",IC)
        self.assertEqual(current["status"],"closed")

    def test_deadline_recalculated_from_uncontrolled_and_gust(self):
        self.service.register_fire_segment(self.a["id"],
            seg_payload("S-1",0,10,fire_status='controlled',gust=1),"p1",FC)
        view=self.service.list_firelines(FC,self.a["id"])
        self.assertEqual(view["uncontrolled_length"],0)
        before=view["fireline_deadline_hours"]
        self.service.register_fire_segment(self.a["id"],
            seg_payload("S-2",10,30,fire_status='burning',gust=10),"p2",FC)
        after=self.service.list_firelines(FC,self.a["id"])
        # 两段合并为0-30整段，任一部分燃烧则整段计入未控制长度
        self.assertEqual(after["uncontrolled_length"],30)
        self.assertEqual(after["gust_level"],10)
        self.assertLess(after["fireline_deadline_hours"],before)
        self.assertEqual(after["fireline_deadline_hours"],
                         fireline_deadline_hours(30,10))
        # 事件详情的顶层deadline也按归并结果重算
        detail=self.service.get_item(self.a["id"],"viewer")
        self.assertEqual(detail["deadline_hours"],after["fireline_deadline_hours"])

    def test_roles_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_fire_segment(self.a["id"],seg_payload("X",0,1),"p","viewer")
        with self.assertRaises(ValidationError):
            self.service.register_fire_segment(self.a["id"],
                seg_payload("S",5,1),"p",FC)
        with self.assertRaises(ValidationError):
            self.service.register_fire_segment(self.a["id"],
                {**seg_payload("S",0,1),"gust_level":13},"p",FC)
        with self.assertRaises(ValidationError):
            self.service.register_fire_segment(self.a["id"],
                {**seg_payload("S",0,1),"observed_at":"not-a-time"},"p",FC)
        with self.assertRaises(PermissionDenied):
            self.service.resolve_conflict(1,"p","viewer")

    def test_resolve_conflict_workflow(self):
        self.service.register_fire_segment(self.a["id"],seg_payload("S-1",0,5),"p1",FC)
        with self.assertRaises(ConflictError):
            self.service.register_fire_segment(self.b["id"],seg_payload("S-1",0,5),"p2",FC)
        conflict=self.service.list_conflicts(IC)[0]
        resolved=self.service.resolve_conflict(conflict["id"],"ic",IC)
        self.assertEqual(resolved["status"],"resolved")
        self.assertEqual(self.service.list_conflicts(IC,status="pending"),[])
        self.assertEqual(len(self.service.list_conflicts(IC,status="resolved")),1)

    def test_merge_rules_pure_function(self):
        segs=[
            {"site_code":"a","start_marker":0,"end_marker":3,"fire_status":"burning",
             "gust_level":2,"observed_at":"2026-09-26T10:00:00+00:00"},
            {"site_code":"b","start_marker":7,"end_marker":9,"fire_status":"controlled",
             "gust_level":1,"observed_at":"2026-09-26T09:00:00+00:00"},
            {"site_code":"c","start_marker":3,"end_marker":7,"fire_status":"controlled",
             "gust_level":5,"observed_at":"2026-09-26T11:00:00+00:00"},
        ]
        groups=merge_segments(segs)
        self.assertEqual(len(groups),1)
        g=groups[0]
        self.assertEqual(g["site_codes"],["a","c","b"])
        self.assertEqual((g["start_marker"],g["end_marker"]),(0,9))
        self.assertEqual(g["fire_status"],"burning")
        self.assertEqual(g["gust_level"],5)
        self.assertEqual(g["observed_at"],"2026-09-26T11:00:00+00:00")
        # 未控制长度越长、阵风越大，时限越短
        self.assertLess(fireline_deadline_hours(30,10),fireline_deadline_hours(5,2))
        self.assertEqual(fireline_deadline_hours(0,0),72)

if __name__=="__main__": unittest.main()
