#!/usr/bin/env python3
"""Recompute v5 statistics from independently audited merged raw records."""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


FLAT_FIELDS = [
    "task_id", "experiment_id", "experiment", "benchmark_family",
    "base_model_id", "model_id", "model_hash", "bounded_problem_id",
    "configuration_id", "seed", "replicate_id", "commit_sha",
    "workflow_run_id", "job_id", "shard_id", "number_of_places",
    "number_of_transitions", "number_of_arcs", "number_of_parts",
    "operations_per_part", "resource_count", "WIP",
    "verified_reachable_concurrency", "cycle_presence", "cycle_semantics",
    "fault_configuration", "observable_ratio", "L", "H", "mode",
    "verdict", "execution_status", "runtime_sec", "time_limit_sec",
    "scg_limit", "SCG_classes", "SCG_edges", "synchronizer_states",
    "candidate_words", "retained_path_records", "terminal_path_pairs",
    "LP_calls", "partial_workload", "failure_stage", "error_type",
    "raw_artifact", "source_code_hash", "configuration_hash",
    "raw_record_hash", "validation_status",
]


def q95(values):
    values = sorted(values)
    return values[min(len(values) - 1, int(0.95 * len(values)))] if values else None


def median(values):
    return statistics.median(values) if values else None


def numeric(records, field, *, completed_only=False):
    values = []
    for record in records:
        if completed_only and record.get("execution_status") != "COMPLETED":
            continue
        value = record.get(field)
        if value is not None:
            values.append(float(value))
    return values


def group_summary(records, keys):
    row = dict(keys)
    statuses = Counter(record.get("execution_status") for record in records)
    verdicts = Counter(str(record.get("verdict")) for record in records)
    completed_runtime = numeric(records, "runtime_sec", completed_only=True)
    row.update({
        "n_declared": len(records),
        "n_models": len({record.get("model_hash") for record in records if record.get("model_hash")}),
        "n_bounded_problems": len({record.get("bounded_problem_id") for record in records}),
        "n_completed": statuses.get("COMPLETED", 0),
        "n_timeout": statuses.get("TIMEOUT_WALL_CLOCK", 0),
        "n_inc": verdicts.get("INC", 0),
        "n_ver": verdicts.get("VER", 0),
        "n_amb": verdicts.get("AMB", 0),
        "n_error": sum(count for status, count in statuses.items()
                       if status not in {"COMPLETED", "TIMEOUT_WALL_CLOCK"}),
        "completion_rate": statuses.get("COMPLETED", 0) / len(records) if records else None,
        "runtime_completed_median_s": median(completed_runtime),
        "runtime_completed_p95_s": q95(completed_runtime),
        "partial_workload_records": sum(bool(record.get("partial_workload")) for record in records),
    })
    for field in ("SCG_classes", "SCG_edges", "synchronizer_states",
                  "candidate_words", "retained_path_records", "LP_calls"):
        values = numeric(records, field)
        row[f"{field}_observed_n"] = len(values)
        row[f"{field}_observed_median"] = median(values)
        row[f"{field}_observed_p95"] = q95(values)
    return row


def write_csv(path, rows):
    rows = list(rows)
    if not rows:
        path.write_text("")
        return
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merged", type=Path, required=True)
    ap.add_argument("--integrity", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    integrity = json.loads(args.integrity.read_text())
    if not integrity.get("PASS"):
        raise SystemExit("refusing to analyze results whose independent integrity audit is not PASS")
    records = [json.loads(line) for line in args.merged.open() if line.strip()]

    flat = []
    for record in records:
        flat.append({
            key: (json.dumps(record.get(key), sort_keys=True)
                  if isinstance(record.get(key), (dict, list)) else record.get(key))
            for key in FLAT_FIELDS
        })
    write_csv(args.out / "all_formal_results.csv", flat)
    for experiment, filename in (
        ("A_structural", "structural_raw.csv"),
        ("B_wip", "wip_raw.csv"),
        ("C_Lsweep", "L_sweep_raw.csv"),
        ("D_difficult", "difficult_raw.csv"),
    ):
        write_csv(args.out / filename, [row for row, record in zip(flat, records)
                                        if record.get("experiment") == experiment])

    structural_groups = defaultdict(list)
    for record in records:
        if record.get("experiment") != "A_structural":
            continue
        prefix, level, _ = record["base_model_id"].split(":", 2)
        structural_groups[(prefix, int(level[1:]))].append(record)
    structural_stats = []
    for (series, level), group in sorted(structural_groups.items()):
        sizes = [record["number_of_places"] + record["number_of_transitions"] for record in group]
        row = group_summary(group, {"series": series, "level": level})
        row.update({"size_min": min(sizes), "size_median": median(sizes), "size_max": max(sizes)})
        structural_stats.append(row)
    write_csv(args.out / "structural_statistics.csv", structural_stats)

    wip_stats = []
    for wip in (1, 2, 3):
        group = [record for record in records
                 if record.get("experiment") == "B_wip" and int(record["WIP"]) == wip]
        wip_stats.append(group_summary(group, {"WIP": wip}))
    write_csv(args.out / "wip_statistics.csv", wip_stats)

    l_groups = defaultdict(list)
    for record in records:
        if record.get("experiment") == "C_Lsweep":
            l_groups[(record["mode"], record["benchmark_family"], int(record["L"]))].append(record)
    l_stats = [
        group_summary(group, {"mode": key[0], "benchmark_family": key[1], "L": key[2]})
        for key, group in sorted(l_groups.items())
    ]
    write_csv(args.out / "L_sweep_statistics.csv", l_stats)

    model_groups = defaultdict(list)
    for record in records:
        if record.get("experiment") == "C_Lsweep":
            model_groups[(record["mode"], record["benchmark_family"], record["model_id"])].append(record)
    sensitivity_rows = []
    for (mode, family, model_id), group in sorted(model_groups.items()):
        group.sort(key=lambda record: record["L"])
        observed = [
            (record["L"], (record.get("counters") or {}).get("trace_records"))
            for record in group if (record.get("counters") or {}).get("trace_records") is not None
        ]
        frontiers = [observed[index][0] for index in range(1, len(observed))
                     if observed[index][1] > observed[index - 1][1]]
        amb_levels = [record["L"] for record in group
                      if record.get("execution_status") == "COMPLETED" and record.get("verdict") == "AMB"]
        statuses = Counter(record.get("execution_status") for record in group)
        sensitivity_rows.append({
            "mode": mode, "benchmark_family": family, "model_id": model_id,
            "declared_levels": len(group), "observed_workload_levels": len(observed),
            "growth_frontier_count": len(frontiers),
            "growth_frontiers": json.dumps(frontiers),
            "first_verified_amb_L": min(amb_levels) if amb_levels else None,
            "completed_levels": statuses.get("COMPLETED", 0),
            "timeout_levels": statuses.get("TIMEOUT_WALL_CLOCK", 0),
        })
    write_csv(args.out / "L_model_sensitivity.csv", sensitivity_rows)

    difficult_groups = defaultdict(list)
    for record in records:
        if record.get("experiment") == "D_difficult":
            parts = record["base_model_id"].split(":")
            difficult_groups[(record["benchmark_family"], int(parts[2][1:]))].append(record)
    difficult_stats = [
        group_summary(group, {"benchmark_family": key[0], "difficulty_level": key[1]})
        for key, group in sorted(difficult_groups.items())
    ]
    write_csv(args.out / "difficult_statistics.csv", difficult_stats)

    status_counts = Counter(record.get("execution_status") for record in records)
    verdict_counts = Counter(str(record.get("verdict")) for record in records)
    complete_sensitivity = [row for row in sensitivity_rows if row["mode"] == "complete"]
    summary = {
        "PASS": True,
        "integrity_audit_PASS": True,
        "evaluation_runs": len(records),
        "distinct_model_hashes": len({record.get("model_hash") for record in records if record.get("model_hash")}),
        "distinct_bounded_problems": len({record.get("bounded_problem_id") for record in records}),
        "parameter_configurations": len({record.get("configuration_id") for record in records}),
        "timing_replicates_beyond_primary": sum(int(record.get("replicate_id") or 0) > 0 for record in records),
        "execution_status": dict(status_counts),
        "verdict": dict(verdict_counts),
        "complete_set_fixed_models": len(complete_sensitivity),
        "complete_set_models_with_history_growth": sum(row["growth_frontier_count"] > 0
                                                        for row in complete_sensitivity),
        "complete_set_total_growth_frontiers": sum(row["growth_frontier_count"]
                                                    for row in complete_sensitivity),
        "notes": [
            "Runtime quantiles use completed records only; completion/status denominators use all declared records.",
            "Observed workload includes checkpoint values when present and reports its own observed_n denominator.",
            "FIRST_CERTIFICATE and complete-set modes are never pooled.",
        ],
    }
    (args.out / "statistics_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
