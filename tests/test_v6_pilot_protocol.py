import unittest

from runner.run_cell import normalized_counters
from scripts.build_v6_pilot import build_tasks


class V6PilotProtocolTests(unittest.TestCase):
    def test_frozen_pilot_has_288_unique_execution_configurations(self):
        tasks = build_tasks()
        self.assertEqual(len(tasks), 288)
        self.assertEqual(len({task["configuration_id"] for task in tasks}), 288)
        self.assertEqual(len({task["expected_model_hash"] for task in tasks}), 232)
        self.assertEqual(len({task["bounded_problem_id"] for task in tasks}), 280)

    def test_scg_stage_stop_uses_null_for_unexecuted_later_work(self):
        counters = normalized_counters({
            "inconclusive_reason": "SCG_STATE_LIMIT",
            "stats": {
                "scg_classes": 50001, "scg_edges": 90000,
                "sync_states": 0, "candidate_words": 0,
                "temporal_states": 0, "trace_records": 0, "lp_calls": 0,
                "runtime_scg": 12.0, "runtime_sync": 0.0,
            },
        })
        self.assertEqual(counters["scg_classes"], 50001)
        self.assertIsNone(counters["sync_states"])
        self.assertIsNone(counters["candidate_words"])
        self.assertIsNone(counters["trace_records"])
        self.assertIsNone(counters["lp_calls"])


if __name__ == "__main__":
    unittest.main()
