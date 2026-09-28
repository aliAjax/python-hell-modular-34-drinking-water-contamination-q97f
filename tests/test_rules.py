import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.rules import assess, apply_action, initial_zones, zone_progress
from src.domain import DomainError


class RuleTest(unittest.TestCase):
    def test_contamination_score_depends_on_ratio_and_population(self):
        critical = assess({"concentration": 50, "limit": 10, "population": 10000})
        low = assess({"concentration": 1, "limit": 10, "population": 100})
        self.assertEqual(critical["level"], "critical")
        self.assertEqual(low["level"], "low")
        self.assertGreater(critical["score"], low["score"])

    def _sampled_item(self, zones_payload, limit=10):
        return {"status": "sampled", "payload": zones_payload}

    def test_restore_rejects_when_a_zone_has_no_current_sample(self):
        payload = {
            "limit": 10,
            "zone_ids": ["Z-1"],
            "zones": initial_zones(["Z-1"]),
            "sample_results": [],
        }
        with self.assertRaises(DomainError) as context:
            apply_action({"status": "sampled", "payload": payload}, "restore", {}, "c", "coordinator")
        self.assertEqual(context.exception.code, "zones_not_cleared")
        self.assertIn("Z-1", str(context.exception))

    def test_restore_accepts_current_qualified_sample(self):
        payload = {
            "limit": 10,
            "zone_ids": ["Z-1"],
            "zones": {"Z-1": {"status": "sampled", "basis": 1, "flushed": True}},
            "disinfection_batches": [{
                "batch_id": "B-1",
                "zone_ids": ["Z-1"],
                "completed_at": "2026-09-27T10:00:00+00:00",
                "basis_by_zone": {"Z-1": 1},
            }],
            "sample_results": [{
                "sample_id": "S-1",
                "zone_id": "Z-1",
                "batch_id": "B-1",
                "concentration": 2,
                "sampled_at": "2026-09-27T11:00:00+00:00",
                "basis": 1,
                "qualified": True,
                "current": True,
            }],
        }
        status, new_payload, _ = apply_action(
            {"status": "sampled", "payload": payload}, "restore", {}, "c", "coordinator"
        )
        self.assertEqual(status, "restored")
        self.assertEqual(new_payload["zones"]["Z-1"]["status"], "restored")
        self.assertEqual(zone_progress(new_payload)[0]["stage"], "restored")

    def test_stale_sample_from_previous_basis_does_not_clear_zone(self):
        # basis 已推进到 2，样本还挂在 basis 1 的旧批次上
        payload = {
            "limit": 10,
            "zone_ids": ["Z-1"],
            "zones": {"Z-1": {"status": "disinfected", "basis": 2, "flushed": True}},
            "disinfection_batches": [
                {
                    "batch_id": "B-1",
                    "zone_ids": ["Z-1"],
                    "completed_at": "2026-09-27T10:00:00+00:00",
                    "basis_by_zone": {"Z-1": 1},
                },
                {
                    "batch_id": "B-2",
                    "zone_ids": ["Z-1"],
                    "completed_at": "2026-09-28T10:00:00+00:00",
                    "basis_by_zone": {"Z-1": 2},
                },
            ],
            "sample_results": [{
                "sample_id": "S-1",
                "zone_id": "Z-1",
                "batch_id": "B-1",
                "concentration": 2,
                "sampled_at": "2026-09-27T11:00:00+00:00",
                "basis": 1,
                "qualified": True,
                "current": False,
            }],
        }
        progress = zone_progress(payload)[0]
        self.assertFalse(progress["qualified_sample"])
        self.assertIn("待消毒后合格样本", progress["missing_labels"])


if __name__ == "__main__":
    unittest.main()
