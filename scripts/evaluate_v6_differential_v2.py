#!/usr/bin/env python3
"""Evaluate the frozen differential v2 protocol.

Reads only independently produced artifacts:
* the frozen protocol mirror ``protocol/v6-performance-differential-2.json``
* Layer B transition-equivalence evidence
* Layer C component-audit evidence
* the v1 differential results (30 conclusive pairs)
* the two 288-cell Pilot runs (126 pairs completed on both sides)
* the 12-family semantic validation and the Fraction oracle results

It recomputes the cross-version Pilot agreement itself instead of trusting a
summary file, and keeps every censored cell in the denominator.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path):
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def load_raw(artifacts: Path):
    records = {}
    for path in sorted(artifacts.glob("*/raw/shard_*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                records[record.get("task_id")] = record
    return records


COUNTER_FIELDS = (
    "scg_classes", "scg_edges", "sync_states", "candidate_words",
    "candidate_words_processed", "temporal_states", "trace_records",
    "terminal_pairs", "lp_calls",
)


def pilot_agreement(baseline_dir: Path, optimized_dir: Path):
    baseline = load_raw(baseline_dir)
    optimized = load_raw(optimized_dir)
    shared = sorted(set(baseline) & set(optimized))
    completed_both = [
        task_id for task_id in shared
        if baseline[task_id].get("execution_status") == "COMPLETED"
        and optimized[task_id].get("execution_status") == "COMPLETED"
    ]
    verdict_mismatches = []
    counter_mismatches = []
    for task_id in completed_both:
        left, right = baseline[task_id], optimized[task_id]
        if (left.get("verdict"), left.get("inconclusive_reason")) != (
            right.get("verdict"), right.get("inconclusive_reason")
        ):
            verdict_mismatches.append(task_id)
        left_counters = left.get("counters") or {}
        right_counters = right.get("counters") or {}
        for field in COUNTER_FIELDS:
            if left_counters.get(field) is None or right_counters.get(field) is None:
                continue
            if left_counters.get(field) != right_counters.get(field):
                counter_mismatches.append({"task_id": task_id, "field": field,
                                           "baseline": left_counters.get(field),
                                           "optimized": right_counters.get(field)})
                break
    flips = [
        task_id for task_id in shared
        if baseline[task_id].get("execution_status") == "TIMEOUT_WALL_CLOCK"
        and optimized[task_id].get("execution_status") == "COMPLETED"
    ]
    reverse_regressions = [
        task_id for task_id in shared
        if baseline[task_id].get("execution_status") == "COMPLETED"
        and optimized[task_id].get("execution_status") == "TIMEOUT_WALL_CLOCK"
    ]
    new_verdicts = {
        "conclusive_ver_amb": sum(
            1 for task_id in flips
            if optimized[task_id].get("verdict") in ("VER", "AMB")
        ),
        "inc": sum(1 for task_id in flips if optimized[task_id].get("verdict") == "INC"),
    }
    return {
        "tasks": len(shared),
        "completed_both": len(completed_both),
        "verdict_mismatch_count": len(verdict_mismatches),
        "counter_mismatch_count": len(counter_mismatches),
        "verdict_mismatches": verdict_mismatches,
        "counter_mismatches": counter_mismatches,
        "timeout_to_completed": len(flips),
        "completed_to_timeout": len(reverse_regressions),
        "new_verdicts": new_verdicts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path,
                        default=ROOT / "protocol/v6-performance-differential-2.json")
    parser.add_argument("--component-dir", type=Path,
                        default=ROOT / "work/v6_differential_v2_component")
    parser.add_argument("--v1-differential", type=Path,
                        default=ROOT / "work/v6_perf_differential_1/differential_results.json")
    parser.add_argument("--baseline-pilot", type=Path,
                        default=ROOT / "work/v6_baseline_pilot_run_42348140150")
    parser.add_argument("--optimized-pilot", type=Path,
                        default=ROOT / "work/v6_optimized_pilot_run_42348132809")
    parser.add_argument("--semantics", type=Path,
                        default=ROOT / "work/v6_optimized_semantics_validation/generator_regression_results.json")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "work/v6_differential_v2_acceptance")
    args = parser.parse_args()
    protocol = load(args.protocol) or {}
    transition = load(args.component_dir / "sync_transition_equivalence.json") or {}
    components = load(args.component_dir / "component_equivalence.json") or {}
    v1 = load(args.v1_differential) or {}
    semantics = load(args.semantics) or {}
    agreement = pilot_agreement(args.baseline_pilot, args.optimized_pilot)
    args.out.mkdir(parents=True, exist_ok=True)

    component_gates = {
        name: bool((components.get("components") or {}).get(name, {}).get("PASS"))
        for name in ("scg_adjacency_index", "marking_cache", "trace_record_dedup",
                     "dbm_fraction", "oracle")
    }
    component_pass = bool(components.get("PASS")) and all(component_gates.values())

    v1_matched = int(v1.get("matched") or 0)
    v1_mismatched = int(v1.get("mismatched") or 0)
    v1_censored = int(v1.get("censored") or 0)
    v1_censoring = v1.get("censoring_breakdown") or {}
    transition_pass = bool(transition.get("PASS"))
    transition_cells = int(transition.get("cells_checked") or 0)
    transition_expected = int(
        ((protocol.get("layer_b") or {}).get("expected_cells"))
        or (v1_matched + v1_censored)
    )

    complete_cases_pass = (
        v1_mismatched == 0
        and v1_matched == 30
        and transition_pass
        and transition_cells >= transition_expected
        and transition.get("mismatch_count", 0) == 0
        and transition.get("graph_reconstruction_pass") is True
        and agreement["completed_both"] == 126
        and agreement["verdict_mismatch_count"] == 0
        and agreement["counter_mismatch_count"] == 0
        and agreement["completed_to_timeout"] == 0
    )
    oracle_pass = (
        bool(semantics.get("PASS"))
        and bool((components.get("components") or {}).get("oracle", {}).get("PASS"))
    )
    censoring_accounted = (
        v1_censored == 18
        and sum(v1_censoring.get(key, 0) for key in
                ("both_censored", "baseline_censored_optimized_completed",
                 "optimized_censored_baseline_completed")) == v1_censored
        and len(v1.get("censored_detail") or []) == v1_censored
        and all(entry.get("censoring") for entry in (v1.get("censored_detail") or []))
        and "censored" in json.dumps(transition).lower()
    )
    no_observed_mismatch = (
        v1_mismatched == 0
        and int(transition.get("mismatch_count") or 0) == 0
        and int(components.get("mismatch_count") or 0) == 0
        and agreement["verdict_mismatch_count"] == 0
        and agreement["counter_mismatch_count"] == 0
    )
    gates = {
        "DIFFERENTIAL_V2_COMPONENT_PASS": component_pass,
        "DIFFERENTIAL_V2_COMPLETE_CASES_PASS": complete_cases_pass,
        "DIFFERENTIAL_V2_ORACLE_PASS": oracle_pass,
        "DIFFERENTIAL_V2_CENSORING_ACCOUNTED": censoring_accounted,
        "DIFFERENTIAL_V2_NO_OBSERVED_MISMATCH": no_observed_mismatch,
    }
    gates["DIFFERENTIAL_V2_ACCEPTED"] = all(gates.values())
    result = {
        "protocol_version": protocol.get("protocol_version"),
        "baseline_source_commit": protocol.get("baseline_source_commit"),
        "optimized_source_commit": protocol.get("optimized_source_commit"),
        "gates": gates,
        "component_gates": component_gates,
        "evidence": {
            "v1_differential": {"matched": v1_matched, "mismatched": v1_mismatched,
                                "censored": v1_censored, "censoring": v1_censoring},
            "transition_equivalence": {
                "cells_checked": transition_cells,
                "states_checked": transition.get("states_checked"),
                "transitions_checked": transition.get("transitions_checked"),
                "mismatch_count": transition.get("mismatch_count"),
                "graph_reconstruction_pass": transition.get("graph_reconstruction_pass"),
                "no_memo_full_automaton": transition.get("no_memo_full_automaton"),
                "censored_cells": transition.get("cells_censored"),
            },
            "pilot_agreement": agreement,
            "semantics": {"PASS": semantics.get("PASS")},
        },
        "components": components.get("components"),
        "decision": "GO" if gates["DIFFERENTIAL_V2_ACCEPTED"] else "NO-GO",
    }
    (args.out / "acceptance.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: result[key] for key in
                      ("decision", "gates", "component_gates", "evidence")}, indent=2))
    return 0 if result["gates"]["DIFFERENTIAL_V2_ACCEPTED"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
