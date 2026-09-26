import json, tempfile, threading, unittest
from http.client import HTTPConnection
from pathlib import Path
from http.server import ThreadingHTTPServer
from src.http_api import make_handler
from src.repository import Repository
from src.service import Service

class FirelineApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.repo=Repository(str(Path(cls.tmp.name)/"api.db"))
        cls.service=Service(cls.repo)
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),
            make_handler(cls.service,str(Path(__file__).resolve().parent.parent/"static")))
        cls.port=cls.server.server_address[1]
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.repo.close(); cls.tmp.cleanup()
    def _req(self,method,path,body=None,role='field_commander',actor='p1'):
        conn=HTTPConnection("127.0.0.1",self.port,timeout=5)
        data=json.dumps(body).encode() if body is not None else None
        headers={"X-Actor":actor,"X-Role":role}
        if data: headers["Content-Type"]="application/json"
        conn.request(method,path,data,headers)
        resp=conn.getresponse(); payload=json.loads(resp.read().decode())
        conn.close(); return resp.status,payload
    def _create(self,ref):
        status,body=self._req("POST","/api/items",
            {"title":ref,"description":"d","severity":"high","quantity":1,
             "threshold":10,"external_ref":ref})
        self.assertEqual(status,201); return body["id"]
    def test_register_replay_merge_conflict_and_list(self):
        a=self._create("API-A"); b=self._create("API-B")
        payload={"site_code":"F-1","start_marker":0,"end_marker":5,
                 "fire_status":"burning","gust_level":4,
                 "observed_at":"2026-09-26T10:00:00+00:00"}
        status,first=self._req("POST",f"/api/items/{a}/fire-segments",payload)
        self.assertEqual(status,201); self.assertFalse(first["replayed"])
        # 幂等重放
        status,replay=self._req("POST",f"/api/items/{a}/fire-segments",
            {**payload,"gust_level":11})
        self.assertEqual(status,201); self.assertTrue(replay["replay"] if "replay" in replay else replay["replayed"])
        self.assertEqual(replay["segment"]["gust_level"],4)
        # 相邻段合并
        status,_=self._req("POST",f"/api/items/{a}/fire-segments",
            {**payload,"site_code":"F-2","start_marker":5,"end_marker":9,"gust_level":7})
        self.assertEqual(status,201)
        status,view=self._req("GET",f"/api/items/{a}/fire-segments",role='viewer')
        self.assertEqual(status,200)
        self.assertEqual(len(view["merged_groups"]),1)
        self.assertEqual(view["merged_groups"][0]["site_codes"],["F-1","F-2"])
        self.assertEqual(view["gust_level"],7)
        # 跨事件冲突：409 + details（不同编号、界桩重叠）
        status,err=self._req("POST",f"/api/items/{b}/fire-segments",
            {**payload,"site_code":"G-9","start_marker":3,"end_marker":6})
        self.assertEqual(status,409)
        self.assertEqual(err["details"]["reason"],"segment_overlap")
        self.assertEqual(err["details"]["owner_item_id"],a)
        # 待核冲突列表
        status,conflicts=self._req("GET","/api/segment-conflicts?status=pending",role='incident_commander')
        self.assertEqual(status,200); self.assertEqual(len(conflicts["conflicts"]),1)
        cid=conflicts["conflicts"][0]["id"]
        # 事件列表显示合并段与待核冲突
        status,items=self._req("GET","/api/items",role='viewer')
        row=next(x for x in items["items"] if x["id"]==a)
        self.assertEqual(row["fireline"]["segment_count"],2)
        self.assertEqual(row["fireline"]["uncontrolled_length"],9)
        # A是被重叠的归属方，双方都能看到这一条待核冲突
        self.assertEqual(row["fireline"]["pending_conflicts"],1)
        # 燃烧段挡关闭：直接打到closed前的controlled→closed
        for target in ('active','contained','controlled'):
            status,body=self._req("GET",f"/api/items/{a}",role='viewer')
            version=body["version"]
            status,res=self._req("POST",f"/api/items/{a}/transition",
                {"target":target,"expected_version":version},role='incident_commander')
            self.assertEqual(status,200,res)
        status,body=self._req("GET",f"/api/items/{a}",role='viewer')
        status,res=self._req("POST",f"/api/items/{a}/transition",
            {"target":"closed","expected_version":body["version"]},role='incident_commander')
        self.assertEqual(status,409)
        self.assertIn("燃烧",res["message"])
        # 核销冲突
        status,res=self._req("PATCH",f"/api/segment-conflicts/{cid}",role='incident_commander')
        self.assertEqual(status,200); self.assertEqual(res["status"],"resolved")
        # 审计链完整
        self.assertTrue(self.repo.verify_audit_chain())

if __name__=="__main__": unittest.main()
