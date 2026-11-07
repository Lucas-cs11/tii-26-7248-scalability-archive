#!/usr/bin/env python3
"""Audit and normalize the completed v6 Pilot without altering raw artifacts."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path


LATER_KEYS = (
    "sync_states", "candidate_words", "candidate_words_processed",
    "temporal_states", "trace_records", "terminal_pairs", "lp_calls",
    "runtime_sync", "runtime_expansion", "runtime_lp",
)


def raw_hash(record):
    payload = dict(record)
    expected = payload.pop("raw_record_hash", None)
    actual = hashlib.sha256(
        json.dumps(payload, default=str, sort_keys=True).encode()
    ).hexdigest()
    return expected, actual


def normalized_counters(record):
    counters = dict(record.get("counters") or {})
    if record.get("execution_status") != "TIMEOUT_WALL_CLOCK":
        return counters or None
    stage = record.get("failure_stage")
    if stage in {"MODEL_GENERATION", "MODEL_GENERATED", "MODEL_VALIDATED"}:
        return None
    if stage == "SCG_COMPLETE":
        for key in LATER_KEYS:
            counters[key] = None
    elif stage == "SYNCHRONIZER_COMPLETE":
        for key in (
            "temporal_states", "trace_records", "terminal_pairs", "lp_calls",
            "runtime_expansion", "runtime_lp",
        ):
            counters[key] = None
    elif stage == "HISTORY_ANALYSIS_PARTIAL":
        # Work counters were checkpointed, but cumulative timers were only
        # assigned after the loop in this Pilot source version.
        counters["runtime_expansion"] = None
        counters["runtime_lp"] = None
    return counters


def percentile(values, probability):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(probability * len(ordered)) - 1)]


def summary_row(group_name, group_value, records):
    walls = [float(record["wall_s"]) for record in records]
    rss = [float(record["peak_rss_mb"]) for record in records]
    statuses = Counter(record.get("execution_status") for record in records)
    verdicts = Counter(str(record.get("verdict")) for record in records)
    return {
        "group": group_name,
        "value": group_value,
        "tasks": len(records),
        "completed": statuses["COMPLETED"],
        "timeout": statuses["TIMEOUT_WALL_CLOCK"],
        "inc": verdicts["INC"],
        "ver": verdicts["VER"],
        "amb": verdicts["AMB"],
        "timeout_fraction": statuses["TIMEOUT_WALL_CLOCK"] / len(records),
        "inc_fraction": verdicts["INC"] / len(records),
        "wall_p50_s": percentile(walls, 0.50),
        "wall_p90_s": percentile(walls, 0.90),
        "wall_p95_s": percentile(walls, 0.95),
        "peak_rss_p50_mb": percentile(rss, 0.50),
        "peak_rss_p90_mb": percentile(rss, 0.90),
        "peak_rss_p95_mb": percentile(rss, 0.95),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(args.manifest.read_text())
    task_rows = [
        row for path in sorted(args.tasks.glob("shard_*.csv"))
        for row in csv.DictReader(path.open())
    ]
    tasks = {row["task_id"]: row for row in task_rows}
    raw_paths = sorted(args.artifacts.glob("*pilot-*-*/raw/shard_*.jsonl"))
    records = [
        json.loads(line) for path in raw_paths for line in path.read_text().splitlines()
        if line.strip()
    ]
    errors = []
    findings = []
    if len(raw_paths) != manifest["shards"]:
        errors.append(f"expected {manifest['shards']} raw shards, found {len(raw_paths)}")
    if len(records) != manifest["tasks"]:
        errors.append(f"expected {manifest['tasks']} records, found {len(records)}")
    by_id = defaultdict(list)
    for record in records:
        by_id[record.get("task_id")].append(record)
    missing = sorted(set(tasks) - set(by_id))
    duplicate = sorted(task_id for task_id, group in by_id.items() if len(group) != 1)
    unexpected = sorted(set(by_id) - set(tasks))
    if missing:
        errors.append(f"missing task IDs: {missing}")
    if duplicate:
        errors.append(f"non-unique final records: {duplicate}")
    if unexpected:
        errors.append(f"unexpected task IDs: {unexpected}")

    audited = []
    raw_zero_findings = 0
    for record in records:
        task = tasks.get(record.get("task_id"))
        if task is None:
            continue
        comparisons = {
            "protocol_version": task["protocol_version"],
            "experiment": task["experiment"],
            "benchmark_family": task["benchmark_family"],
            "model_id": task["model_id"],
            "bounded_problem_id": task["bounded_problem_id"],
            "configuration_id": task["configuration_id"],
            "seed": int(task["seed"]),
            "replicate": int(task["replicate"]),
            "L": int(task["L"]),
            "H": float(task["H"]),
            "mode": task["mode"],
            "timeout_s": int(task["timeout_s"]),
            "model_hash": task["expected_model_hash"],
            "structure_hash": task["expected_structure_hash"],
        }
        for key, expected in comparisons.items():
            if record.get(key) != expected:
                errors.append(f"{record['task_id']}: {key} differs from frozen task")
        if record.get("params") != json.loads(task["params"]):
            errors.append(f"{record['task_id']}: params differ from frozen task")
        if str(record.get("workflow_run_id")) != str(args.run_id):
            errors.append(f"{record['task_id']}: workflow run ID mismatch")
        if record.get("commit_sha") != args.source_sha:
            errors.append(f"{record['task_id']}: source SHA mismatch")
        if not str(record.get("job_id", "")).isdigit():
            errors.append(f"{record['task_id']}: job ID is not numeric")
        if record.get("peak_rss_mb") is None or record["peak_rss_mb"] <= 0:
            errors.append(f"{record['task_id']}: Peak RSS missing or invalid")
        expected_hash, actual_hash = raw_hash(record)
        if expected_hash != actual_hash:
            errors.append(f"{record['task_id']}: raw record hash mismatch")
        status = record.get("execution_status")
        if status not in {"COMPLETED", "TIMEOUT_WALL_CLOCK"}:
            errors.append(f"{record['task_id']}: unexpected execution status {status}")
        if status == "COMPLETED" and record.get("verdict") not in {"VER", "AMB", "INC"}:
            errors.append(f"{record['task_id']}: completed record lacks valid verdict")
        if status == "TIMEOUT_WALL_CLOCK" and record.get("verdict") is not None:
            errors.append(f"{record['task_id']}: timeout has a verdict")

        counters = record.get("counters") or {}
        if status == "TIMEOUT_WALL_CLOCK" and record.get("failure_stage") == "SCG_COMPLETE":
            if any(counters.get(key) == 0 for key in LATER_KEYS):
                raw_zero_findings += 1
        normalized = dict(record)
        normalized.update({
            "stratum": task["stratum"],
            "base_model_id": task["base_model_id"],
            "audited_counters": normalized_counters(record),
        })
        audited.append(normalized)

    validation_files = list(args.artifacts.glob(
        "*pilot-validation-*/v6_pilot_protocol_validation.json"
    ))
    if len(validation_files) != 1:
        errors.append(f"expected one protocol-validation artifact, found {len(validation_files)}")
    elif not json.loads(validation_files[0].read_text()).get("PASS"):
        errors.append("GitHub protocol validation is not PASS")
    if raw_zero_findings:
        findings.append({
            "severity": "instrumentation",
            "records": raw_zero_findings,
            "issue": "SCG_COMPLETE timeout checkpoints used zero for unexecuted later stages",
            "treatment": "raw artifacts retained; audited_counters replaces those values with null",
        })

    audited.sort(key=lambda record: record["task_id"])
    with (args.out / "audited_records.jsonl").open("w") as handle:
        for record in audited:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    rows = []
    for group_name in ("experiment", "benchmark_family", "mode"):
        for value in sorted({record.get(group_name) for record in audited}):
            rows.append(summary_row(
                group_name, value,
                [record for record in audited if record.get(group_name) == value],
            ))
    with (args.out / "pilot_statistics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    status_counts = Counter(record.get("execution_status") for record in audited)
    verdict_counts = Counter(str(record.get("verdict")) for record in audited)
    total_wall = sum(float(record["wall_s"]) for record in audited)
    result = {
        "PASS": not errors,
        "acceptance": "PASS_WITH_INSTRUMENTATION_LIMITATION" if not errors and findings else (
            "PASS" if not errors else "FAIL"
        ),
        "errors": sorted(set(errors)),
        "findings": findings,
        "tasks": len(audited),
        "raw_shards": len(raw_paths),
        "distinct_model_hashes": len({record.get("model_hash") for record in audited}),
        "distinct_bounded_problems": len({record.get("bounded_problem_id") for record in audited}),
        "execution_status": dict(status_counts),
        "verdict": dict(verdict_counts),
        "peak_rss_missing": sum(record.get("peak_rss_mb") is None for record in audited),
        "peak_rss_max_mb": max(record["peak_rss_mb"] for record in audited),
        "observed_runner_minutes_compute_only": total_wall / 60,
        "artifact_bytes_downloaded": sum(
            path.stat().st_size for path in args.artifacts.rglob("*") if path.is_file()
        ),
        "formal_gate": {
            "PILOT_BUDGET_AND_MEMORY_PASS": False,
            "reason": "timeout fraction requires optimization/budget reassessment before formal freeze",
        },
    }
    (args.out / "pilot_audit.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
