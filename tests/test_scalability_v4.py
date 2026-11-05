import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.audit_v4_results import task_spec_mismatches
from src.benchmarks.rmt_fms_generator import generate_rmt_fms
from src.benchmarks.v5_families import FAMILY_SPECS, generate_v5_model
from src.model.validation import validate_model
from src.scg.builder import build_scg
from src.synchronizer.builder import build_synchronizer
from src.verifier import verify


BASE_PARAMS = {
    "n_machines": 4,
    "n_parts": 1,
    "ops_per_part": 4,
    "wip_bound": 1,
    "unobservable_ratio": 0.5,
    "fault_locations": 2,
    "interval_width_factor": 1.0,
    "adjustable_ratio": 0.25,
    "seed": 0,
    "n_configs": 1,
}


class ScalabilityV4GeneratorTests(unittest.TestCase):
    def test_v5_family_registry_builds_valid_distinct_models(self):
        hashes = set()
        for family in FAMILY_SPECS:
            tpn, L, H, attributes = generate_v5_model(family, seed=0)
            self.assertTrue(L > 0 and H > 0)
            self.assertEqual(attributes["benchmark_family"], family)
            self.assertFalse(validate_model(tpn))
            hashes.add((len(tpn.places), len(tpn.transitions), len(tpn.arcs),
                        tuple(sorted(tpn.observable_alphabet))))
        self.assertGreaterEqual(len(hashes), 10)

    def test_audit_rejects_record_that_differs_from_frozen_task(self):
        params = {"n_parts": 1, "seed": 0}
        expected = {"x": {
            "experiment": "Lsweep", "model": "L1", "seed": "0",
            "replicate": "0", "L": "8", "H": "60.0",
            "mode": "complete", "timeout_s": "60",
            "params": '{"n_parts": 1, "seed": 0}',
        }}
        record = {
            "task_id": "x", "experiment": "Lsweep", "model": "L1",
            "seed": 0, "replicate": 0, "L": 9, "H": 60.0,
            "mode": "complete", "timeout_s": 60, "params": params,
        }
        self.assertEqual(task_spec_mismatches(expected, [record]), ["x"])

    def test_optional_inspections_create_multiple_L_frontiers(self):
        tpn, _, _ = generate_rmt_fms(**BASE_PARAMS, inspection_locations=3)
        scg = build_scg(tpn).build_truncated()
        counts = [
            len(build_synchronizer(tpn, scg, L).candidate_words)
            for L in range(0, 16)
        ]
        frontiers = [
            L for L in range(1, len(counts)) if counts[L] > counts[L - 1]
        ]
        self.assertGreaterEqual(frontiers.__len__(), 3, counts)

    def test_zero_inspections_preserves_v3_model_shape(self):
        original, _, _ = generate_rmt_fms(**BASE_PARAMS)
        explicit_zero, _, _ = generate_rmt_fms(
            **BASE_PARAMS, inspection_locations=0
        )
        self.assertEqual(original.places, explicit_zero.places)
        self.assertEqual(original.transitions, explicit_zero.transitions)
        self.assertEqual(original.arcs, explicit_zero.arcs)
        self.assertEqual(original.initial_marking, explicit_zero.initial_marking)
        self.assertEqual(original.completion_markings, explicit_zero.completion_markings)

    def test_scg_limit_is_not_misreported_as_unreachable_completion(self):
        limited_scg = SimpleNamespace(
            classes={},
            edges=[],
            completion_ids=frozenset(),
            scg_state_limit_reached=True,
            has_zero_time_unobservable_cycle=False,
        )
        with patch("src.verifier.build_scg", return_value=limited_scg):
            result = verify(SimpleNamespace(), L=1, H=1.0)
        self.assertEqual(result["result"], "INC")
        self.assertEqual(result["inconclusive_reason"], "SCG_STATE_LIMIT")


if __name__ == "__main__":
    unittest.main()
