import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.rules import assess
from src.domain import DomainError


class RuleTest(unittest.TestCase):
    def test_contamination_score_depends_on_ratio_and_population(self):
        critical = assess({"concentration": 50, "limit": 10, "population": 10000})
        low = assess({"concentration": 1, "limit": 10, "population": 100})
        self.assertEqual(critical["level"], "critical")
        self.assertEqual(low["level"], "low")
        self.assertGreater(critical["score"], low["score"])

    def test_restore_rejects_failed_sample(self):
        from src.rules import apply_action, init_zone_progress
        payload = init_zone_progress({
            "limit": 10,
            "zone_ids": ["Z-3"],
            "sample_results": [],
        })
        payload["zone_progress"]["Z-3"]["flushed"] = {"at": "2026-09-27T01:00:00+00:00", "seq": 1}
        payload["zone_progress"]["Z-3"]["disinfected"] = {"at": "2026-09-27T02:00:00+00:00", "batch_id": "B-1", "seq": 2}
        payload["zone_progress"]["Z-3"]["sampled"] = {
            "sample_id": "S-X", "concentration": 12, "batch_id": "B-1", "seq": 3,
        }
        item = {"status": "sampled", "payload": payload}
        with self.assertRaises(DomainError) as context:
            apply_action(item, "restore", {}, "c", "coordinator")
        self.assertEqual(context.exception.code, "zones_not_cleared")

    def test_restore_requires_every_zone_to_have_passing_sample(self):
        from src.rules import restore_readiness, init_zone_progress
        payload = init_zone_progress({"limit": 10, "zone_ids": ["Z-1", "Z-2"], "sample_results": []})
        ready, waiting, summaries = restore_readiness(payload)
        self.assertFalse(ready)
        self.assertEqual(set(waiting), {"Z-1", "Z-2"})
        for summary in summaries:
            self.assertEqual(summary["stage"], "pending_flush")
            self.assertEqual(summary["missing"], ["冲洗", "消毒", "采样"])


if __name__ == "__main__":
    unittest.main()
