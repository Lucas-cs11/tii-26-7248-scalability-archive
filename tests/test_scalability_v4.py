import unittest

from src.benchmarks.rmt_fms_generator import generate_rmt_fms
from src.scg.builder import build_scg
from src.synchronizer.builder import build_synchronizer


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


if __name__ == "__main__":
    unittest.main()
