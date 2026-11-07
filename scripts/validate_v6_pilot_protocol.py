#!/usr/bin/env python3
"""Independently validate the frozen v6 Pilot task grid before execution."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.run_cell import model_hash, structure_hash
from src.benchmarks.v6_families import FAMILY_SPECS, generate_v6_model


SHARD = re.compile(r"shard_[0-9]{3}\.csv\Z")


def main():
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
        errors.append("shard count mismatch")
    if len(rows) != manifest["tasks"]:
        errors.append("task count mismatch")
    for key in ("task_id", "configuration_id"):
        values = [row[key] for row in rows]
        if len(values) != len(set(values)):
            errors.append(f"duplicate {key}")
    if len({row["model_id"] for row in rows}) != manifest["distinct_model_hashes"]:
        errors.append("distinct model count mismatch")
    if len({row["bounded_problem_id"] for row in rows}) != manifest["distinct_bounded_problems"]:
        errors.append("distinct bounded-problem count mismatch")
    per_family = {family: sum(row["benchmark_family"] == family for row in rows)
                  for family in FAMILY_SPECS}
    if any(count != manifest["tasks_per_family"] for count in per_family.values()):
        errors.append(f"family task counts differ: {per_family}")

    generated = {}
    for row in rows:
        model_id = row["model_id"]
        params = json.loads(row["params"])
        if model_id not in generated:
            tpn, _, _, _ = generate_v6_model(**params)
            generated[model_id] = (model_hash(tpn), structure_hash(tpn))
        full_hash, struct_hash = generated[model_id]
        if full_hash != row["expected_model_hash"] or struct_hash != row["expected_structure_hash"]:
            errors.append(f"frozen hash mismatch: {row['task_id']}")
        if row["protocol_version"] != manifest["version"]:
            errors.append(f"protocol version mismatch: {row['task_id']}")
        if int(row["replicate"]) != 0:
            errors.append(f"timing replicate present: {row['task_id']}")

    result = {
        "PASS": not errors,
        "errors": sorted(set(errors)),
        "tasks": len(rows),
        "shards": len(paths),
        "families": len(per_family),
        "distinct_model_hashes": len({row["expected_model_hash"] for row in rows}),
        "distinct_bounded_problems": len({row["bounded_problem_id"] for row in rows}),
        "unique_configurations": len({row["configuration_id"] for row in rows}),
        "duplicate_configurations": len(rows) - len({row["configuration_id"] for row in rows}),
        "semantics_source_sha": semantics.get("semantics_source_sha"),
        "semantics_run_id": semantics.get("github_run_id"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
