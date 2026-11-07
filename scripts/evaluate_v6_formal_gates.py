#!/usr/bin/env python3
"""Evaluate the formal 8,280-cell gates required before freezing the protocol."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path):
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--semantics-gate", type=Path,
                        default=ROOT / "protocol/v6_semantics_gate.json")
    parser.add_argument("--differential-v2", type=Path,
                        default=ROOT / "work/v6_differential_v2_acceptance/acceptance.json")
    parser.add_argument("--baseline-pilot-audit", type=Path,
                        default=ROOT / "work/v6_baseline_pilot_audit/pilot_audit.json")
    parser.add_argument("--optimized-pilot-audit", type=Path,
                        default=ROOT / "work/v6_optimized_pilot_audit/pilot_audit.json")
    parser.add_argument("--pilot-comparison", type=Path,
                        default=ROOT / "work/v6_pilot_comparison/pilot_comparison.json")
    parser.add_argument("--modal-pilot-audit", type=Path,
                        default=ROOT / "work/v6_modal_pilot/audit.json")
    parser.add_argument("--modal-pilot-summary", type=Path,
                        default=ROOT / "work/v6_modal_pilot/pilot_summary.json")
    parser.add_argument("--modal-recovery", type=Path,
                        default=ROOT / "work/v6_modal_recovery/recovery_test.json")
    parser.add_argument("--modal-budget", type=Path,
                        default=ROOT / "work/v6_modal_budget/budget.json")
    parser.add_argument("--formal-manifest", type=Path,
                        default=ROOT / "protocol/v6_formal_manifest.json")
    parser.add_argument("--formal-validation", type=Path,
                        default=ROOT / "work/v6_formal_protocol_validation/validation.json")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "work/v6_formal_gate_decision")
    args = parser.parse_args()
    semantics = load(args.semantics_gate) or {}
    v2 = load(args.differential_v2) or {}
    baseline_audit = load(args.baseline_pilot_audit) or {}
    optimized_audit = load(args.optimized_pilot_audit) or {}
    comparison = load(args.pilot_comparison) or {}
    modal_audit = load(args.modal_pilot_audit) or {}
    modal_summary = load(args.modal_pilot_summary) or {}
    recovery = load(args.modal_recovery) or {}
    budget = load(args.modal_budget) or {}
    manifest = load(args.formal_manifest) or {}
    validation = load(args.formal_validation) or {}
    args.out.mkdir(parents=True, exist_ok=True)

    gates = {
        "SCG_STRONG_TIME_PASS": bool(semantics.get("PASS")),
        "SCG_SOUNDNESS_AND_COMPLETENESS_PASS": bool(
            "SCG_SOUNDNESS_AND_COMPLETENESS_PASS" in (semantics.get("gate_names") or [])
        ),
        "DBM_NORMALIZATION_PASS": bool(
            "DBM_NORMALIZATION_PASS" in (semantics.get("gate_names") or [])
        ),
        "ALL_12_FAMILIES_SEMANTIC_PASS": bool(
            "ALL_12_FAMILIES_SEMANTIC_PASS" in (semantics.get("gate_names") or [])
        ),
        "DIFFERENTIAL_V2_ACCEPTED": bool(
            (v2.get("gates") or {}).get("DIFFERENTIAL_V2_ACCEPTED")
        ),
        "PILOT_PERFORMANCE_AUDITED": bool(
            baseline_audit.get("PASS")
            and optimized_audit.get("PASS")
            and baseline_audit.get("tasks") == 288
            and optimized_audit.get("tasks") == 288
            and (comparison.get("tasks") == 288)
        ),
    }
    optimized = (comparison.get("optimized") or {})
    rss_p95 = optimized.get("rss_p95_mib")
    timeout_fraction = optimized.get("timeout_fraction")
    budget_result = (comparison.get("budget_optimized") or {})
    gates["PILOT_MEMORY_PASS"] = bool(
        rss_p95 is not None and rss_p95 <= 2048
        and timeout_fraction is not None and timeout_fraction <= 0.40
        and budget_result.get("expected_compute_minutes") is not None
        and budget_result["expected_compute_minutes"] <= 3000
    )
    gates["MODAL_ENVIRONMENT_PASS"] = bool(
        modal_audit.get("PASS")
        and modal_summary.get("PASS", True)
        and modal_summary.get("verdict_consistency_pass")
        and not modal_summary.get("oom_events")
        # timeouts are right-censored observations, not missing tasks: every
        # task must have a record, with no error, missing or duplicate record.
        and modal_summary.get("tasks_recorded") == modal_summary.get("tasks_expected")
        and not modal_summary.get("missing_count")
        and not modal_summary.get("duplicate_count")
        and not modal_summary.get("tasks_error")
        and not modal_summary.get("missing")
        and not modal_summary.get("duplicates")
        and modal_summary.get("resource_measurements_recorded")
    )
    gates["MODAL_CHECKPOINT_RECOVERY_PASS"] = bool(
        recovery.get("PASS")
        and recovery.get("resume_skipped_completed") is True
        and recovery.get("no_duplicate_records") is True
    )
    gates["MODAL_BUDGET_POLICY_PASS"] = bool(
        budget.get("authorized")
        and budget.get("cap_usd") is not None
        and budget.get("cap_usd") <= 10.0
        and budget.get("current_spend_usd") is not None
        and budget.get("projected_full_run_usd") is not None
        and budget.get("projected_full_run_usd") <= budget.get("cap_usd")
        and budget.get("credits_confirmed_by_user") is True
    )
    grid_ok = bool(
        manifest.get("tasks") == 8280
        and manifest.get("unique_configurations") == 8280
        and validation.get("PASS")
        and validation.get("tasks") == 8280
        and validation.get("unique_configurations") == 8280
        and validation.get("distinct_bounded_problems") == 7380
    )
    gates["FORMAL_PROTOCOL_READY"] = grid_ok
    result = {
        "decision": "GO" if all(gates.values()) else "NO-GO",
        "failed_gates": [name for name, value in gates.items() if not value],
        "gates": gates,
        "measured": {
            "pilot": {
                "tasks": comparison.get("tasks"),
                "completed": optimized.get("completed"),
                "timeout_fraction": timeout_fraction,
                "rss_p95_mib": rss_p95,
                "expected_compute_minutes": budget_result.get("expected_compute_minutes"),
            },
            "modal_pilot": modal_summary,
            "budget": budget,
            "formal_grid": {
                "tasks": manifest.get("tasks"),
                "shards": manifest.get("shards"),
                "validation_pass": validation.get("PASS"),
            },
        },
    }
    (args.out / "formal_gate_decision.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({"decision": result["decision"],
                      "failed_gates": result["failed_gates"],
                      "gates": gates}, indent=2))
    return 0 if result["decision"] == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
