#!/usr/bin/env python3
"""Build and freeze the v6 performance differential protocol v2.

The protocol is held as a single Python structure so that the human-readable
YAML and the machine-readable JSON mirror can never drift.  Freezing means:
write both files, print their SHA-256, and commit them *before* the formal v2
verification run.  The frozen v1 protocol
(``protocol/v6_perf_differential_1.json``) is never modified.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YAML_OUT = ROOT / "protocol" / "v6-performance-differential-2.yaml"
JSON_OUT = ROOT / "protocol" / "v6-performance-differential-2.json"

BASELINE_COMMIT = "10d27726c085076fe790d574eabd15d7423ac86d"
OPTIMIZED_COMMIT = "215443219c524878f7af4cef0e4b8d8caf790531"
TOOLING_COMMIT_AT_FREEZE = "ae9ecd9f990179d8373d30b7ab69624e80c84558"

PROTOCOL = {
    "protocol_version": "v6-performance-differential-2",
    "frozen_before_execution": True,
    "supersedes": "v6-performance-differential-1 (unchanged, its 18 censored pairs stay on record)",
    "baseline_source_commit": BASELINE_COMMIT,
    "optimized_source_commit": OPTIMIZED_COMMIT,
    "tooling_commit_at_freeze": TOOLING_COMMIT_AT_FREEZE,
    "semantics_version": "v6-strong-time-1",
    "serialization_version": "json-fraction-v2",
    "python": "3.13",
    "oracle": {
        "fraction_timed_semantics": "src/semantics/fraction_oracle.py (15 independent language tests)",
        "wang_tac_2015": "tests/test_scalability_v4.py regression",
        "tina_scg": "tests/test_strong_time_scg.py A--F regressions",
        "industrial_17p_15t": "work/v6_freeze baseline fingerprints (read-only reference)",
        "family_regressions": "scripts/validate_v6_model_semantics.py",
    },
    "layer_a": {
        "purpose": "complete end-to-end equivalence for configurations both versions can finish",
        "inherited_complete_pairs": {
            "source": "GitHub Actions runs 42348140150 (baseline) and 42348132809 (optimized), frozen 288-cell Pilot",
            "pairs": 126,
            "rule": "every task completed by both versions; compare all comparison fields",
        },
        "new_cells": {
            "source": "protocol/v6_perf_differential_1.json (48 structural cells)",
            "conclusive_pairs_expected": 30,
            "cell_timeout_seconds": 60,
            "layer_b_timeout_seconds": 300,
        },
        "coverage_requirements": {
            "families": 12,
            "modes": ["first", "complete"],
            "structural_sizes": "A1/A2/A3 series levels",
            "wip": [1, 2, 3],
            "l_sweep": "consecutive L levels with fixed model and H",
            "difficult": "D family levels; boundary reported even when inconclusive",
            "lp_boundary_equality": "included through complete-mode and half-integer interval cases",
            "unobservable_faults": "all families carry unobservable fault transitions",
            "cycles": "F10/F11/F12 rework and cycle families",
            "resource_contention": "F07 shared contention",
        },
        "comparison_fields": [
            "verdict", "inconclusive_reason", "certificate_mode",
            "enumeration_complete", "critical_pair_canonical_set",
            "critical_pair_count", "model_hash", "scg_state_limit_reached",
            "zero_time_unobservable_cycle", "counters.scg_classes",
            "counters.scg_edges", "counters.sync_states", "counters.candidate_words",
            "counters.candidate_words_processed", "counters.temporal_states",
            "counters.trace_records", "counters.terminal_pairs",
            "counters.enclosure_rejected_pairs", "counters.lp_calls",
        ],
        "first_certificate_rule": (
            "a legal first certificate may differ if search order is unaffected by the "
            "memoisation; both certificates must be verified legal and the verdict must match"
        ),
        "complete_set_rule": (
            "the full canonical critical-pair set must match, not only its cardinality"
        ),
        "runtime_fields_excluded": True,
    },
    "layer_b": {
        "purpose": "transition-by-transition equivalence of the memoised synchronizer",
        "scope_cells": "all 48 cells of v6_perf_differential_1 plus every cell censored there",
        "procedure": [
            "build the synchronizer with expose_transitions=True",
            "for every reachable SyncState and every observable label, recompute the transition without the memo cache",
            "compare X_N/X_F canonical sets, state identity and empty-successor handling with the memoised value",
            "rebuild the reachable graph, edge multiset and ordered candidate words from fresh transitions",
            "compare with the automaton produced by build_synchronizer",
            "for cells that fit the budget, additionally compare build_synchronizer(use_transition_memo=False) with True",
        ],
        "coverage_rule": "every legal successor label, including labels with empty successors; never only cache hits",
        "acceptance": (
            "zero transition mismatches and exact graph/candidate-word reconstruction; cells where "
            "the 300 s budget is exceeded are reported as censored and cannot count as PASS"
        ),
        "censored_rule": (
            "a pair that cannot be completed on both sides is never a pass; the evidence level "
            "(full graph vs. transition-level within the optimized tree) is recorded explicitly"
        ),
    },
    "layer_c": {
        "purpose": "per-optimization component audit with independent evidence",
        "components": {
            "scg_adjacency_index": [
                "adjacency equals the original edge-list projection per class",
                "edge multiplicity and transition labels preserved",
                "empty successor lists handled",
                "index is not stale for any queried graph",
            ],
            "synchronizer_memo": [
                "key contains every input that determines the successor (X_N, X_F, label)",
                "immutable frozenset keys only",
                "no reuse across different fault classes",
                "empty successors are memoised too",
                "memo cannot change the reachable graph",
            ],
            "marking_cache": [
                "enabled/fire/persistent/newly-enabled depend only on (marking, transition) and are equivalent to tpn methods",
                "no DBM/clock-domain data is cached under a marking-only key",
                "no cross-model reuse",
            ],
            "dbm_fraction": [
                "half-integer rationals, arbitrary denominators, negatives, zero, infinity",
                "boundary equality and epsilon-tight constraints",
                "empty zone detection, canonicalisation, persistent projection, newly-enabled reset",
            ],
            "trace_record_dedup": [
                "identical sigma deduplicated",
                "different fault histories never merged",
                "different time feasibility never merged",
                "COMPLETE_SET critical pairs preserved",
                "first-entry completion preserved",
            ],
        },
        "acceptance": "zero component mismatches; tests executed, not asserted by prose",
    },
    "acceptance_gates": [
        "DIFFERENTIAL_V2_COMPONENT_PASS",
        "DIFFERENTIAL_V2_COMPLETE_CASES_PASS",
        "DIFFERENTIAL_V2_ORACLE_PASS",
        "DIFFERENTIAL_V2_CENSORING_ACCOUNTED",
        "DIFFERENTIAL_V2_NO_OBSERVED_MISMATCH",
    ],
    "composite_gate": "DIFFERENTIAL_V2_ACCEPTED",
    "composite_rule": (
        "all component gates true; no observed diagnostic or critical-pair mismatch; "
        "censored cells kept in the denominator and reported; every optimization rule used by "
        "the production verifier covered by component or transition evidence"
    ),
    "forbidden": [
        "removing unfinished tasks from the denominator",
        "declaring a censored pair as agreement",
        "editing v6-performance-differential-1",
        "post-hoc selection of easy cells to replace difficult ones",
    ],
    "evidence_paths": {
        "component": "work/v6_differential_v2_component/",
        "v1_differential": "work/v6_perf_differential_1/",
        "pilot_runs": ["work/v6_baseline_pilot_run_42348140150", "work/v6_optimized_pilot_run_42348132809"],
        "scg_equivalence": "work/v6_scg_equivalence_1/",
        "sync_equivalence_attempt": "work/v6_sync_equivalence_1/",
        "oracle": "tests/test_fraction_oracle.py, tests/test_strong_time_scg.py, scripts/validate_v6_model_semantics.py",
    },
}


def _yaml_dump(value, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_yaml_dump(item, indent + 1))
            elif isinstance(item, list) and not item:
                lines.append(f"{pad}{key}: []")
            elif isinstance(item, dict) and not item:
                lines.append(f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                rendered = _yaml_dump(item, indent + 1)
                lines.append(f"{pad}-")
                lines.append(rendered)
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_scalar(value)}"


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if any(ch in text for ch in ":#[]{}&*!|>'\"%@`") or text.strip() != text:
        return json.dumps(text)
    return text


def main() -> int:
    payload = json.dumps(PROTOCOL, indent=2, sort_keys=True) + "\n"
    JSON_OUT.write_text(payload)
    header = (
        "# IEEE TII Scalability v6 - performance differential protocol v2\n"
        "# Frozen structure mirrored in protocol/v6-performance-differential-2.json\n"
        "# Generated by scripts/build_v6_differential_v2_protocol.py; do not hand-edit.\n"
    )
    YAML_OUT.write_text(header + _yaml_dump(PROTOCOL) + "\n")
    print(json.dumps({
        "yaml": str(YAML_OUT.relative_to(ROOT)),
        "json": str(JSON_OUT.relative_to(ROOT)),
        "json_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "yaml_sha256": hashlib.sha256(YAML_OUT.read_bytes()).hexdigest(),
        "gates": PROTOCOL["acceptance_gates"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
