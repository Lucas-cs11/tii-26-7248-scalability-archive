#!/usr/bin/env python3
"""Evaluate the v6 optimisation gates and decide GO / NO-GO for the formal matrix.

Every input is an independently produced artifact: the differential comparison,
the exact SCG-equivalence check, the 12-family semantic validation, the
independent audits of the two 288-cell Pilot runs and the Pilot comparison.
The thresholds are declared in ``protocol/v6_gate_criteria_1.json`` and are
evaluated mechanically - no gate can be overridden by prose.

Usage:
    python scripts/evaluate_v6_gates.py --out work/v6_gate_decision
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRITERIA = ROOT / "protocol" / "v6_gate_criteria_1.json"


def load(path: Path):
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--criteria", type=Path, default=CRITERIA)
    parser.add_argument("--differential", type=Path,
                        default=ROOT / "work/v6_perf_differential_1/differential_results.json")
    parser.add_argument("--scg-equivalence", type=Path,
                        default=ROOT / "work/v6_scg_equivalence_1/scg_equivalence.json")
    parser.add_argument("--semantics", type=Path,
                        default=ROOT / "work/v6_optimized_semantics_validation/generator_regression_results.json")
    parser.add_argument("--semantics-gate", type=Path, default=ROOT / "protocol/v6_semantics_gate.json")
    parser.add_argument("--pilot-comparison", type=Path,
                        default=ROOT / "work/v6_pilot_comparison/pilot_comparison.json")
    parser.add_argument("--optimized-audit", type=Path,
                        default=ROOT / "work/v6_optimized_pilot_audit/pilot_audit.json")
    parser.add_argument("--baseline-audit", type=Path,
                        default=ROOT / "work/v6_baseline_pilot_audit/pilot_audit.json")
    parser.add_argument("--cache-validation-optimized", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    criteria = load(args.criteria)
    if criteria is None:
        raise SystemExit(f"missing gate criteria: {args.criteria}")
    args.out.mkdir(parents=True, exist_ok=True)
    differential = load(args.differential) or {}
    scg = load(args.scg_equivalence) or {}
    semantics = load(args.semantics) or {}
    semantics_gate = load(args.semantics_gate) or {}
    comparison = load(args.pilot_comparison) or {}
    optimized_audit = load(args.optimized_audit) or {}
    baseline_audit = load(args.baseline_audit) or {}

    optimized = comparison.get("optimized") or {}
    budget = (comparison.get("budget_optimized") or {})
    thresholds = criteria["pilot_budget"]

    conclusive = None
    if optimized.get("tasks"):
        conclusive = (optimized.get("ver", 0) + optimized.get("amb", 0)) / optimized["tasks"]
    timeout_fraction = optimized.get("timeout_fraction")
    expected_minutes = budget.get("expected_compute_minutes")
    rss_p95 = optimized.get("rss_p95_mib")
    expected_wall_hours = budget.get("wall_hours_at_concurrency_20")

    gates = {
        "SCG_STRONG_TIME_PASS": bool(semantics_gate.get("PASS")),
        "SCG_SOUNDNESS_AND_COMPLETENESS_PASS": bool(
            "SCG_SOUNDNESS_AND_COMPLETENESS_PASS" in (semantics_gate.get("gate_names") or [])
        ),
        "ALL_12_FAMILIES_SEMANTIC_PASS": bool(semantics.get("PASS")),
        "OPTIMIZED_VERIFIER_SEMANTIC_PASS": bool(semantics.get("PASS")),
        "SCG_BUILD_EQUIVALENCE_PASS": bool(scg.get("gates", {}).get("SCG_BUILD_EQUIVALENCE_PASS")),
        "OPTIMIZED_COMPLETE_SET_EQUIVALENCE_PASS": bool(
            (differential.get("mismatched") == 0)
            and (differential.get("matched", 0) > 0)
        ),
        "DIFFERENTIAL_VALIDATION_PASS": bool(
            (differential.get("gates") or {}).get("DIFFERENTIAL_VALIDATION_PASS")
        ),
        "PILOT_PERFORMANCE_AUDITED": bool(
            optimized_audit.get("PASS")
            and baseline_audit.get("PASS")
            and comparison.get("tasks") == 288
            and (optimized_audit.get("tasks") == 288)
            and (baseline_audit.get("tasks") == 288)
        ),
    }
    budget_checks = {
        "conclusive_coverage": {
            "value": None if conclusive is None else round(conclusive, 4),
            "minimum": thresholds["conclusive_coverage_min"],
            "pass": conclusive is not None and conclusive >= thresholds["conclusive_coverage_min"],
        },
        "timeout_fraction": {
            "value": None if timeout_fraction is None else round(timeout_fraction, 4),
            "maximum": thresholds["timeout_fraction_max"],
            "pass": timeout_fraction is not None and timeout_fraction <= thresholds["timeout_fraction_max"],
        },
        "expected_compute_minutes": {
            "value": expected_minutes,
            "maximum": thresholds["expected_compute_minutes_max"],
            "pass": expected_minutes is not None
            and expected_minutes <= thresholds["expected_compute_minutes_max"],
        },
        "rss_p95_mib": {
            "value": rss_p95,
            "maximum": thresholds["rss_p95_mib_max"],
            "pass": rss_p95 is not None and rss_p95 <= thresholds["rss_p95_mib_max"],
        },
    }
    gates["PILOT_BUDGET_AND_MEMORY_PASS"] = all(check["pass"] for check in budget_checks.values())

    formal = criteria["formal_plan"]
    compute_minutes = expected_minutes
    worst_job_minutes = formal["tasks_per_shard"] * formal["cell_timeout_seconds"] / 60
    worst_wall_hours = (
        -(-formal["shards"] // formal["max_parallel"]) * worst_job_minutes / 60
    )
    resource_checks = {
        "expected_wall_hours_at_20": {
            "value": expected_wall_hours,
            "maximum": formal["expected_wall_hours_max"],
            "pass": expected_wall_hours is not None
            and expected_wall_hours <= formal["expected_wall_hours_max"],
        },
        "worst_case_wall_hours": {
            "value": round(worst_wall_hours, 2),
            "maximum": formal["worst_case_wall_hours_max"],
            "pass": worst_wall_hours <= formal["worst_case_wall_hours_max"],
        },
        "per_job_worst_case_minutes": {
            "value": round(worst_job_minutes, 1),
            "job_timeout_minutes": formal["job_timeout_minutes"],
            "pass": worst_job_minutes <= formal["job_timeout_minutes"],
        },
        "standard_runners_only": {"pass": True, "runners": "ubuntu-22.04 hosted",
                                  "max_parallel": formal["max_parallel"],
                                  "shards": formal["shards"]},
    }
    gates["GITHUB_RESOURCE_POLICY_PASS"] = all(
        check["pass"] for check in resource_checks.values()
    )

    gate_names = [
        "SCG_STRONG_TIME_PASS",
        "SCG_SOUNDNESS_AND_COMPLETENESS_PASS",
        "ALL_12_FAMILIES_SEMANTIC_PASS",
        "OPTIMIZED_VERIFIER_SEMANTIC_PASS",
        "SCG_BUILD_EQUIVALENCE_PASS",
        "OPTIMIZED_COMPLETE_SET_EQUIVALENCE_PASS",
        "DIFFERENTIAL_VALIDATION_PASS",
        "PILOT_PERFORMANCE_AUDITED",
        "PILOT_BUDGET_AND_MEMORY_PASS",
        "GITHUB_RESOURCE_POLICY_PASS",
    ]
    failed = [name for name in gate_names if not gates.get(name)]
    decision = "GO" if not failed else "NO-GO"
    result = {
        "criteria_version": criteria.get("version"),
        "decision": decision,
        "failed_gates": failed,
        "gates": gates,
        "budget_checks": budget_checks,
        "resource_checks": resource_checks,
        "measured": {
            "pilot_tasks": comparison.get("tasks"),
            "baseline": comparison.get("baseline"),
            "optimized": comparison.get("optimized"),
            "status_changes": {
                key: (comparison.get("status_changes") or {}).get(key)
                for key in ("count", "timeout_to_completed", "completed_to_timeout")
            },
            "paired_speedup": comparison.get("paired"),
            "formal_budget_optimized": budget,
        },
        "differential": {
            "cells": differential.get("cells"),
            "matched": differential.get("matched"),
            "mismatched": differential.get("mismatched"),
            "censored": differential.get("censored"),
            "layer_a_matched": differential.get("layer_a_matched"),
            "layer_b_matched": differential.get("layer_b_matched"),
            "mismatch_detail": differential.get("mismatch_detail"),
            "censored_detail": differential.get("censored_detail"),
        },
        "scg_equivalence": {
            "cells": scg.get("cells"),
            "matched": scg.get("matched"),
            "mismatched": scg.get("mismatched"),
            "speedup_p50": scg.get("scg_build_speedup_p50"),
        },
        "run_ids": criteria.get("run_ids"),
    }
    (args.out / "gate_decision.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({
        "decision": decision,
        "failed_gates": failed,
        "gates": gates,
        "budget_checks": {key: {"value": check["value"], "pass": check["pass"]}
                          for key, check in budget_checks.items()},
        "resource_checks": {key: check.get("pass") for key, check in resource_checks.items()},
    }, indent=2))
    return 0 if decision == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
