#!/usr/bin/env python3
"""Aggregate the frozen profiling sample into a bottleneck picture.

Reads the raw records written by ``scripts/run_v6_profile_sample.py`` and the
frozen sample, then reports, per family and stratum:

* execution status and right-censored wall times,
* the stage decomposition of completed cells (SCG, synchronizer, unobservable
  closure subset, temporal expansion, critical-pair collection, LP build, LP
  solve, other),
* operation counts (successor lookups, synchronizer transitions, words
  explored, expansion nodes/records, LP solves),
* where the censored cells stopped and what their last stage counters were.

No number is invented: missing stage data is reported as null / "not executed".
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STAGE_FIELDS = (
    "runtime_scg",
    "runtime_sync",
    "runtime_unobservable_closure",
    "runtime_expansion",
    "runtime_certificate",
    "runtime_lp_build",
    "runtime_lp_solve",
    "runtime_lp",
    "runtime_total",
)
COUNT_FIELDS = (
    "scg_classes", "scg_edges", "sync_states", "sync_edges", "candidate_words",
    "sync_transition_calls", "sync_successors_calls", "sync_words_explored",
    "sync_closure_N_calls", "sync_closure_F_calls",
    "exp_nodes_created", "exp_edges", "exp_record_checks", "exp_worklist_pops",
    "lp_calls", "lp_builds", "lp_solves", "lp_feasible", "lp_infeasible",
)


def percentile(values, probability):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(probability * len(ordered)) - 1)]


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not (isinstance(value, float) and math.isnan(value)):
        return float(value)
    return None


def metrics(records):
    completed = [r for r in records if r.get("execution_status") == "COMPLETED"]
    timeouts = [r for r in records if r.get("execution_status") == "TIMEOUT_WALL_CLOCK"]
    walls = [v for r in completed for v in [_num(r.get("wall_s"))] if v is not None]

    def stage_sum(field):
        return sum(
            v for r in completed
            for v in [_num((r.get("counters") or {}).get(field))] if v is not None
        )

    stage_total = {field: stage_sum(field) for field in STAGE_FIELDS}
    wall_total = sum(walls)
    measured = (
        stage_total["runtime_scg"]
        + stage_total["runtime_sync"]
        + stage_total["runtime_expansion"]
        + stage_total["runtime_certificate"]
        + stage_total["runtime_lp"]
    )
    return {
        "cells": len(records),
        "completed": len(completed),
        "timeout": len(timeouts),
        "completed_wall_sum_s": round(wall_total, 3),
        "completed_wall_p50_s": percentile(walls, 0.50),
        "completed_wall_p90_s": percentile(walls, 0.90),
        "stage_sum_s": {field: round(value, 4) for field, value in stage_total.items()},
        "stage_share_of_wall": {
            field: (round(value / wall_total, 4) if wall_total else None)
            for field, value in stage_total.items()
        },
        "other_s": round(max(0.0, wall_total - measured), 3) if walls else None,
        "counts_sum": {
            field: sum(
                v for r in completed
                for v in [_num((r.get("counters") or {}).get(field))] if v is not None
            )
            for field in COUNT_FIELDS
        },
        "rss_max_mib": max(
            (v for r in completed for v in [_num(r.get("peak_rss_mib") or r.get("peak_rss_mb"))] if v is not None),
            default=None,
        ),
        "timeout_stage": dict(
            Counter(str(r.get("failure_stage") or "UNKNOWN") for r in timeouts)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=Path, default=ROOT / "protocol/v6_profile_sample_1.json")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    sample = json.loads(args.sample.read_text())
    out_dir = args.out or args.run_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted((args.run_dir / "raw").glob("*.jsonl")):
        records.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    sample_cells = {cell["task_id"]: cell for cell in sample["cells_detail"]}
    for record in records:
        cell = sample_cells.get(record.get("task_id"), {})
        record["benchmark_family"] = record.get("benchmark_family") or cell.get("benchmark_family")
        record["stratum"] = record.get("stratum") or cell.get("stratum")
    result = {
        "sample": str(args.sample.relative_to(ROOT)) if args.sample.is_relative_to(ROOT) else str(args.sample),
        "sample_cells": len(sample_cells),
        "raw_records": len(records),
        "overall": metrics(records),
        "by_family": {
            family: metrics([r for r in records if r.get("benchmark_family") == family])
            for family in sorted({r.get("benchmark_family") for r in records})
        },
        "by_stratum": {
            stratum: metrics([r for r in records if r.get("stratum") == stratum])
            for stratum in sorted({r.get("stratum") for r in records})
        },
        "completed_stage_ranking": [],
        "timeout_detail": [],
    }
    overall = result["overall"]
    ranking = [
        (field, value)
        for field, value in overall["stage_sum_s"].items()
        if value
    ]
    result["completed_stage_ranking"] = [
        {"stage": field, "seconds": value,
         "share_of_completed_wall": overall["stage_share_of_wall"].get(field)}
        for field, value in sorted(ranking, key=lambda item: item[1], reverse=True)
    ]
    for record in records:
        if record.get("execution_status") != "TIMEOUT_WALL_CLOCK":
            continue
        counters = record.get("counters") or {}
        result["timeout_detail"].append({
            "task_id": record.get("task_id"),
            "family": record.get("benchmark_family"),
            "stratum": record.get("stratum"),
            "failure_stage": record.get("failure_stage"),
            "wall_s": record.get("wall_s"),
            "scg_classes": counters.get("scg_classes"),
            "sync_states": counters.get("sync_states"),
            "candidate_words": counters.get("candidate_words"),
            "exp_nodes_created": counters.get("exp_nodes_created"),
            "lp_calls": counters.get("lp_calls"),
            "runtime_scg": counters.get("runtime_scg"),
            "runtime_sync": counters.get("runtime_sync"),
            "runtime_expansion": counters.get("runtime_expansion"),
        })
    (out_dir / "profile_analysis.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    rows = []
    for family, values in result["by_family"].items():
        row = {"scope": "family", "name": family}
        row.update({
            "cells": values["cells"], "completed": values["completed"],
            "timeout": values["timeout"],
            "wall_p50_s": values["completed_wall_p50_s"],
            "wall_p90_s": values["completed_wall_p90_s"],
        })
        for field in STAGE_FIELDS:
            row[field] = values["stage_sum_s"].get(field)
        rows.append(row)
    with (out_dir / "profile_statistics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({
        "raw_records": result["raw_records"],
        "completed": overall["completed"],
        "timeout": overall["timeout"],
        "completed_wall_sum_s": overall["completed_wall_sum_s"],
        "stage_ranking": result["completed_stage_ranking"],
        "timeout_stage": overall["timeout_stage"],
        "outputs": [str(out_dir / name) for name in ("profile_analysis.json", "profile_statistics.csv")],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
