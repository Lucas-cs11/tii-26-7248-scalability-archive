import unittest
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from scripts.audit_v4_results import task_spec_mismatches
from scripts.audit_v5_results import timeout_semantics_valid
from scripts.validate_v5_formal_protocol import prove_generated_semantics
from runner.run_cell import model_hash
from runner.shard_state import partition_existing_records
from src.benchmarks.rmt_fms_generator import generate_rmt_fms
from src.benchmarks.v5_families import FAMILY_SPECS, generate_v5_model
from src.model.validation import validate_model
from src.model.tpn import TPN
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
    def test_model_hash_includes_adjustment_semantics(self):
        tpn, _, _, _ = generate_v5_model("F02_sequential_dense", seed=0)
        name = next(name for name, transition in tpn.transitions.items()
                    if transition.is_adjustable)
        changed = dict(tpn.transitions)
        changed[name] = replace(changed[name], is_adjustable=False)
        altered = TPN(
            places=dict(tpn.places), transitions=changed, arcs=list(tpn.arcs),
            initial_marking=dict(tpn.initial_marking),
            completion_markings=[dict(marking) for marking in tpn.completion_markings],
        )
        self.assertNotEqual(model_hash(tpn), model_hash(altered))

    def test_v5_constructive_witness_reaches_wip_fault_cycle_and_completion(self):
        tpn, _, _, attributes = generate_v5_model(
            "F11_resource_cycle", seed=17, n_parts=3, wip_bound=3,
        )
        witnesses = prove_generated_semantics(tpn, attributes)
        self.assertTrue(witnesses["completion_reachable"])
        self.assertEqual(witnesses["reachable_concurrency"], 3)
        self.assertGreater(witnesses["fault_transitions_witnessed"], 0)
        self.assertGreater(witnesses["rework_cycles_witnessed"], 0)

    def test_timeout_requires_truthful_partial_workload_and_stage(self):
        record = {
            "verdict": None, "analysis_status": "NOT_RUN", "runtime_s": None,
            "termination_status": "TIMEOUT_WALL_CLOCK",
            "failure_stage": "SCG", "counters": {"scg_classes": 19},
            "partial_workload": True, "validation_status": "PASS",
        }
        self.assertTrue(timeout_semantics_valid(record))
        record["partial_workload"] = False
        self.assertFalse(timeout_semantics_valid(record))
        generation_timeout = {
            "verdict": None, "analysis_status": "NOT_RUN", "runtime_s": None,
            "termination_status": "TIMEOUT_WALL_CLOCK",
            "failure_stage": "MODEL_GENERATION", "counters": None,
            "partial_workload": False, "validation_status": "NOT_RUN",
        }
        self.assertTrue(timeout_semantics_valid(generation_timeout))

    def test_retry_reruns_error_but_preserves_completed_and_timeout(self):
        lines = [
            json.dumps({"task_id": "ok", "execution_status": "COMPLETED"}),
            json.dumps({"task_id": "timeout", "execution_status": "TIMEOUT_WALL_CLOCK"}),
            json.dumps({"task_id": "error", "execution_status": "ERROR"}),
            json.dumps({"task_id": "invalid", "execution_status": "VALIDATION_FAILURE"}),
        ]
        final, superseded = partition_existing_records(lines)
        self.assertEqual({record["task_id"] for record in final}, {"ok", "timeout"})
        self.assertEqual(
            {record["task_id"] for record in superseded}, {"error", "invalid"}
        )

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
