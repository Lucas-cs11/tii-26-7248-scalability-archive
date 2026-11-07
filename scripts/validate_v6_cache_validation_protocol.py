#!/usr/bin/env python3
"""Independently validate the frozen v6 cache-validation protocol.

The validator re-derives every model hash from the CSV ``params`` and checks
the pre-registered cache semantics (cold first occurrence, warm later
occurrences) without importing the builder that produced the files.
"""
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
REQUIRED_MANIFEST_KEYS = (
    "version", "frozen_before_execution", "selection_uses_verdicts",
    "selection_rule", "semantics_gate", "tasks", "families",
    "tasks_per_family", "shards", "tasks_per_shard", "experiments", "strata",
    "cell_timeout_seconds", "job_timeout_minutes", "max_parallel", "runner",
    "artifact_retention_days", "distinct_model_hashes",
    "distinct_bounded_problems", "unique_configurations",
    "duplicate_configurations", "expected_cache", "differential_pairs",
    "cache_key_components", "cache_policy",
    "worst_case_compute_runner_minutes", "shard_planning_rule",
)


def load_rows(tasks_dir):
    """Return (shard_names, rows) preserving shard order and row order."""
    paths = sorted(path for path in tasks_dir.glob("*.csv") if SHARD.fullmatch(path.name))
    rows = []
    for path in paths:
        shard = int(path.stem.split("_")[1])
        for line_index, row in enumerate(csv.DictReader(path.open()), start=1):
            row["_shard"] = shard
            row["_line"] = line_index
            rows.append(row)
    return paths, rows


def pilot_overlap():
    """Report overlap with the frozen Pilot; never an error for this protocol."""
    pilot_manifest_path = ROOT / "protocol/v6_pilot_manifest.json"
    pilot_tasks_dir = ROOT / "protocol/v6_pilot_tasks"
    pilot_manifest = {}
    if pilot_manifest_path.is_file():
        pilot_manifest = json.loads(pilot_manifest_path.read_text())
    pilot_bounded = set()
    if pilot_tasks_dir.is_dir():
        for path in pilot_tasks_dir.glob("shard_*.csv"):
            with path.open() as handle:
                pilot_bounded.update(
                    row["bounded_problem_id"] for row in csv.DictReader(handle)
                )
    return pilot_manifest, pilot_bounded


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--semantics-gate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    errors = []
    manifest = json.loads(args.manifest.read_text())
    semantics = json.loads(args.semantics_gate.read_text())
    family_names = list(FAMILY_SPECS)

    for key in REQUIRED_MANIFEST_KEYS:
        if key not in manifest:
            errors.append(f"manifest missing key: {key}")
    if not manifest.get("frozen_before_execution"):
        errors.append("manifest is not marked frozen_before_execution")
    if manifest.get("selection_uses_verdicts") is not False:
        errors.append("manifest must declare selection_uses_verdicts=false")
    if not semantics.get("PASS"):
        errors.append("semantics gate is not PASS")

    paths, rows = load_rows(args.tasks)
    expected_shard_names = {f"shard_{index:03d}.csv" for index in range(manifest["shards"])}
    actual_shard_names = {path.name for path in paths}
    if actual_shard_names != expected_shard_names:
        errors.append(
            "shard file set mismatch: "
            f"missing={sorted(expected_shard_names - actual_shard_names)} "
            f"extra={sorted(actual_shard_names - expected_shard_names)}"
        )
    if len(paths) != manifest["shards"]:
        errors.append("shard count mismatch")
    if len(rows) != manifest["tasks"]:
        errors.append("task count mismatch")
    if manifest["families"] != len(family_names):
        errors.append("manifest family count mismatch")
    if manifest.get("family_names") not in (None, family_names):
        errors.append("manifest family_names order mismatch")
    if manifest["tasks_per_family"] * manifest["families"] != manifest["tasks"]:
        errors.append("tasks_per_family x families != tasks")

    for key in ("task_id", "configuration_id"):
        values = [row[key] for row in rows]
        if len(values) != len(set(values)):
            errors.append(f"duplicate {key}")
    for row in rows:
        if row["task_id"] != row["configuration_id"]:
            errors.append(f"task_id != configuration_id: {row['task_id']}")

    rows_by_shard = {}
    for row in rows:
        rows_by_shard.setdefault(row["_shard"], []).append(row)
    if sorted(rows_by_shard) != list(range(manifest["shards"])):
        errors.append("shard index set mismatch")
    for shard, shard_rows in rows_by_shard.items():
        if len(shard_rows) != manifest["tasks_per_shard"]:
            errors.append(f"shard {shard} task count mismatch")
        families = {row["benchmark_family"] for row in shard_rows}
        if len(families) != 1:
            errors.append(f"shard {shard} spans families: {sorted(families)}")
        elif shard < len(family_names) and families != {family_names[shard]}:
            errors.append(f"shard {shard} family order mismatch: {sorted(families)}")

    per_family = {
        family: sum(row["benchmark_family"] == family for row in rows)
        for family in family_names
    }
    if set(per_family) != {row["benchmark_family"] for row in rows}:
        errors.append("row family outside declared family list")
    if any(count != manifest["tasks_per_family"] for count in per_family.values()):
        errors.append(f"family task counts differ: {per_family}")

    generated = {}
    for row in rows:
        if row["protocol_version"] != manifest["version"]:
            errors.append(f"protocol version mismatch: {row['task_id']}")
        if int(row["timeout_s"]) != manifest["cell_timeout_seconds"]:
            errors.append(f"timeout mismatch: {row['task_id']}")
        if int(row["replicate"]) not in (0, 1):
            errors.append(f"unexpected replicate: {row['task_id']}")
        if row["expected_cache_status"] not in ("miss", "hit"):
            errors.append(f"unexpected expected_cache_status: {row['task_id']}")
        if row["cache_group"] != row["expected_model_hash"]:
            errors.append(f"cache_group != model hash: {row['task_id']}")
        if int(row["L"]) <= 0:
            errors.append(f"non-positive L: {row['task_id']}")

        cache_key = row["params"]
        if cache_key not in generated:
            params = json.loads(cache_key)
            tpn, _, generated_h, _ = generate_v6_model(**params)
            generated[cache_key] = (
                model_hash(tpn), structure_hash(tpn), generated_h,
            )
        full_hash, struct_hash, generated_h = generated[cache_key]
        if full_hash != row["expected_model_hash"]:
            errors.append(f"frozen model hash mismatch: {row['task_id']}")
        if struct_hash != row["expected_structure_hash"]:
            errors.append(f"frozen structure hash mismatch: {row['task_id']}")
        if abs(float(row["H"]) - float(generated_h)) > 1e-12:
            errors.append(f"H differs from generated H: {row['task_id']}")
        if not row["model_id"].endswith(full_hash[:20]):
            errors.append(f"model_id does not derive from model hash: {row['task_id']}")

    # Same-model colocation: one model hash must live in exactly one shard.
    shards_by_model = {}
    for row in rows:
        shards_by_model.setdefault(row["expected_model_hash"], set()).add(row["_shard"])
    misplaced = {
        model: sorted(shards) for model, shards in shards_by_model.items()
        if len(shards) != 1
    }
    for model, shards in misplaced.items():
        errors.append(f"model hash split across shards: {model[:20]} -> {shards}")

    # Pre-registered cache order: cold first occurrence, warm later occurrences.
    expected_miss = expected_hit = 0
    per_shard_cache = {}
    for shard in sorted(rows_by_shard):
        seen = set()
        miss = hit = 0
        for row in sorted(rows_by_shard[shard], key=lambda item: item["_line"]):
            group = row["cache_group"]
            derived = "hit" if group in seen else "miss"
            seen.add(group)
            if derived != row["expected_cache_status"]:
                errors.append(
                    "cache expectation disagrees with structural order: "
                    f"{row['task_id']} declared {row['expected_cache_status']} "
                    f"but order implies {derived}"
                )
            if derived == "miss":
                miss += 1
            else:
                hit += 1
        per_shard_cache[shard] = {"miss": miss, "hit": hit}
        expected_miss += miss
        expected_hit += hit
        declared = manifest["expected_cache"]["per_shard"]
        if (miss, hit) != (declared["miss"], declared["hit"]):
            errors.append(f"shard {shard} cache counts differ: miss={miss} hit={hit}")
    declared_total = manifest["expected_cache"]["total"]
    if (expected_miss, expected_hit) != (declared_total["miss"], declared_total["hit"]):
        errors.append(
            "total cache counts differ: "
            f"miss={expected_miss} hit={expected_hit}"
        )

    # Differential pairs: same model/problem/L/H/mode, replicate 0 vs 1.
    pair_rows = {row["task_id"]: row for row in rows}
    pairs = manifest["differential_pairs"]
    if len(pairs) != manifest["families"]:
        errors.append("differential pair count mismatch")
    checked_pairs = 0
    for pair in pairs:
        if len(pair) != 2:
            errors.append(f"malformed differential pair: {pair}")
            continue
        first, second = (pair_rows.get(task_id) for task_id in pair)
        if first is None or second is None:
            errors.append(f"differential pair references unknown task: {pair}")
            continue
        checked_pairs += 1
        for field in ("params", "L", "H", "mode"):
            if first[field] != second[field]:
                errors.append(f"differential pair {field} differs: {pair}")
        if first["bounded_problem_id"] != second["bounded_problem_id"]:
            errors.append(f"differential pair bounded_problem_id differs: {pair}")
        if first["configuration_id"] == second["configuration_id"]:
            errors.append(f"differential pair configuration_id equal: {pair}")
        if sorted((int(first["replicate"]), int(second["replicate"]))) != [0, 1]:
            errors.append(f"differential pair replicate values differ from 0/1: {pair}")

    distinct_model_hashes = len({row["expected_model_hash"] for row in rows})
    distinct_bounded_problems = len({row["bounded_problem_id"] for row in rows})
    unique_configurations = len({row["configuration_id"] for row in rows})
    duplicate_configurations = len(rows) - unique_configurations
    for key, actual in (
        ("distinct_model_hashes", distinct_model_hashes),
        ("distinct_bounded_problems", distinct_bounded_problems),
        ("unique_configurations", unique_configurations),
        ("duplicate_configurations", duplicate_configurations),
    ):
        if manifest[key] != actual:
            errors.append(f"manifest {key}={manifest[key]} but measured {actual}")

    experiments = {
        name: sum(row["experiment"] == name for row in rows)
        for name in manifest["experiments"]
    }
    if experiments != manifest["experiments"]:
        errors.append(f"experiment counts differ: {experiments}")
    strata = {
        name: sum(row["stratum"] == name for row in rows)
        for name in manifest["strata"]
    }
    if strata != manifest["strata"]:
        errors.append(f"stratum counts differ: {strata}")

    pilot_manifest, pilot_bounded = pilot_overlap()
    bounded_overlap = {row["bounded_problem_id"] for row in rows} & pilot_bounded

    result = {
        "PASS": not errors,
        "errors": sorted(set(errors)),
        "protocol_version": manifest["version"],
        "tasks": len(rows),
        "shards": len(paths),
        "families": len(per_family),
        "tasks_per_family": per_family,
        "experiments": experiments,
        "strata": strata,
        "distinct_model_hashes": distinct_model_hashes,
        "distinct_bounded_problems": distinct_bounded_problems,
        "unique_configurations": unique_configurations,
        "duplicate_configurations": duplicate_configurations,
        "cache": {
            "expected_miss": expected_miss,
            "expected_hit": expected_hit,
            "per_shard": per_shard_cache,
            "model_groups": {
                "total": len(shards_by_model),
                "per_shard": len(shards_by_model) // manifest["shards"],
            },
        },
        "differential_pairs": {
            "count": len(pairs),
            "checked": checked_pairs,
        },
        "same_model_same_shard": not misplaced,
        "pilot_bounded_problem_overlap": len(bounded_overlap),
        "pilot_distinct_bounded_problems": pilot_manifest.get(
            "distinct_bounded_problems"
        ),
        "pilot_manifest": str(Path("protocol/v6_pilot_manifest.json")),
        "semantics_gate": {
            "PASS": bool(semantics.get("PASS")),
            "semantics_source_sha": semantics.get("semantics_source_sha"),
            "github_run_id": semantics.get("github_run_id"),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
