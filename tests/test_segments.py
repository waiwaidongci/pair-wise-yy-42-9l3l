import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES, segment_deadline_hours


def segment_payload(field_ref, start, end, fire_status="burning", gust=5,
                    observed="2026-09-26T08:00:00+00:00"):
    return {"field_ref": field_ref, "start_marker": start, "end_marker": end,
            "fire_status": fire_status, "gust_level": gust, "observed_at": observed}


class SegmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "东线火场", "description": " patrol merge", "severity": "high",
             "quantity": 10, "threshold": 5, "external_ref": "SEG-1"},
            "creator", "field_commander")

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def test_replay_returns_first_result(self):
        payload = segment_payload("FS-1", "JZ-10", "JZ-14")
        first = self.service.register_segment(self.item["id"], payload, "patrol-a", "field_commander")
        replayed = self.service.register_segment(
            self.item["id"], dict(payload, gust_level=9), "patrol-b", "logistics")
        self.assertEqual(first, replayed)
        listing = self.service.list_segments(self.item["id"], "viewer")
        self.assertEqual(len(listing["segments"]), 1)
        self.assertEqual(listing["conflicts"], [])

    def test_adjacent_segments_merge_and_bridge(self):
        self.service.register_segment(self.item["id"], segment_payload("FS-1", "JZ-10", "JZ-14"),
                                      "patrol-a", "field_commander")
        self.service.register_segment(self.item["id"], segment_payload("FS-2", "JZ-18", "JZ-22"),
                                      "patrol-b", "field_commander")
        merged = self.service.register_segment(
            self.item["id"], segment_payload("FS-3", "JZ-14", "JZ-18", gust=7),
            "patrol-c", "field_commander")
        self.assertTrue(merged["merged"])
        self.assertEqual((merged["start_marker"], merged["end_marker"]), ("JZ-10", "JZ-22"))
        self.assertEqual(merged["field_refs"], ["FS-1", "FS-2", "FS-3"])
        self.assertEqual(merged["length"], 12)
        listing = self.service.list_segments(self.item["id"], "viewer")
        self.assertEqual(len(listing["segments"]), 1)
        self.assertTrue(listing["segments"][0]["merged"])

    def test_cross_item_overlap_rejected_and_listed(self):
        other = self.service.create_item(
            {"title": "西线火场", "description": "other fire", "severity": "moderate",
             "quantity": 3, "threshold": 5, "external_ref": "SEG-2"},
            "creator", "field_commander")
        self.service.register_segment(self.item["id"], segment_payload("FS-1", "JZ-10", "JZ-14"),
                                      "patrol-a", "field_commander")
        with self.assertRaises(ConflictError) as ctx:
            self.service.register_segment(other["id"], segment_payload("FS-9", "JZ-12", "JZ-16"),
                                          "patrol-c", "field_commander")
        self.assertIn(str(self.item["id"]), ctx.exception.message)
        self.assertIn("东线火场", ctx.exception.message)
        listing = self.service.list_segments(other["id"], "viewer")
        self.assertEqual(listing["segments"], [])
        self.assertEqual(len(listing["conflicts"]), 1)
        conflict = listing["conflicts"][0]
        self.assertEqual(conflict["status"], "pending")
        self.assertEqual(conflict["conflicting_item_id"], self.item["id"])
        visible = self.service.list_segments(self.item["id"], "viewer")
        self.assertEqual(len(visible["conflicts"]), 1)

    def test_burning_segment_blocks_closure_until_controlled(self):
        self.service.register_segment(self.item["id"], segment_payload("FS-1", "JZ-10", "JZ-14"),
                                      "patrol-a", "field_commander")
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        self.assertIn("火线片段", ctx.exception.message)
        self.service.register_segment(
            self.item["id"],
            segment_payload("FS-2", "JZ-10", "JZ-14", fire_status="contained", gust=2,
                            observed="2026-09-26T10:00:00+00:00"),
            "patrol-a", "field_commander")
        current = self.service.get_item(self.item["id"], "viewer")
        closed = self.service.transition(current["id"], STATES[-1], current["version"],
                                         "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"], STATES[-1])
        with self.assertRaises(ConflictError):
            self.service.register_segment(self.item["id"], segment_payload("FS-3", "JZ-1", "JZ-2"),
                                          "patrol-a", "field_commander")

    def test_deadline_recomputed_from_uncontrolled_length_and_gust(self):
        base = self.service.get_item(self.item["id"], "viewer")["deadline_hours"]
        self.service.register_segment(self.item["id"], segment_payload("FS-1", "JZ-10", "JZ-20", gust=8),
                                      "patrol-a", "field_commander")
        enriched = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(enriched["uncontrolled_length"], 10)
        self.assertEqual(enriched["max_gust_level"], 8)
        self.assertEqual(enriched["open_segment_count"], 1)
        self.assertEqual(enriched["deadline_hours"], segment_deadline_hours("high", 10, 8))
        self.assertLess(enriched["deadline_hours"], base)

    def test_segment_validation_and_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_segment(self.item["id"], segment_payload("FS-1", "JZ-1", "JZ-2"),
                                          "intruder", "viewer")
        with self.assertRaises(ValidationError):
            self.service.register_segment(self.item["id"], segment_payload("FS-1", "JZ-5", "JZ-2"),
                                          "patrol-a", "field_commander")
        with self.assertRaises(ValidationError):
            self.service.register_segment(self.item["id"], segment_payload("FS-1", "JZ-1", "JZ-2", fire_status="unknown"),
                                          "patrol-a", "field_commander")
        with self.assertRaises(ValidationError):
            self.service.register_segment(self.item["id"], segment_payload("FS-1", "JZ-1", "JZ-2", observed="not-a-time"),
                                          "patrol-a", "field_commander")


if __name__ == "__main__":
    unittest.main()
