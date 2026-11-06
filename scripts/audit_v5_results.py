#!/usr/bin/env python3
"""Independent identity, provenance, semantics, and workload audit for v5."""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


SHARD_NAME = re.compile(r"shard_[0-9]{3}\.csv\Z")
RAW_SHARD_NAME = re.compile(r"shard_[0-9]{3}\.jsonl\Z")
ARTIFACT_NAME = re.compile(r"v5-formal(?:-retry)?-[0-9]+-[0-9]+\Z")


def load_tasks(tasks_dir: Path):
    all_paths = sorted(tasks_dir.glob("*.csv"))
    unexpected = [path.name for path in all_paths if not SHARD_NAME.fullmatch(path.name)]
    if unexpected:
        raise ValueError(f"unexpected task CSV filenames: {unexpected}")
    paths = [path for path in all_paths if SHARD_NAME.fullmatch(path.name)]
    rows = []
    for path in paths:
        with path.open(newline="") as fh:
            rows.extend(csv.DictReader(fh))
    return paths, rows


def load_records(artifacts: Path):
    selection_path = artifacts / "artifact_selection.json"
    selection = json.loads(selection_path.read_text()) if selection_path.is_file() else None
    if selection:
        names = selection.get("selected_artifacts") or []
        if (len(names) != 120 or len(names) != len(set(names))
                or any(not ARTIFACT_NAME.fullmatch(name) for name in names)):
            raise ValueError("artifact selection must name 120 unique validated artifact directories")
        paths = []
        for name in names:
            candidates = sorted((artifacts / name / "raw").glob("*.jsonl"))
            if len(candidates) != 1 or not RAW_SHARD_NAME.fullmatch(candidates[0].name):
                raise ValueError(f"selected artifact {name} does not contain exactly one canonical raw shard")
            paths.extend(candidates)
        paths.sort()
    else:
        all_raw = sorted(artifacts.glob("**/raw/*.jsonl"))
        unexpected = [str(path.relative_to(artifacts)) for path in all_raw if not RAW_SHARD_NAME.fullmatch(path.name)]
        if unexpected:
            raise ValueError(f"unexpected raw shard filenames: {unexpected}")
        paths = all_raw
    records = []
    for path in paths:
        for line_number, line in enumerate(path.open(), 1):
            record = json.loads(line)
            record["audit_source_path"] = str(path.relative_to(artifacts))
            record["audit_source_line"] = line_number
            records.append(record)
    return paths, records, selection


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


def timeout_semantics_valid(record):
    counters = record.get("counters")
    validation = record.get("validation_status")
    if (record.get("verdict") is not None
            or record.get("analysis_status") != "NOT_RUN"
            or record.get("runtime_s") is not None
            or record.get("termination_status") != "TIMEOUT_WALL_CLOCK"
            or not record.get("failure_stage")
            or record.get("partial_workload") != bool(counters)
            or validation not in {"NOT_RUN", "PASS"}):
        return False
    if record.get("failure_stage") == "MODEL_GENERATION" and validation != "NOT_RUN":
        return False
    return True


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
    raw_paths, records, artifact_selection = load_records(args.artifacts)
    task_ids = [row["task_id"] for row in task_rows]
    record_ids = [record.get("task_id") for record in records]
    expected = {row["task_id"]: row for row in task_rows}
    errors = []
    formal = str(manifest.get("version", "")).startswith("v5-formal")

    protocol_files = sorted(args.artifacts.glob("**/formal_protocol_validation.json"))
    protocol_validation = None
    expected_models = {}
    if formal:
        if len(protocol_files) != 1:
            errors.append(f"expected exactly one formal protocol validation, found {len(protocol_files)}")
        else:
            protocol_validation = json.loads(protocol_files[0].read_text())
            if not protocol_validation.get("PASS"):
                errors.append("formal protocol validation is not PASS")
            expected_models = protocol_validation.get("models") or {}
            if len(expected_models) != protocol_validation.get("declared_model_ids"):
                errors.append("formal protocol validation model manifest is incomplete")

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
        expected_model = expected_models.get(record.get("model_id")) if formal else None
        if expected_model and record.get("model_hash") not in (None, expected_model.get("model_hash")):
            spec_mismatches.append(record["task_id"])
        if status == "COMPLETED":
            if verdict not in {"VER", "AMB", "INC"}:
                status_failures.append(record["task_id"])
            if verdict in {"VER", "AMB"} and analysis != "CONCLUSIVE":
                status_failures.append(record["task_id"])
            if verdict == "INC" and (analysis != "INCONCLUSIVE" or not record.get("inconclusive_reason")):
                status_failures.append(record["task_id"])
        elif status == "TIMEOUT_WALL_CLOCK":
            if not timeout_semantics_valid(record):
                status_failures.append(record["task_id"])
        else:
            status_failures.append(record["task_id"])
        if formal:
            job_id = str(record.get("job_id") or "")
            if not job_id.isdigit():
                required_field_failures.append({
                    "task_id": record["task_id"], "fields": ["numeric_job_id"]
                })
            if record.get("source_code_hash") != record.get("commit_sha"):
                required_field_failures.append({
                    "task_id": record["task_id"], "fields": ["source_code_hash_match"]
                })
            prefixes = (
                f"v5-formal-{record.get('shard_id')}-{record.get('workflow_run_id')}/raw/",
                f"v5-formal-retry-{record.get('shard_id')}-{record.get('workflow_run_id')}/raw/",
            )
            if not str(record.get("raw_artifact") or "").startswith(prefixes):
                required_field_failures.append({
                    "task_id": record["task_id"], "fields": ["formal_raw_artifact_path"]
                })
    if spec_mismatches:
        errors.append(f"frozen specification mismatches: {len(set(spec_mismatches))}")
    if hash_failures:
        errors.append(f"record/configuration hash failures: {len(set(hash_failures))}")
    if status_failures:
        errors.append(f"status-semantics failures: {len(set(status_failures))}")
    if required_field_failures:
        errors.append(f"required-field failures: {len(required_field_failures)}")

    run_ids = {str(r.get("workflow_run_id")) for r in records if r.get("workflow_run_id")}
    commit_shas = {r.get("commit_sha") for r in records if r.get("commit_sha")}
    authorized_run_ids = (
        set(map(str, artifact_selection.get("authorized_record_run_ids", [])))
        if artifact_selection else set()
    )
    if formal:
        if artifact_selection:
            if (run_ids - authorized_run_ids or len(commit_shas) != 1
                    or commit_shas != {artifact_selection.get("formal_commit_sha")}):
                errors.append(
                    f"formal/retry provenance exceeds selection: runs={run_ids}, "
                    f"authorized={authorized_run_ids}, commits={commit_shas}"
                )
        elif len(run_ids) != 1 or len(commit_shas) != 1:
            errors.append(f"formal raw provenance is not one run/commit: runs={run_ids}, commits={commit_shas}")

    # Fixed models remain bit-identical even when a runtime record timed out:
    # the pre-execution full model manifest supplies the frozen expected hash.
    c_rows = [r for r in records if r.get("experiment") == "C_Lsweep"]
    c_model_mismatches = []
    for key, group in itertools.groupby(sorted(c_rows, key=lambda r: r["model_id"]), key=lambda r: r["model_id"]):
        group = list(group)
        hashes = {r["model_hash"] for r in group if r.get("model_hash")}
        expected_hash = (expected_models.get(key) or {}).get("model_hash")
        if len(hashes) > 1 or (expected_hash and any(value != expected_hash for value in hashes)):
            c_model_mismatches.append(key)
    if c_model_mismatches:
        errors.append(f"C fixed-model hash mismatches: {len(c_model_mismatches)}")

    # WIP changes markings only; static structure must remain paired.
    b_rows = [r for r in records if r.get("experiment") == "B_wip"]
    b_structure_mismatches = []
    for key, group in itertools.groupby(sorted(b_rows, key=lambda r: expected[r["task_id"]]["base_model_id"]),
                                          key=lambda r: expected[r["task_id"]]["base_model_id"]):
        group = list(group)
        observed = {r["structure_hash"] for r in group if r.get("structure_hash")}
        expected_structures = {
            expected_models[r["model_id"]]["structure_hash"]
            for r in group if r["model_id"] in expected_models
        }
        if len(group) != 3 or len(observed) > 1 or (expected_structures and len(expected_structures) != 1):
            b_structure_mismatches.append(key)
    if b_structure_mismatches:
        errors.append(f"B paired structure mismatches: {len(b_structure_mismatches)}")

    # Every declared level and status stays in the denominator; workload
    # frontiers use only observed checkpoint/completed counters.
    sensitivity = {}
    declared_families = sorted(manifest.get("families") or {r.get("benchmark_family") for r in c_rows})
    for mode in ("first", "complete"):
        sensitivity[mode] = {}
        for family in declared_families:
            family_rows = [
                r for r in c_rows
                if r.get("benchmark_family") == family and r.get("mode") == mode
            ]
            model_summaries = []
            for model_id, group in itertools.groupby(
                    sorted(family_rows, key=lambda r: (r["model_id"], r["L"])),
                    key=lambda r: r["model_id"]):
                group = list(group)
                values = [(r["L"], (r.get("counters") or {}).get("trace_records")) for r in group]
                observed = [(L, value) for L, value in values if value is not None]
                frontiers = [
                    observed[i][0] for i in range(1, len(observed))
                    if observed[i][1] > observed[i - 1][1]
                ]
                model_summaries.append({
                    "model_id": model_id,
                    "declared_levels": len(group),
                    "declared_L": [value[0] for value in values],
                    "execution_status": dict(Counter(r.get("execution_status") for r in group)),
                    "observed_workload_levels": len(observed),
                    "growth_frontiers": frontiers,
                })
            sensitivity[mode][family] = model_summaries

    semantic_files = sorted(args.artifacts.glob("**/semantic_validation.json"))
    semantic_payloads = [json.loads(path.read_text()) for path in semantic_files]
    semantic_pass = bool(semantic_payloads) and all(payload.get("PASS") for payload in semantic_payloads)
    if formal:
        if len(semantic_files) != 1:
            semantic_pass = False
        raw_run_id = (
            str(artifact_selection.get("formal_run_id"))
            if artifact_selection else next(iter(run_ids), None)
        )
        raw_commit = next(iter(commit_shas), None)
        if semantic_payloads:
            semantic_pass = semantic_pass and all(
                str(payload.get("workflow_run_id")) == raw_run_id
                and payload.get("commit_sha") == raw_commit
                for payload in semantic_payloads
            )
        if protocol_validation:
            manifest_digest = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
            semantic_pass = semantic_pass and (
                str(protocol_validation.get("workflow_run_id")) == raw_run_id
                and protocol_validation.get("commit_sha") == raw_commit
                and protocol_validation.get("frozen_manifest_sha256") == manifest_digest
            )
        if raw_run_id:
            expected_validation_dir = f"v5-formal-semantic-{raw_run_id}"
            semantic_pass = semantic_pass and all(
                path.parent.name == expected_validation_dir
                for path in semantic_files + protocol_files
            )
    if not semantic_pass:
        errors.append("semantic/protocol validation provenance missing, mismatched, or not PASS")

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
        "formal_protocol_validation_PASS": (
            protocol_validation.get("PASS") if protocol_validation else None
        ),
        "workflow_run_ids": sorted(run_ids),
        "commit_shas": sorted(commit_shas),
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
            merged = dict(record)
            merged["base_model_id"] = expected[record["task_id"]]["base_model_id"]
            fh.write(json.dumps(merged, sort_keys=True) + "\n")
    print(json.dumps({k: summary[k] for k in (
        "PASS", "errors", "tasks", "records", "raw_shards",
        "distinct_model_hashes", "distinct_bounded_problems",
        "distinct_configurations", "execution_status", "verdict",
    )}, indent=2))
    return 0 if summary["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
