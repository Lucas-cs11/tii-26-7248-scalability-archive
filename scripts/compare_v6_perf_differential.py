#!/usr/bin/env python3
"""Compare the two sides of the v6 performance differential and decide its gate.

A pair counts only when both sides completed under the same cell cap.  Pairs
still censored after Layer B are reported as uncertified and make
``DIFFERENTIAL_VALIDATION_PASS`` false - "both timed out" is never treated as
agreement.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_FIELDS = (
    "verdict",
    "inconclusive_reason",
    "certificate_mode",
    "enumeration_complete",
    "critical_pair_count",
    "model_hash",
)
COUNTER_FIELDS = (
    "scg_classes", "scg_edges", "sync_states", "candidate_words",
    "candidate_words_processed", "temporal_states", "trace_records",
    "terminal_pairs", "lp_calls", "enclosure_rejected_pairs",
    "terminal_normal_records", "terminal_faulty_records",
)


def load_results(path):
    if not path.is_file():
        return {}
    return {
        json.loads(line)["task_id"]: json.loads(line)
        for line in path.read_text().splitlines() if line.strip()
    }


def compare_cell(task_id, baseline, optimized):
    mismatches = []
    if baseline.get("execution_status") != "COMPLETED" or optimized.get("execution_status") != "COMPLETED":
        baseline_status = baseline.get("execution_status")
        optimized_status = optimized.get("execution_status")
        if baseline_status != "COMPLETED" and optimized_status != "COMPLETED":
            category = "both_censored"
        elif baseline_status != "COMPLETED":
            category = "baseline_censored_optimized_completed"
        else:
            category = "optimized_censored_baseline_completed"
        return {
            "task_id": task_id,
            "status": "CENSORED",
            "censoring": category,
            "mismatches": [],
            "baseline_status": baseline_status,
            "optimized_status": optimized_status,
        }
    for field in SEMANTIC_FIELDS:
        if baseline.get(field) != optimized.get(field):
            mismatches.append({"field": field, "baseline": baseline.get(field),
                               "optimized": optimized.get(field)})
    if baseline.get("certificates") != optimized.get("certificates"):
        mismatches.append({
            "field": "certificates",
            "baseline": len(baseline.get("certificates") or []),
            "optimized": len(optimized.get("certificates") or []),
        })
    baseline_stats = baseline.get("stats") or {}
    optimized_stats = optimized.get("stats") or {}
    compared_counters = []
    for field in COUNTER_FIELDS:
        if field in baseline_stats or field in optimized_stats:
            compared_counters.append(field)
            left = baseline_stats.get(field)
            right = optimized_stats.get(field)
            if left != right:
                mismatches.append({"field": field, "baseline": left, "optimized": right})
    baseline_wall = baseline.get("wall_s")
    optimized_wall = optimized.get("wall_s")
    speedup = None
    if baseline_wall and optimized_wall and optimized_wall > 0:
        speedup = round(baseline_wall / optimized_wall, 2)
    return {
        "task_id": task_id,
        "status": "MATCH" if not mismatches else "MISMATCH",
        "censoring": None,
        "baseline_status": baseline.get("execution_status"),
        "optimized_status": optimized.get("execution_status"),
        "mismatches": mismatches,
        "counter_fields_compared": compared_counters,
        "baseline_wall_s": baseline_wall,
        "optimized_wall_s": optimized_wall,
        "speedup": speedup,
        "baseline_certificates": len(baseline.get("certificates") or []),
        "optimized_certificates": len(optimized.get("certificates") or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, default=ROOT / "protocol/v6_perf_differential_1.json")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text())
    out_dir = args.out or args.run_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    layer_a = load_results(args.run_dir / "layer_a_results.jsonl")
    layer_b = load_results(args.run_dir / "layer_b_results.jsonl")
    family = {cell["task_id"]: cell for cell in protocol["cells_detail"]}
    pairs = []
    for task_id in sorted(family):
        baseline = layer_a[task_id]["baseline"]
        optimized = layer_a[task_id]["optimized"]
        layer = "A"
        if (
            baseline.get("execution_status") != "COMPLETED"
            or optimized.get("execution_status") != "COMPLETED"
        ) and task_id in layer_b:
            baseline = layer_b[task_id]["baseline"]
            optimized = layer_b[task_id]["optimized"]
            layer = "B"
        comparison = compare_cell(task_id, baseline, optimized)
        comparison["layer"] = layer
        comparison["benchmark_family"] = family[task_id]["benchmark_family"]
        comparison["stratum"] = family[task_id]["stratum"]
        comparison["mode"] = family[task_id]["mode"]
        pairs.append(comparison)
    matched = [p for p in pairs if p["status"] == "MATCH"]
    mismatched = [p for p in pairs if p["status"] == "MISMATCH"]
    censored = [p for p in pairs if p["status"] == "CENSORED"]
    speedups = sorted(p["speedup"] for p in matched if p.get("speedup"))
    median_speedup = speedups[len(speedups) // 2] if speedups else None
    result = {
        "protocol_version": protocol["protocol_version"],
        "baseline_commit": protocol["baseline_commit"],
        "optimized_commit": protocol["optimized_commit"],
        "cells": len(pairs),
        "matched": len(matched),
        "mismatched": len(mismatched),
        "censored": len(censored),
        "layer_a_matched": sum(1 for p in matched if p["layer"] == "A"),
        "layer_b_matched": sum(1 for p in matched if p["layer"] == "B"),
        "mismatch_detail": mismatched,
        "censoring_breakdown": {
            "both_censored": sum(1 for p in censored if p.get("censoring") == "both_censored"),
            "baseline_censored_optimized_completed": sum(
                1 for p in censored
                if p.get("censoring") == "baseline_censored_optimized_completed"
            ),
            "optimized_censored_baseline_completed": sum(
                1 for p in censored
                if p.get("censoring") == "optimized_censored_baseline_completed"
            ),
        },
        "censored_detail": [
            {key: p.get(key) for key in ("task_id", "benchmark_family", "stratum",
                                         "baseline_status", "optimized_status", "censoring")}
            for p in censored
        ],
        "performance": {
            "paired_speedup_samples": len(speedups),
            "paired_speedup_median": median_speedup,
            "paired_speedup_min": speedups[0] if speedups else None,
            "paired_speedup_max": speedups[-1] if speedups else None,
        },
        "gates": {
            "DIFFERENTIAL_VALIDATION_PASS": (
                not mismatched and not censored and len(matched) == len(pairs)
            ),
            "reason": (
                "all pairs completed on both sides and matched exactly"
                if (not mismatched and not censored and len(matched) == len(pairs))
                else f"{len(censored)} censored, {len(mismatched)} mismatched"
            ),
        },
    }
    (out_dir / "differential_results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    with (out_dir / "differential_pairs.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "task_id", "benchmark_family", "stratum", "mode", "layer", "status",
            "baseline_status", "optimized_status", "censoring", "baseline_wall_s",
            "optimized_wall_s", "speedup", "mismatch_fields",
        ])
        writer.writeheader()
        for pair in pairs:
            writer.writerow({
                "task_id": pair["task_id"],
                "benchmark_family": pair["benchmark_family"],
                "stratum": pair["stratum"],
                "mode": pair["mode"],
                "layer": pair["layer"],
                "status": pair["status"],
                "baseline_status": pair.get("baseline_status"),
                "optimized_status": pair.get("optimized_status"),
                "censoring": pair.get("censoring"),
                "baseline_wall_s": pair.get("baseline_wall_s"),
                "optimized_wall_s": pair.get("optimized_wall_s"),
                "speedup": pair.get("speedup"),
                "mismatch_fields": ";".join(m["field"] for m in pair["mismatches"]),
            })
    print(json.dumps({
        "cells": result["cells"],
        "matched": result["matched"],
        "mismatched": result["mismatched"],
        "censored": result["censored"],
        "layer_a_matched": result["layer_a_matched"],
        "layer_b_matched": result["layer_b_matched"],
        "performance": result["performance"],
        "gates": result["gates"],
    }, indent=2))
    return 0 if result["gates"]["DIFFERENTIAL_VALIDATION_PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
