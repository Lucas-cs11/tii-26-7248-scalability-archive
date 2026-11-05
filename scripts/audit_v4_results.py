#!/usr/bin/env python3
"""Independent completeness and semantics audit for v4 GitHub artifacts."""
from __future__ import annotations

import argparse
import csv
import itertools
import json
from collections import Counter
from pathlib import Path


COUNTERS = (
    "scg_classes", "scg_edges", "sync_states", "candidate_words",
    "temporal_states", "trace_records", "terminal_pairs", "lp_calls",
)


def load_expected(tasks_dir: Path):
    rows = []
    paths = sorted(tasks_dir.glob("shard_*.csv"))
    for path in paths:
        with path.open(newline="") as fh:
            rows.extend(csv.DictReader(fh))
    duplicate_ids = sorted(
        task_id for task_id, count in Counter(row["task_id"] for row in rows).items()
        if count > 1
    )
    return {row["task_id"]: row for row in rows}, len(rows), len(paths), duplicate_ids


def load_records(artifacts: Path):
    records = []
    paths = sorted(artifacts.glob("**/*.jsonl"))
    for path in paths:
        for line_no, line in enumerate(path.open(), 1):
            rec = json.loads(line)
            rec["source_shard_file"] = path.name
            rec["source_line"] = line_no
            records.append(rec)
    return paths, records


def task_spec_mismatches(expected, records):
    """Return task IDs whose recorded cell differs from the frozen CSV row."""
    mismatches = []
    for rec in records:
        task_id = rec.get("task_id")
        row = expected.get(task_id)
        if row is None:
            continue
        recorded = {
            "experiment": str(rec.get("experiment")),
            "model": str(rec.get("model")),
            "seed": str(rec.get("seed")),
            "replicate": str(rec.get("replicate")),
            "L": str(rec.get("L")),
            "H": str(float(rec.get("H"))),
            "mode": str(rec.get("mode")),
            # The executed v4 timeout records predate explicit timeout_s
            # emission. The runner used the frozen CSV value; verify any
            # recorded value and otherwise bind it to the declared row.
            "timeout_s": str(int(
                rec.get("timeout_s") if rec.get("timeout_s") is not None
                else row["timeout_s"]
            )),
            "params": json.dumps(rec.get("params"), sort_keys=True),
        }
        declared = {
            **{key: row[key] for key in ("experiment", "model", "seed", "replicate", "L", "mode")},
            "H": str(float(row["H"])),
            "timeout_s": str(int(row["timeout_s"])),
            "params": json.dumps(json.loads(row["params"]), sort_keys=True),
        }
        if recorded != declared:
            mismatches.append(task_id)
    return sorted(mismatches)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=Path, required=True)
    ap.add_argument("--artifacts", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    expected, expected_rows, task_shards, expected_duplicates = load_expected(args.tasks)
    shard_paths, records = load_records(args.artifacts)
    ids = [r.get("task_id") for r in records]
    duplicate_ids = sorted(k for k, v in Counter(ids).items() if v > 1)
    missing = sorted(set(expected) - set(ids))
    unexpected = sorted(set(ids) - set(expected))
    errors = []

    manifest_path = args.tasks.parent / "v4_formal_manifest.json"
    if not manifest_path.is_file():
        errors.append(f"missing frozen manifest: {manifest_path}")
        declared_tasks, declared_shards = 465, 60
    else:
        manifest = json.loads(manifest_path.read_text())
        declared_tasks = int(manifest["tasks"])
        declared_shards = int(manifest["shards"])
    if task_shards != declared_shards or expected_rows != declared_tasks:
        errors.append(
            f"frozen task grid mismatch: {task_shards}/{declared_shards} shards, "
            f"{expected_rows}/{declared_tasks} rows"
        )
    if expected_duplicates:
        errors.append(f"duplicate frozen task IDs: {len(expected_duplicates)}")
    if len(shard_paths) != 60:
        errors.append(f"expected 60 raw shard files, found {len(shard_paths)}")
    if missing or unexpected or duplicate_ids:
        errors.append("task identity mismatch")
    spec_mismatches = task_spec_mismatches(expected, records)
    if spec_mismatches:
        errors.append(f"frozen task specification mismatches: {len(spec_mismatches)}")
    for rec in records:
        row = expected.get(rec.get("task_id"))
        rec["raw_timeout_s_present"] = rec.get("timeout_s") is not None
        rec["declared_timeout_s"] = int(row["timeout_s"]) if row else None

    allowed_execution = {"COMPLETED", "TIMEOUT", "ERROR", "VALIDATION_FAILURE"}
    for rec in records:
        status = rec.get("execution_status")
        verdict = rec.get("verdict")
        analysis = rec.get("analysis_status")
        reason = rec.get("inconclusive_reason")
        if status not in allowed_execution:
            errors.append(f"{rec.get('task_id')}: unknown execution status {status}")
        if status in {"ERROR", "VALIDATION_FAILURE"}:
            errors.append(f"{rec.get('task_id')}: unsuccessful execution status {status}")
        if status == "COMPLETED" and verdict not in {"VER", "AMB", "INC"}:
            errors.append(f"{rec.get('task_id')}: completed without a valid verdict")
        if status == "TIMEOUT" and (verdict is not None or analysis != "NOT_RUN"):
            errors.append(f"{rec.get('task_id')}: TIMEOUT assigned a verdict")
        if status == "TIMEOUT" and reason is not None:
            errors.append(f"{rec.get('task_id')}: TIMEOUT assigned an internal reason")
        if verdict == "INC" and not (
            status == "COMPLETED" and analysis == "INCONCLUSIVE" and reason
        ):
            errors.append(f"{rec.get('task_id')}: malformed INC record")
        if verdict in {"VER", "AMB"} and analysis != "CONCLUSIVE":
            errors.append(f"{rec.get('task_id')}: conclusive verdict/status mismatch")
        if verdict in {"VER", "AMB"} and reason is not None:
            errors.append(f"{rec.get('task_id')}: conclusive verdict has an INC reason")
        if status == "COMPLETED" and not rec.get("counters"):
            errors.append(f"{rec.get('task_id')}: completed record lacks counters")

    # Repeats must reproduce all deterministic workload counters and model hash.
    rep_mismatches = []
    key_fn = lambda r: (
        r["experiment"], r["model"], r["seed"], r["L"], r["H"], r["mode"]
    )
    for key, group in itertools.groupby(sorted(records, key=key_fn), key=key_fn):
        group = list(group)
        hashes = {r.get("model_hash") for r in group if r.get("model_hash")}
        counter_sets = {
            tuple((name, (r.get("counters") or {}).get(name)) for name in COUNTERS)
            for r in group if r.get("counters")
        }
        if len(hashes) > 1 or len(counter_sets) > 1:
            rep_mismatches.append(str(key))
    if rep_mismatches:
        errors.append(f"replicate invariant failures: {len(rep_mismatches)}")

    # L must be the sole active bound within a fixed model and introduce new
    # histories at three or more consecutive frontiers for every formal model.
    l_frontiers = {}
    l_records = sorted(
        (r for r in records if r["experiment"] == "Lsweep" and r["replicate"] == 0),
        key=lambda r: (r["model"], r["seed"], r["L"]),
    )
    for key, group in itertools.groupby(l_records, key=lambda r: (r["model"], r["seed"])):
        group = list(group)
        hashes = {r["model_hash"] for r in group}
        values = [(r["L"], r["counters"]["candidate_words"]) for r in group]
        frontiers = [
            values[i][0] for i in range(1, len(values))
            if values[i][1] > values[i - 1][1]
        ]
        l_frontiers[f"{key[0]}:seed{key[1]}"] = frontiers
        if len(hashes) != 1 or len(frontiers) < 3:
            errors.append(f"L-sweep acceptance failed for {key}")

    # Structural x coordinates must be strictly increasing in declared order.
    size_values = {}
    for model in [f"S{i}" for i in range(1, 8)]:
        cell = next(r for r in records if r["experiment"] == "size" and r["model"] == model)
        size_values[model] = cell["n_places"] + cell["n_transitions"]
    if list(size_values.values()) != sorted(set(size_values.values())):
        errors.append("structural sizes are not strictly increasing")

    # Raw merged files.
    with (args.out / "merged_results.jsonl").open("w") as fh:
        for rec in sorted(records, key=lambda r: r["task_id"]):
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    flat_fields = [
        "task_id", "experiment", "model", "seed", "replicate", "L", "H", "mode",
        "execution_status", "analysis_status", "verdict", "inconclusive_reason",
        "runtime_s", "wall_s", "timeout_s", "declared_timeout_s",
        "raw_timeout_s_present", "model_hash", "n_places",
        "n_transitions", "n_arcs", *COUNTERS, "source_shard_file", "source_line",
    ]
    with (args.out / "merged_results.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=flat_fields)
        writer.writeheader()
        for rec in sorted(records, key=lambda r: r["task_id"]):
            row = {name: rec.get(name) for name in flat_fields}
            if row["timeout_s"] is None:
                row["timeout_s"] = expected[rec["task_id"]]["timeout_s"]
            for name in COUNTERS:
                row[name] = (rec.get("counters") or {}).get(name)
            writer.writerow(row)

    summary = {
        "PASS": not errors,
        "errors": errors,
        "tasks_expected": len(expected),
        "frozen_task_rows": expected_rows,
        "frozen_task_shards": task_shards,
        "duplicate_frozen_task_ids": expected_duplicates,
        "records": len(records),
        "raw_shard_files": len(shard_paths),
        "unique_task_ids": len(set(ids)),
        "missing": len(missing),
        "unexpected": len(unexpected),
        "duplicates": len(duplicate_ids),
        "task_specification_mismatches": spec_mismatches,
        "execution_status": dict(Counter(r.get("execution_status") for r in records)),
        "analysis_status": dict(Counter(r.get("analysis_status") for r in records)),
        "verdicts": {str(k): v for k, v in Counter(r.get("verdict") for r in records).items()},
        "inconclusive_reasons": {
            str(k): v for k, v in Counter(r.get("inconclusive_reason") for r in records).items()
        },
        "by_experiment": dict(Counter(r["experiment"] for r in records)),
        "replicate_invariant_failures": rep_mismatches,
        "L_candidate_frontiers": l_frontiers,
        "structural_sizes": size_values,
    }
    (args.out / "validation.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0 if summary["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
