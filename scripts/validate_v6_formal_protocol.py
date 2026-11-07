#!/usr/bin/env python3
"""Independently validate the frozen v6 full-scale task grid before execution."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.run_cell import model_hash, structure_hash  # noqa: E402
from src.benchmarks.v6_families import FAMILY_SPECS, generate_v6_model  # noqa: E402

SHARD = re.compile(r"shard_[0-9]{3}\.csv\Z")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--semantics-gate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    semantics = json.loads(args.semantics_gate.read_text())
    paths = sorted(path for path in args.tasks.glob("*.csv") if SHARD.fullmatch(path.name))
    rows = [row for path in paths for row in csv.DictReader(path.open())]
    errors = []
    if not semantics.get("PASS"):
        errors.append("semantics gate is not PASS")
    if len(paths) != manifest["shards"]:
        errors.append(f"shard count {len(paths)} != {manifest['shards']}")
    if len(rows) != manifest["tasks"]:
        errors.append(f"task count {len(rows)} != {manifest['tasks']}")
    for key in ("task_id", "configuration_id"):
        values = [row[key] for row in rows]
        if len(values) != len(set(values)):
            errors.append(f"duplicate {key}")
    if any(row["task_id"] != row["configuration_id"] for row in rows):
        errors.append("task_id != configuration_id")
    quota_to_experiment = {
        "structural": "A_structural",
        "wip": "B_wip",
        "lsweep": "C_Lsweep",
        "difficult": "D_difficult",
    }
    per_experiment = {
        name: sum(row["experiment"] == name for row in rows) for name in quota_to_experiment.values()
    }
    for quota_name, quota in manifest["quota"].items():
        experiment = quota_to_experiment[quota_name]
        if per_experiment[experiment] != quota:
            errors.append(
                f"quota mismatch for {quota_name}: {per_experiment[experiment]} != {quota}"
            )
    if len({row["model_id"] for row in rows}) != manifest["distinct_model_hashes"]:
        errors.append("distinct model count mismatch")
    if len({row["bounded_problem_id"] for row in rows}) != manifest["distinct_bounded_problems"]:
        errors.append("distinct bounded-problem count mismatch")
    per_shard = {path.name: 0 for path in paths}
    for row in rows:
        per_shard_key = f"shard_{int(row.get('shard_hint', 0)):03d}.csv"
    for path in paths:
        count = sum(1 for _ in csv.DictReader(path.open()))
        if count != manifest["tasks_per_shard"]:
            errors.append(f"{path.name}: {count} tasks != {manifest['tasks_per_shard']}")
    generated = {}
    for row in rows:
        params = json.loads(row["params"])
        cache_key = json.dumps(params, sort_keys=True)
        if cache_key not in generated:
            tpn, _, _, _ = generate_v6_model(**params)
            generated[cache_key] = (model_hash(tpn), structure_hash(tpn))
        full_hash, struct_hash = generated[cache_key]
        if full_hash != row["expected_model_hash"]:
            errors.append(f"model hash mismatch: {row['task_id']}")
            break
        if struct_hash != row["expected_structure_hash"]:
            errors.append(f"structure hash mismatch: {row['task_id']}")
            break
        if int(row["timeout_s"]) != manifest["cell_timeout_seconds"]:
            errors.append(f"timeout mismatch: {row['task_id']}")
            break
        if row["protocol_version"] != manifest["version"]:
            errors.append(f"protocol version mismatch: {row['task_id']}")
            break
        if int(row["replicate"]) != 0:
            errors.append(f"unexpected replicate: {row['task_id']}")
            break
    result = {
        "PASS": not errors,
        "errors": sorted(set(errors)),
        "tasks": len(rows),
        "shards": len(paths),
        "tasks_per_shard": manifest["tasks_per_shard"],
        "families": len(FAMILY_SPECS),
        "experiments": per_experiment,
        "distinct_model_hashes": len({row["expected_model_hash"] for row in rows}),
        "distinct_bounded_problems": len({row["bounded_problem_id"] for row in rows}),
        "unique_configurations": len({row["configuration_id"] for row in rows}),
        "semantics_source_sha": semantics.get("semantics_source_sha"),
        "semantics_run_id": semantics.get("github_run_id"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
