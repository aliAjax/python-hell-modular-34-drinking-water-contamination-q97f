import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _prepare_zones(self, item, zones, batch_id="B-1"):
        """对每个区域执行冲洗、消毒（同一批次覆盖全部区域）、合格采样。"""
        for zone in zones:
            item = self.service.act(item["id"], "flush", {"zone_id": zone}, "field-1", "field_operator", item["version"])
        item = self.service.act(
            item["id"], "disinfect",
            {"batch_id": batch_id, "zone_ids": zones, "completed": True},
            "field-1", "field_operator", item["version"],
        )
        for index, zone in enumerate(zones):
            item = self.service.act(
                item["id"], "sample",
                {"sample_id": "S-%s" % zone, "zone_id": zone, "concentration": 2},
                "lab-1", "lab", item["version"],
            )
        return item

    def test_complete_water_response_workflow(self):
        item = self.service.create_item({
            "source_id": "SRC-1",
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1", "Z-2"],
            "population": 5000,
            "complaints": 4,
        }, "analyst-1", "analyst")
        self.assertEqual(set(item["payload"]["zone_progress"]), {"Z-1", "Z-2"})
        item = self.service.act(item["id"], "verify", {"sample_count": 2}, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["payload"]["assessment"]["level"], "high")
        item = self.service.act(item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"}, "disp-1", "dispatcher", item["version"])
        item = self.service.act(item["id"], "switch_source", {"alternate_source_id": "ALT-1"}, "coord-1", "coordinator", item["version"])
        item = self._prepare_zones(item, ["Z-1", "Z-2"])

        zones = {z["zone_id"]: z for z in self.service.get_item(item["id"])["zones"]}
        self.assertTrue(all(z["ready_to_restore"] for z in zones.values()))
        self.assertTrue(self.service.get_item(item["id"])["restore_ready"])

        item = self.service.act(item["id"], "restore", {"note": "全部合格"}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")
        self.assertEqual({b["zone_id"] for b in item["payload"]["restoration"]["basis"]}, {"Z-1", "Z-2"})
        self.assertGreaterEqual(len(item["audit"]), 8)

    def test_cannot_restore_while_a_zone_has_no_new_sample(self):
        item = self.service.create_item({
            "source_id": "SRC-1",
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1", "Z-2"],
            "population": 5000,
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"}, "disp-1", "dispatcher", item["version"])
        # 只完成 Z-1，Z-2 完全没有复检
        item = self._prepare_zones(item, ["Z-1"])

        detail = self.service.get_item(item["id"])
        self.assertFalse(detail["restore_ready"])
        self.assertEqual(detail["zones_waiting"], ["Z-2"])
        z2 = next(z for z in detail["zones"] if z["zone_id"] == "Z-2")
        self.assertEqual(z2["stage"], "pending_flush")
        self.assertIn("冲洗", z2["missing"])

        from src.domain import DomainError
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "zones_not_cleared")

    def test_sample_must_match_disinfection_batch_and_follow_it(self):
        from src.domain import DomainError
        item = self.service.create_item({
            "source_id": "SRC-1",
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1"],
            "population": 100,
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"}, "disp-1", "dispatcher", item["version"])
        item = self.service.act(item["id"], "flush", {"zone_id": "Z-1"}, "field-1", "field_operator", item["version"])
        item = self.service.act(
            item["id"], "disinfect",
            {"batch_id": "B-9", "zone_ids": ["Z-1"], "completed": True},
            "field-1", "field_operator", item["version"],
        )
        disinfected_at = item["payload"]["zone_progress"]["Z-1"]["disinfected"]["at"]
        with self.assertRaises(DomainError) as context:
            self.service.act(
                item["id"], "sample",
                {"sample_id": "S-OLD", "zone_id": "Z-1", "concentration": 1,
                 "sampled_at": "2000-01-01T00:00:00+00:00"},
                "lab-1", "lab", item["version"],
            )
        self.assertEqual(context.exception.code, "sample_before_disinfection")

        item = self.service.act(
            item["id"], "sample",
            {"sample_id": "S-1", "zone_id": "Z-1", "concentration": 2, "sampled_at": disinfected_at},
            "lab-1", "lab", item["version"],
        )
        self.assertEqual(item["payload"]["sample_results"][0]["batch_id"], "B-9")

    def test_failed_sample_after_restore_reopens_to_sampled(self):
        from src.domain import DomainError
        item = self.service.create_item({
            "source_id": "SRC-1",
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1", "Z-2"],
            "population": 100,
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"}, "d", "dispatcher", item["version"])
        item = self._prepare_zones(item, ["Z-1", "Z-2"])
        item = self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

        # Z-1 复检超限：原恢复依据作废，事件回到已采样
        item = self.service.act(
            item["id"], "sample",
            {"sample_id": "S-FAIL", "zone_id": "Z-1", "concentration": 99},
            "lab-1", "lab", item["version"],
        )
        self.assertEqual(item["status"], "sampled")
        self.assertNotIn("restoration", item["payload"])
        self.assertEqual(len(item["payload"]["restoration_history"]), 1)
        self.assertTrue(
            any(e["event_type"] == "restoration_invalidated" for e in item["audit"]),
            "复检超限时应留下恢复依据作废审计",
        )
        detail = self.service.get_item(item["id"])
        z1 = next(z for z in detail["zones"] if z["zone_id"] == "Z-1")
        self.assertFalse(z1["ready_to_restore"])
        self.assertIn("合格复检样本", z1["missing"])
        z2 = next(z for z in detail["zones"] if z["zone_id"] == "Z-2")
        self.assertTrue(z2["ready_to_restore"])
        self.assertFalse(detail["restore_ready"])
        with self.assertRaises(DomainError):
            self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])

        # Z-1 同批次再次采样合格后，无需重做 Z-2 即可恢复
        item = self.service.act(
            item["id"], "sample",
            {"sample_id": "S-OK", "zone_id": "Z-1", "concentration": 1},
            "lab-1", "lab", item["version"],
        )
        self.assertTrue(self.service.get_item(item["id"])["restore_ready"])
        item = self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_new_source_after_restore_voids_that_zone(self):
        from src.domain import DomainError
        item = self.service.create_item({
            "source_id": "SRC-1",
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1", "Z-2"],
            "population": 100,
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"}, "d", "dispatcher", item["version"])
        item = self._prepare_zones(item, ["Z-1", "Z-2"])
        item = self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        restored_version = item["version"]

        # 再次登记 Z-1 的污染来源
        source = self.service.add_source(item["id"], {
            "source_type": "pipe_break",
            "external_id": "PB-77",
            "observed_at": "2026-09-28T00:00:00+00:00",
            "zone_id": "Z-1",
            "concentration": 40,
        }, "field-1", "field_operator")
        self.assertIsNotNone(source["invalidation"])

        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "sampled")
        self.assertEqual(item["version"], restored_version + 1)
        self.assertNotIn("restoration", item["payload"])
        self.assertEqual(len(item["payload"]["restoration_history"]), 1)
        z1 = next(z for z in item["zones"] if z["zone_id"] == "Z-1")
        self.assertEqual(z1["stage"], "sampled")
        self.assertIsNone(z1["sample"])  # 旧样本不再代表当前水质
        self.assertTrue(any("重新采样" in m for m in z1["missing"]))
        self.assertFalse(item["restore_ready"])
        with self.assertRaises(DomainError):
            self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])

        # Z-1 重新采样合格（消毒批次仍有效），Z-2 依据不受影响
        item = self.service.act(
            item["id"], "sample",
            {"sample_id": "S-RE", "zone_id": "Z-1", "concentration": 1},
            "lab-1", "lab", item["version"],
        )
        item = self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")


if __name__ == "__main__":
    unittest.main()
