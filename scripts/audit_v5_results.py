#!/usr/bin/env python3
"""Independent identity, provenance, semantics, and workload audit for v5."""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
from collections import Counter, defaultdict
from pathlib import Path


def load_tasks(tasks_dir: Path):
    paths = sorted(tasks_dir.glob("shard_*.csv"))
    rows = []
    for path in paths:
        with path.open(newline="") as fh:
            rows.extend(csv.DictReader(fh))
    return paths, rows


def load_records(artifacts: Path):
    paths = sorted(artifacts.glob("**/raw/shard_*.jsonl"))
    records = []
    for path in paths:
        for line_number, line in enumerate(path.open(), 1):
            record = json.loads(line)
            record["audit_source_path"] = str(path.relative_to(artifacts))
            record["audit_source_line"] = line_number
            records.append(record)
    return paths, records


def expected_spec(row):
    return {
        "tag": row["task_id"],
        "experiment_id": row["experiment_id"],
        "benchmark_family": row["benchmark_family"],
        "model_id": row["model_id"],
        "bounded_problem_id": row["bounded_problem_id"],
        "configuration_id": row["configuration_id"],
        "experiment": row["experiment"],
        "model": row["model_id"],
        "seed": int(row["seed"]),
        "replicate": int(row["replicate"]),
        "L": int(row["L"]),
        "H": float(row["H"]),
        "mode": row["mode"],
        "timeout_s": int(row["timeout_s"]),
        "params": json.loads(row["params"]),
    }


def record_hash_valid(record):
    observed = record.get("raw_record_hash")
    payload = dict(record)
    payload.pop("raw_record_hash", None)
    payload.pop("audit_source_path", None)
    payload.pop("audit_source_line", None)
    expected = hashlib.sha256(
        json.dumps(payload, default=str, sort_keys=True).encode()
    ).hexdigest()
    return observed == expected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--tasks", type=Path, required=True)
    ap.add_argument("--artifacts", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(args.manifest.read_text())
    task_paths, task_rows = load_tasks(args.tasks)
    raw_paths, records = load_records(args.artifacts)
    task_ids = [row["task_id"] for row in task_rows]
    record_ids = [record.get("task_id") for record in records]
    expected = {row["task_id"]: row for row in task_rows}
    errors = []

    duplicate_tasks = sorted(k for k, v in Counter(task_ids).items() if v > 1)
    duplicate_records = sorted(k for k, v in Counter(record_ids).items() if v > 1)
    missing = sorted(set(task_ids) - set(record_ids))
    unexpected = sorted(set(record_ids) - set(task_ids))
    if len(task_paths) != manifest["shards"] or len(task_rows) != manifest["tasks"]:
        errors.append("frozen manifest/task-grid count mismatch")
    if len(raw_paths) != manifest["shards"]:
        errors.append(f"raw shard count {len(raw_paths)} != {manifest['shards']}")
    if duplicate_tasks or duplicate_records or missing or unexpected:
        errors.append("task identity coverage mismatch")

    spec_mismatches = []
    hash_failures = []
    status_failures = []
    required_field_failures = []
    required_fields = (
        "experiment_id", "benchmark_family", "model_id", "model_hash",
        "bounded_problem_id", "configuration_id", "seed", "replicate_id",
        "commit_sha", "workflow_run_id", "job_id", "shard_id",
        "execution_status", "time_limit_sec", "source_code_hash",
        "configuration_hash", "raw_record_hash", "validation_status",
    )
    for record in records:
        row = expected.get(record.get("task_id"))
        if not row:
            continue
        spec = expected_spec(row)
        expected_configuration_hash = hashlib.sha256(
            json.dumps(spec, sort_keys=True).encode()
        ).hexdigest()
        comparisons = {
            "experiment_id": row["experiment_id"],
            "benchmark_family": row["benchmark_family"],
            "model_id": row["model_id"],
            "bounded_problem_id": row["bounded_problem_id"],
            "configuration_id": row["configuration_id"],
            "seed": int(row["seed"]), "replicate": int(row["replicate"]),
            "L": int(row["L"]), "H": float(row["H"]), "mode": row["mode"],
            "timeout_s": int(row["timeout_s"]), "params": json.loads(row["params"]),
        }
        if any(record.get(key) != value for key, value in comparisons.items()):
            spec_mismatches.append(record["task_id"])
        if record.get("configuration_hash") != expected_configuration_hash or not record_hash_valid(record):
            hash_failures.append(record["task_id"])
        missing_fields = [name for name in required_fields if record.get(name) is None]
        # A timed-out process may still lack a model hash if generation itself timed out.
        if record.get("execution_status") == "TIMEOUT_WALL_CLOCK":
            missing_fields = [name for name in missing_fields if name != "model_hash"]
        if missing_fields:
            required_field_failures.append({"task_id": record["task_id"], "fields": missing_fields})
        status = record.get("execution_status")
        verdict = record.get("verdict")
        analysis = record.get("analysis_status")
        if status == "COMPLETED":
            if verdict not in {"VER", "AMB", "INC"}:
                status_failures.append(record["task_id"])
            if verdict in {"VER", "AMB"} and analysis != "CONCLUSIVE":
                status_failures.append(record["task_id"])
            if verdict == "INC" and (analysis != "INCONCLUSIVE" or not record.get("inconclusive_reason")):
                status_failures.append(record["task_id"])
        elif status == "TIMEOUT_WALL_CLOCK":
            if verdict is not None or analysis != "NOT_RUN" or record.get("runtime_s") is not None:
                status_failures.append(record["task_id"])
            counters = record.get("counters")
            if counters and any(value == 0 for value in counters.values() if value is not None):
                # Zero is allowed only when it is an observed partial counter;
                # the partial_workload flag and failure stage make that explicit.
                if not record.get("partial_workload") or not record.get("failure_stage"):
                    status_failures.append(record["task_id"])
        else:
            status_failures.append(record["task_id"])
    if spec_mismatches:
        errors.append(f"frozen specification mismatches: {len(set(spec_mismatches))}")
    if hash_failures:
        errors.append(f"record/configuration hash failures: {len(set(hash_failures))}")
    if status_failures:
        errors.append(f"status-semantics failures: {len(set(status_failures))}")
    if required_field_failures:
        errors.append(f"required-field failures: {len(required_field_failures)}")

    # Fixed models must remain bit-identical across L and algorithm modes.
    c_rows = [r for r in records if r.get("experiment") == "C_Lsweep" and r.get("model_hash")]
    c_model_mismatches = []
    for key, group in itertools.groupby(sorted(c_rows, key=lambda r: r["model_id"]), key=lambda r: r["model_id"]):
        hashes = {r["model_hash"] for r in group}
        if len(hashes) != 1:
            c_model_mismatches.append(key)
    if c_model_mismatches:
        errors.append(f"C fixed-model hash mismatches: {len(c_model_mismatches)}")

    # WIP changes markings only; static structure must remain paired.
    b_rows = [r for r in records if r.get("experiment") == "B_wip" and r.get("structure_hash")]
    b_structure_mismatches = []
    for key, group in itertools.groupby(sorted(b_rows, key=lambda r: expected[r["task_id"]]["base_model_id"]),
                                          key=lambda r: expected[r["task_id"]]["base_model_id"]):
        if len({r["structure_hash"] for r in group}) != 1:
            b_structure_mismatches.append(key)
    if b_structure_mismatches:
        errors.append(f"B paired structure mismatches: {len(b_structure_mismatches)}")

    # Family-wide L-sensitivity report. Timeouts remain in the denominator.
    sensitivity = {}
    for family in sorted({r.get("benchmark_family") for r in c_rows}):
        family_rows = [r for r in c_rows if r.get("benchmark_family") == family and r.get("mode") == "complete"]
        model_summaries = []
        for model_id, group in itertools.groupby(sorted(family_rows, key=lambda r: (r["model_id"], r["L"])), key=lambda r: r["model_id"]):
            group = list(group)
            values = [(r["L"], (r.get("counters") or {}).get("trace_records")) for r in group]
            observed = [(L, value) for L, value in values if value is not None]
            frontiers = [observed[i][0] for i in range(1, len(observed)) if observed[i][1] > observed[i - 1][1]]
            model_summaries.append({"model_id": model_id, "observed_levels": len(observed), "growth_frontiers": frontiers})
        sensitivity[family] = model_summaries

    semantic_files = sorted(args.artifacts.glob("**/semantic_validation.json"))
    semantic_pass = bool(semantic_files) and all(json.loads(path.read_text()).get("PASS") for path in semantic_files)
    if not semantic_pass:
        errors.append("semantic validation artifact missing or not PASS")

    status_counts = Counter(r.get("execution_status") for r in records)
    verdict_counts = Counter(r.get("verdict") for r in records)
    summary = {
        "PASS": not errors,
        "errors": errors,
        "tasks": len(task_rows), "records": len(records),
        "task_shards": len(task_paths), "raw_shards": len(raw_paths),
        "missing": len(missing), "unexpected": len(unexpected),
        "duplicate_tasks": duplicate_tasks, "duplicate_records": duplicate_records,
        "specification_mismatches": sorted(set(spec_mismatches)),
        "hash_failures": sorted(set(hash_failures)),
        "status_semantics_failures": sorted(set(status_failures)),
        "required_field_failures": required_field_failures,
        "semantic_validation_PASS": semantic_pass,
        "distinct_model_hashes": len({r.get("model_hash") for r in records if r.get("model_hash")}),
        "distinct_bounded_problems": len({r.get("bounded_problem_id") for r in records}),
        "distinct_configurations": len({r.get("configuration_id") for r in records}),
        "execution_status": dict(status_counts),
        "verdict": {str(k): v for k, v in verdict_counts.items()},
        "C_fixed_model_mismatches": c_model_mismatches,
        "B_structure_mismatches": b_structure_mismatches,
        "L_sensitivity": sensitivity,
    }
    (args.out / "integrity_validation.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    with (args.out / "merged_results.jsonl").open("w") as fh:
        for record in sorted(records, key=lambda r: r["task_id"]):
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    print(json.dumps({k: summary[k] for k in (
        "PASS", "errors", "tasks", "records", "raw_shards",
        "distinct_model_hashes", "distinct_bounded_problems",
        "distinct_configurations", "execution_status", "verdict",
    )}, indent=2))
    return 0 if summary["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
