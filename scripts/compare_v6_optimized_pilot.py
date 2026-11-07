#!/usr/bin/env python3
"""Compare the frozen 288-cell Pilot across two versions of the verifier.

Both sides ran the same frozen task list, the same 60 s per-cell cap and the
same GitHub-hosted ubuntu-22.04 runner type; the only difference is the
verifier implementation.  The comparison reports, per family / experiment /
stratum: execution status, verdicts, right-censored wall percentiles, peak
RSS, the instrumented stage decomposition and operation counts, paired
speedups for cells completed on both sides, and a censoring-aware budget for
the formal 8,280-cell matrix.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

FORMAL_QUOTA = {"structural": 600, "wip": 1080, "lsweep": 5400, "difficult": 1200}
CELL_CAP = 60.0
FORMAL_TASKS = sum(FORMAL_QUOTA.values())


def load_raw(artifacts: Path):
    records = []
    for path in sorted(artifacts.glob("**/raw/shard_*.jsonl")):
        records.extend(
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        )
    return records


def percentile(values, probability):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(probability * len(ordered)) - 1)]


def num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) and not (isinstance(value, float) and math.isnan(value)):
        return float(value)
    return None


def summarize(records):
    statuses = Counter(r.get("execution_status") for r in records)
    verdicts = Counter(str(r.get("verdict")) for r in records)
    completed = [r for r in records if r.get("execution_status") == "COMPLETED"]
    walls = [v for r in completed for v in [num(r.get("wall_s"))] if v is not None]
    rss = [v for r in records for v in [num(r.get("peak_rss_mb"))] if v is not None and v > 0]
    sync = [v for r in completed for v in [num(r.get("runtime_synchronizer_s"))] if v is not None]
    expansion = [v for r in completed for v in [num(r.get("runtime_history_expansion_s"))] if v is not None]
    scg = [v for r in completed for v in [num(r.get("runtime_scg_build_s") or (r.get("counters") or {}).get("runtime_scg"))] if v is not None]
    timeouts = [r for r in records if r.get("execution_status") == "TIMEOUT_WALL_CLOCK"]
    return {
        "tasks": len(records),
        "completed": statuses.get("COMPLETED", 0),
        "timeout": statuses.get("TIMEOUT_WALL_CLOCK", 0),
        "error": statuses.get("ERROR", 0),
        "inc": verdicts.get("INC", 0),
        "ver": verdicts.get("VER", 0),
        "amb": verdicts.get("AMB", 0),
        "timeout_fraction": (statuses.get("TIMEOUT_WALL_CLOCK", 0) / len(records)) if records else None,
        "timeout_stage": dict(Counter(str(r.get("failure_stage")) for r in timeouts)),
        "wall_p50_s": percentile(walls, 0.50),
        "wall_p90_s": percentile(walls, 0.90),
        "wall_p95_s": percentile(walls, 0.95),
        "rss_p95_mib": percentile(rss, 0.95),
        "rss_max_mib": max(rss) if rss else None,
        "sync_p50_s": percentile(sync, 0.50),
        "expansion_p50_s": percentile(expansion, 0.50),
        "scg_p50_s": percentile(scg, 0.50),
    }


def bucket_of(record):
    experiment = record.get("experiment")
    return {
        "A_structural": "structural",
        "B_wip": "wip",
        "C_Lsweep": "lsweep",
        "D_difficult": "difficult",
    }.get(experiment, "structural")


def budget_from(records):
    """Censoring-aware expectation for the formal matrix using this run's mix."""
    by_bucket = defaultdict(list)
    for record in records:
        by_bucket[bucket_of(record)].append(record)
    result = {"buckets": {}, "expected_compute_minutes": None}
    total = 0.0
    complete = True
    for bucket, quota in FORMAL_QUOTA.items():
        rows = by_bucket.get(bucket) or []
        summary = summarize(rows)
        timeout_fraction = summary["timeout_fraction"]
        wall_p50 = summary["wall_p50_s"]
        if timeout_fraction is None or wall_p50 is None:
            complete = False
            result["buckets"][bucket] = {"quota": quota, "complete": False}
            continue
        expected_cell = timeout_fraction * CELL_CAP + (1 - timeout_fraction) * wall_p50
        total += expected_cell * quota
        result["buckets"][bucket] = {
            "quota": quota,
            "observed_tasks": summary["tasks"],
            "timeout_fraction": round(timeout_fraction, 4),
            "wall_p50_s": wall_p50,
            "expected_cell_s": round(expected_cell, 3),
            "expected_compute_minutes": round(expected_cell * quota / 60, 1),
            "complete": True,
        }
    if complete:
        result["expected_compute_minutes"] = round(total / 60, 1)
        result["expected_compute_hours"] = round(total / 3600, 2)
        result["worst_case_compute_minutes"] = FORMAL_TASKS * CELL_CAP / 60
        result["wall_hours_at_concurrency_20"] = round(total / 60 / 20 / 60, 2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-artifacts", type=Path, required=True)
    parser.add_argument("--optimized-artifacts", type=Path, required=True)
    parser.add_argument("--baseline-run-id", required=True)
    parser.add_argument("--optimized-run-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    baseline = {r.get("task_id"): r for r in load_raw(args.baseline_artifacts)}
    optimized = {r.get("task_id"): r for r in load_raw(args.optimized_artifacts)}
    shared = sorted(set(baseline) & set(optimized))
    if len(shared) != len(baseline) or len(shared) != len(optimized):
        raise SystemExit(
            f"task sets differ: baseline={len(baseline)} optimized={len(optimized)} shared={len(shared)}"
        )
    flips = []
    paired_speedups = []
    for task_id in shared:
        left = baseline[task_id]
        right = optimized[task_id]
        left_status = left.get("execution_status")
        right_status = right.get("execution_status")
        if left_status != right_status:
            flips.append({
                "task_id": task_id,
                "family": left.get("benchmark_family"),
                "experiment": left.get("experiment"),
                "baseline_status": left_status,
                "optimized_status": right_status,
                "baseline_stage": left.get("failure_stage"),
                "optimized_stage": right.get("failure_stage"),
                "baseline_verdict": left.get("verdict"),
                "optimized_verdict": right.get("verdict"),
            })
        left_wall = num(left.get("wall_s"))
        right_wall = num(right.get("wall_s"))
        if (
            left_status == "COMPLETED" and right_status == "COMPLETED"
            and left_wall and right_wall and right_wall > 0
        ):
            paired_speedups.append(left_wall / right_wall)
    paired_speedups.sort()
    result = {
        "baseline_run_id": args.baseline_run_id,
        "optimized_run_id": args.optimized_run_id,
        "tasks": len(shared),
        "baseline": summarize(list(baseline.values())),
        "optimized": summarize(list(optimized.values())),
        "status_changes": {
            "count": len(flips),
            "timeout_to_completed": sum(
                1 for f in flips
                if f["baseline_status"] == "TIMEOUT_WALL_CLOCK" and f["optimized_status"] == "COMPLETED"
            ),
            "completed_to_timeout": sum(
                1 for f in flips
                if f["baseline_status"] == "COMPLETED" and f["optimized_status"] == "TIMEOUT_WALL_CLOCK"
            ),
            "detail": flips,
        },
        "paired": {
            "completed_on_both": len(paired_speedups),
            "speedup_p50": percentile(paired_speedups, 0.50),
            "speedup_p90": percentile(paired_speedups, 0.90),
            "speedup_min": paired_speedups[0] if paired_speedups else None,
            "speedup_max": paired_speedups[-1] if paired_speedups else None,
        },
        "by_experiment": {
            name: {
                "baseline": summarize([r for r in baseline.values() if r.get("experiment") == name]),
                "optimized": summarize([r for r in optimized.values() if r.get("experiment") == name]),
            }
            for name in ("A_structural", "B_wip", "C_Lsweep", "D_difficult")
        },
        "by_family": {
            family: {
                "baseline": summarize([r for r in baseline.values() if r.get("benchmark_family") == family]),
                "optimized": summarize([r for r in optimized.values() if r.get("benchmark_family") == family]),
            }
            for family in sorted({r.get("benchmark_family") for r in baseline.values()})
        },
        "budget_baseline": budget_from(list(baseline.values())),
        "budget_optimized": budget_from(list(optimized.values())),
    }
    (args.out / "pilot_comparison.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    rows = []
    for scope in ("by_experiment", "by_family"):
        for name, values in result[scope].items():
            row = {"scope": scope[3:], "name": name}
            for side in ("baseline", "optimized"):
                for key in ("tasks", "completed", "timeout", "inc", "ver", "amb",
                            "timeout_fraction", "wall_p50_s", "wall_p90_s",
                            "rss_p95_mib", "sync_p50_s", "expansion_p50_s", "scg_p50_s"):
                    row[f"{side}_{key}"] = values[side].get(key)
            rows.append(row)
    with (args.out / "pilot_comparison.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({
        "tasks": result["tasks"],
        "baseline": {k: result["baseline"][k] for k in ("completed", "timeout", "inc", "ver", "amb", "timeout_fraction")},
        "optimized": {k: result["optimized"][k] for k in ("completed", "timeout", "inc", "ver", "amb", "timeout_fraction")},
        "status_changes": {k: result["status_changes"][k] for k in ("count", "timeout_to_completed", "completed_to_timeout")},
        "paired_speedup_p50": result["paired"]["speedup_p50"],
        "budget_baseline_hours": result["budget_baseline"].get("expected_compute_hours"),
        "budget_optimized_hours": result["budget_optimized"].get("expected_compute_hours"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
