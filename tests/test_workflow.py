import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _advance_to_flushing(self, zones):
        item = self.service.create_item({
            "source_id": "SRC-1",
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": zones,
            "population": 5000,
            "complaints": 4,
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "verify", {"sample_count": 2}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"}, "disp-1", "dispatcher", item["version"])
        item = self.service.act(item["id"], "switch_source", {"alternate_source_id": "ALT-1"}, "coord-1", "coordinator", item["version"])
        return item

    def _flush_zone(self, item, zone_id):
        return self.service.act(item["id"], "flush", {"zone_id": zone_id}, "field-1", "field_operator", item["version"])

    def _disinfect(self, item, batch_id, zone_ids, completed_at="2026-09-27T10:00:00+00:00"):
        return self.service.act(item["id"], "disinfect", {
            "batch_id": batch_id, "zone_ids": zone_ids,
            "completed": True, "completed_at": completed_at,
        }, "field-1", "field_operator", item["version"])

    def _sample(self, item, sample_id, zone_id, batch_id, concentration, sampled_at="2026-09-27T11:00:00+00:00"):
        return self.service.act(item["id"], "sample", {
            "sample_id": sample_id, "zone_id": zone_id, "batch_id": batch_id,
            "concentration": concentration, "sampled_at": sampled_at,
        }, "lab-1", "lab", item["version"])

    def test_complete_water_response_workflow(self):
        item = self._advance_to_flushing(["Z-1"])
        item = self._flush_zone(item, "Z-1")
        item = self._disinfect(item, "B-1", ["Z-1"])
        item = self._sample(item, "S-1", "Z-1", "B-1", 2)
        item = self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")
        self.assertGreaterEqual(len(item["audit"]), 8)
        progress = {entry["zone_id"]: entry for entry in item["zone_progress"]}
        self.assertEqual(progress["Z-1"]["stage"], "restored")

    def test_restore_requires_every_zone_to_have_current_sample(self):
        item = self._advance_to_flushing(["Z-1", "Z-2"])
        item = self._flush_zone(item, "Z-1")
        item = self._flush_zone(item, "Z-2")
        # 批次只覆盖 Z-1，Z-2 也从未取得当前合格样本
        item = self._disinfect(item, "B-1", ["Z-1"])
        item = self._sample(item, "S-1", "Z-1", "B-1", 2)
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "zones_not_cleared")
        self.assertIn("Z-2", str(context.exception))
        progress = {entry["zone_id"]: entry for entry in item["zone_progress"]}
        self.assertIn("待消毒批次覆盖", progress["Z-2"]["missing_labels"])

        # 补齐 Z-2 后才能恢复
        item = self._disinfect(item, "B-2", ["Z-2"], "2026-09-27T12:00:00+00:00")
        item = self._sample(item, "S-2", "Z-2", "B-2", 3, "2026-09-27T13:00:00+00:00")
        item = self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_sample_must_reference_same_batch_and_be_after_disinfection(self):
        item = self._advance_to_flushing(["Z-1", "Z-2"])
        item = self._flush_zone(item, "Z-1")
        item = self._flush_zone(item, "Z-2")
        item = self._disinfect(item, "B-1", ["Z-1"])
        # Z-2 不在批次覆盖范围内
        with self.assertRaises(DomainError) as context:
            self._sample(item, "S-X", "Z-2", "B-1", 1)
        self.assertEqual(context.exception.code, "batch_zone_mismatch")
        # 采样时间早于消毒完成时间无效
        with self.assertRaises(DomainError) as context:
            self._sample(item, "S-X", "Z-1", "B-1", 1, "2026-09-27T09:00:00+00:00")
        self.assertEqual(context.exception.code, "sample_before_disinfection")
        # 引用不存在的批次
        with self.assertRaises(DomainError) as context:
            self._sample(item, "S-X", "Z-1", "B-NONE", 1)
        self.assertEqual(context.exception.code, "batch_not_found")

    def test_exceeded_sample_invalidates_basis_and_reopens_zone(self):
        item = self._advance_to_flushing(["Z-1"])
        item = self._flush_zone(item, "Z-1")
        item = self._disinfect(item, "B-1", ["Z-1"])
        item = self._sample(item, "S-1", "Z-1", "B-1", 2)
        item = self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")
        # 恢复后复检超限：原恢复依据作废，事件回到已采样
        item = self._sample(item, "S-2", "Z-1", "B-1", 20, "2026-09-28T09:00:00+00:00")
        self.assertEqual(item["status"], "sampled")
        progress = item["zone_progress"][0]
        self.assertEqual(progress["stage"], "flushed")
        self.assertFalse(progress["qualified_sample"])
        self.assertEqual(progress["basis"], 2)
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "zones_not_cleared")
        # 旧合格样本不再作为恢复依据；必须重新消毒再采样
        item = self._disinfect(item, "B-2", ["Z-1"], "2026-09-28T10:00:00+00:00")
        item = self._sample(item, "S-3", "Z-1", "B-2", 1, "2026-09-28T11:00:00+00:00")
        item = self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_reregistered_source_invalidates_zone_basis(self):
        item = self._advance_to_flushing(["Z-1"])
        item = self._flush_zone(item, "Z-1")
        item = self._disinfect(item, "B-1", ["Z-1"])
        item = self._sample(item, "S-1", "Z-1", "B-1", 2)
        item = self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        # 再次登记该区域的污染来源
        result = self.service.add_source(item["id"], {
            "source_type": "pipe",
            "external_id": "P-9",
            "observed_at": "2026-09-28T08:00:00+00:00",
            "concentration": 15,
            "zone_id": "Z-1",
        }, "field-2", "field_operator")
        self.assertEqual(result["basis_invalidated"]["zone_id"], "Z-1")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "sampled")
        self.assertEqual(item["payload"]["zones"]["Z-1"]["status"], "sampled")
        self.assertEqual(item["payload"]["zones"]["Z-1"]["basis"], 2)
        progress = item["zone_progress"][0]
        self.assertFalse(progress["qualified_sample"])
        # 其他区域/无关区域的来源登记不影响该区域
        result = self.service.add_source(item["id"], {
            "source_type": "tank",
            "external_id": "T-2",
            "observed_at": "2026-09-28T08:30:00+00:00",
        }, "field-2", "field_operator")
        self.assertIsNone(result["basis_invalidated"])

    def test_batch_can_cover_multiple_zones(self):
        item = self._advance_to_flushing(["Z-1", "Z-2"])
        item = self._flush_zone(item, "Z-1")
        item = self._flush_zone(item, "Z-2")
        item = self._disinfect(item, "B-1", ["Z-1", "Z-2"])
        item = self._sample(item, "S-1", "Z-1", "B-1", 1)
        item = self._sample(item, "S-2", "Z-2", "B-1", 2, "2026-09-27T11:30:00+00:00")
        item = self.service.act(item["id"], "restore", {}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")
        batch = item["payload"]["disinfection_batches"][0]
        self.assertEqual(batch["zone_ids"], ["Z-1", "Z-2"])


if __name__ == "__main__":
    unittest.main()
