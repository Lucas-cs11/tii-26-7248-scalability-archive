#!/usr/bin/env python3
"""Validate the frozen v5 task grid and every distinct generated TPN."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.run_cell import model_hash, structure_hash
from src.benchmarks.v5_families import FAMILY_SPECS, generate_v5_model
from src.model.validation import validate_model


SHARD_NAME = re.compile(r"shard_[0-9]{3}\.csv\Z")


def fire_checked(tpn, marking, transition):
    if transition not in tpn.transitions or not tpn.is_enabled(marking, transition):
        raise ValueError(f"constructive witness cannot fire {transition}")
    return tpn.fire(marking, transition)


def named_transition(tpn, prefix):
    matches = sorted(name for name in tpn.transitions if name.startswith(prefix))
    if len(matches) != 1:
        raise ValueError(f"expected one transition for {prefix}, got {matches}")
    return matches[0]


def normal_operation_to_end(tpn, marking, part, operation):
    transfer = named_transition(tpn, f"t_trans_j{part}_op{operation}_")
    marking = fire_checked(tpn, marking, transfer)
    process = named_transition(tpn, f"t_proc_j{part}_op{operation}_")
    marking = fire_checked(tpn, marking, process)
    finish = [
        name for name in tpn.transitions
        if name.startswith(f"t_finish_j{part}_op{operation}_")
    ]
    if finish:
        if len(finish) != 1:
            raise ValueError("ambiguous normal finish transition")
        marking = fire_checked(tpn, marking, finish[0])
    return marking


def marking_at_operation_start(tpn, part, operation):
    marking = dict(tpn.initial_marking)
    marking = fire_checked(tpn, marking, f"t_release_j{part}")
    for prior in range(1, operation):
        marking = normal_operation_to_end(tpn, marking, part, prior)
        marking = fire_checked(
            tpn, marking, f"t_chain_j{part}_op{prior}_to_op{prior + 1}"
        )
    return marking


def prove_generated_semantics(tpn, attrs):
    """Construct finite witnesses implied by the generator's normal path."""
    parts = attrs["number_of_parts"]
    operations = attrs["operations_per_part"]
    wip = attrs["WIP"]

    # Exact WIP reachability witness: distinct releases consume capacity.
    concurrent = dict(tpn.initial_marking)
    for part in range(1, wip + 1):
        concurrent = fire_checked(tpn, concurrent, f"t_release_j{part}")
    reached = wip - concurrent.get("p_wip_capacity", 0)
    if reached != wip:
        raise ValueError(f"reachable concurrency {reached} != WIP {wip}")

    # Completion witness: process parts sequentially along the optional-free path.
    completion = dict(tpn.initial_marking)
    for part in range(1, parts + 1):
        completion = fire_checked(tpn, completion, f"t_release_j{part}")
        for operation in range(1, operations + 1):
            completion = normal_operation_to_end(tpn, completion, part, operation)
            if operation < operations:
                completion = fire_checked(
                    tpn, completion,
                    f"t_chain_j{part}_op{operation}_to_op{operation + 1}",
                )
            else:
                completion = fire_checked(tpn, completion, f"t_complete_j{part}")
    completion = fire_checked(tpn, completion, "t_task_done")
    if not tpn.in_completion(completion):
        raise ValueError("constructive normal path does not reach completion")

    # Every seeded fault is reachable before completion on its own witness path.
    fault_count = 0
    for name, transition in tpn.transitions.items():
        if not transition.is_fault:
            continue
        match = re.match(r"t_fault_j([0-9]+)_op([0-9]+)_(.*)\Z", name)
        if not match:
            raise ValueError(f"unrecognized fault transition {name}")
        part, operation = map(int, match.group(1, 2))
        marking = marking_at_operation_start(tpn, part, operation)
        marking = fire_checked(tpn, marking, name)
        recovery = f"t_recov_j{part}_op{operation}_{match.group(3)}"
        fire_checked(tpn, marking, recovery)
        fault_count += 1
    if fault_count == 0:
        raise ValueError("model has no reachable fault witness")

    rework_count = 0
    for name, transition in tpn.transitions.items():
        match = re.match(r"t_rework_j([0-9]+)_op([0-9]+)\Z", name)
        if not match:
            continue
        part, operation = map(int, match.group(1, 2))
        marking = marking_at_operation_start(tpn, part, operation)
        marking = normal_operation_to_end(tpn, marking, part, operation)
        if transition.interval.earliest <= 0:
            raise ValueError(f"non-positive rework cycle {name}")
        marking = fire_checked(tpn, marking, name)
        if marking.get(f"p{part}_op{operation}_start", 0) != 1:
            raise ValueError(f"rework {name} does not return to operation start")
        rework_count += 1

    route_count = 0
    for name in tpn.transitions:
        match = re.match(r"t_route_alt_j([0-9]+)_op([0-9]+)_to_op([0-9]+)\Z", name)
        if not match:
            continue
        part, operation, _ = map(int, match.group(1, 2, 3))
        marking = marking_at_operation_start(tpn, part, operation)
        marking = normal_operation_to_end(tpn, marking, part, operation)
        fire_checked(tpn, marking, name)
        route_count += 1
    return {
        "completion_reachable": True,
        "fault_transitions_witnessed": fault_count,
        "reachable_concurrency": reached,
        "rework_cycles_witnessed": rework_count,
        "alternative_routes_witnessed": route_count,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--tasks", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    all_csv_paths = sorted(args.tasks.glob("*.csv"))
    unexpected_task_files = [path.name for path in all_csv_paths if not SHARD_NAME.fullmatch(path.name)]
    shard_paths = [path for path in all_csv_paths if SHARD_NAME.fullmatch(path.name)]
    rows = [row for path in shard_paths for row in csv.DictReader(path.open())]
    errors = []
    if unexpected_task_files:
        errors.append(f"unexpected task CSV filenames: {unexpected_task_files}")

    task_ids = [row["task_id"] for row in rows]
    config_ids = [row["configuration_id"] for row in rows]
    problem_ids = [row["bounded_problem_id"] for row in rows]
    if len(shard_paths) != manifest["shards"]:
        errors.append("task-shard count differs from manifest")
    if len(rows) != manifest["tasks"]:
        errors.append("task count differs from manifest")
    if len(task_ids) != len(set(task_ids)) or len(config_ids) != len(set(config_ids)):
        errors.append("duplicate task or configuration identity")
    if len(set(problem_ids)) != manifest["distinct_bounded_problems"]:
        errors.append("bounded-problem count differs from manifest")
    if any(int(row["replicate"]) != 0 for row in rows):
        errors.append("timing repetitions are present in the unique-configuration matrix")

    by_model = defaultdict(list)
    for row in rows:
        by_model[row["model_id"]].append(row)
    generated = {}
    invalid_models = []
    inconsistent_model_specs = []
    semantic_witness_failures = []
    for model_id, group in sorted(by_model.items()):
        specs = {(row["seed"], row["params"]) for row in group}
        if len(specs) != 1:
            inconsistent_model_specs.append(model_id)
            continue
        seed, params_text = next(iter(specs))
        params = json.loads(params_text)
        tpn, _, _, attrs = generate_v5_model(**params)
        validation_errors = validate_model(tpn)
        if validation_errors:
            invalid_models.append({"model_id": model_id, "errors": validation_errors})
        try:
            witnesses = prove_generated_semantics(tpn, attrs)
        except Exception as exc:  # noqa: BLE001
            witnesses = None
            semantic_witness_failures.append({"model_id": model_id, "error": str(exc)})
        generated[model_id] = {
            "benchmark_family": group[0]["benchmark_family"],
            "seed": int(seed),
            "model_hash": model_hash(tpn),
            "structure_hash": structure_hash(tpn),
            "number_of_places": len(tpn.places),
            "number_of_transitions": len(tpn.transitions),
            "number_of_arcs": len(tpn.arcs),
            "WIP": attrs["WIP"],
            "verified_reachable_concurrency": attrs["verified_reachable_concurrency"],
            "concurrency_proof": attrs["concurrency_proof"],
            "cycle_presence": attrs["cycle_presence"],
            "cycle_semantics": attrs["cycle_semantics"],
            "synthetic_model": attrs["synthetic_model"],
            "semantic_witnesses": witnesses,
        }
    if inconsistent_model_specs:
        errors.append(f"model IDs map to inconsistent generation specs: {len(inconsistent_model_specs)}")
    if invalid_models:
        errors.append(f"structurally invalid generated models: {len(invalid_models)}")
    if semantic_witness_failures:
        errors.append(f"constructive semantic witness failures: {len(semantic_witness_failures)}")

    # A requires 12 declared levels and 50 distinct seeded models per level.
    a_rows = [row for row in rows if row["experiment"] == "A_structural"]
    a_levels = defaultdict(list)
    for row in a_rows:
        level = row["base_model_id"].rsplit(":s", 1)[0]
        a_levels[level].append(row)
    if len(a_levels) != 12 or any(len(group) != 50 for group in a_levels.values()):
        errors.append("A is not 12 levels x 50 seeded models")
    a_duplicate_levels = []
    for level, group in a_levels.items():
        hashes = [generated[row["model_id"]]["model_hash"] for row in group]
        if len(hashes) != len(set(hashes)):
            a_duplicate_levels.append(level)
    if a_duplicate_levels:
        errors.append(f"A contains duplicate generated TPNs within levels: {len(a_duplicate_levels)}")
    a_control_failures = []
    expected_series = {
        "A1_depth": ("ops_per_part", [4, 7, 10, 13], {
            "n_parts": 2, "n_machines": 4, "wip_bound": 1,
        }),
        "A2_parts": ("n_parts", [2, 4, 7, 10], {
            "ops_per_part": 4, "n_machines": 5, "wip_bound": 1,
        }),
        "A3_resources": ("route_choices", [0, 2, 5, 9], {
            "n_parts": 3, "ops_per_part": 4, "n_machines": 4, "wip_bound": 1,
        }),
    }
    a_size_ranges = {}
    for series, (varied, expected_values, fixed) in expected_series.items():
        levels = []
        for level in range(4):
            key = f"{series}:l{level:02d}"
            group = a_levels.get(key, [])
            params = [json.loads(row["params"]) for row in group]
            values = {item.get(varied) for item in params}
            if values != {expected_values[level]} or any(
                    any(item.get(name) != value for name, value in fixed.items())
                    for item in params):
                a_control_failures.append(key)
            sizes = [
                generated[row["model_id"]]["number_of_places"]
                + generated[row["model_id"]]["number_of_transitions"]
                for row in group
            ]
            if sizes:
                levels.append((min(sizes), max(sizes)))
                a_size_ranges[key] = {"min": min(sizes), "max": max(sizes)}
        if any(levels[index][0] <= levels[index - 1][1] for index in range(1, len(levels))):
            a_control_failures.append(f"{series}:non_monotone_measured_size")
    if a_control_failures:
        errors.append(f"A control/monotonicity failures: {len(a_control_failures)}")

    # B requires 360 base models and paired WIP markings with identical structure.
    b_rows = [row for row in rows if row["experiment"] == "B_wip"]
    b_groups = defaultdict(list)
    for row in b_rows:
        b_groups[row["base_model_id"]].append(row)
    b_pair_failures = []
    for base, group in b_groups.items():
        wp = {generated[row["model_id"]]["WIP"] for row in group}
        structures = {generated[row["model_id"]]["structure_hash"] for row in group}
        concurrency = {
            generated[row["model_id"]]["semantic_witnesses"]["reachable_concurrency"]
            for row in group if generated[row["model_id"]]["semantic_witnesses"]
        }
        if len(group) != 3 or wp != {1, 2, 3} or concurrency != {1, 2, 3} or len(structures) != 1:
            b_pair_failures.append(base)
    if len(b_groups) != 360 or b_pair_failures:
        errors.append(f"B pairing/proof failures: bases={len(b_groups)}, failures={len(b_pair_failures)}")
    b_family_hash_duplicates = []
    for family in FAMILY_SPECS:
        family_rows = [row for row in b_rows if row["benchmark_family"] == family]
        wip1 = [row for row in family_rows if generated[row["model_id"]]["WIP"] == 1]
        hashes = [generated[row["model_id"]]["model_hash"] for row in wip1]
        if len(hashes) != len(set(hashes)):
            b_family_hash_duplicates.append(family)
    if b_family_hash_duplicates:
        errors.append(f"B duplicate base models by family: {len(b_family_hash_duplicates)}")

    # C1 is 300 fixed TPNs x 15 consecutive L; C2 is a fixed 60-model subset.
    c_first = [row for row in rows if row["experiment"] == "C_Lsweep" and row["mode"] == "first"]
    c_complete = [row for row in rows if row["experiment"] == "C_Lsweep" and row["mode"] == "complete"]
    c_first_groups = defaultdict(list)
    c_complete_groups = defaultdict(list)
    for row in c_first:
        c_first_groups[row["model_id"]].append(row)
    for row in c_complete:
        c_complete_groups[row["model_id"]].append(row)
    c_failures = []
    for model_id, group in c_first_groups.items():
        levels = sorted(int(row["L"]) for row in group)
        h_values = {float(row["H"]) for row in group}
        if len(levels) != 15 or levels != list(range(levels[0], levels[0] + 15)) or len(h_values) != 1:
            c_failures.append(model_id)
    expected_complete_seeds = {0, 6, 12, 18, 24}
    complete_seeds = {int(group[0]["seed"]) for group in c_complete_groups.values()}
    if (len(c_first_groups) != 300 or len(c_complete_groups) != 60 or c_failures
            or complete_seeds != expected_complete_seeds
            or not set(c_complete_groups).issubset(c_first_groups)):
        errors.append(
            f"C fixed-sweep/subset failures: first={len(c_first_groups)}, "
            f"complete={len(c_complete_groups)}, malformed={len(c_failures)}"
        )
    c_family_hash_duplicates = []
    for family in FAMILY_SPECS:
        family_models = [
            model_id for model_id, group in c_first_groups.items()
            if group[0]["benchmark_family"] == family
        ]
        hashes = [generated[model_id]["model_hash"] for model_id in family_models]
        if len(hashes) != 25 or len(hashes) != len(set(hashes)):
            c_family_hash_duplicates.append(family)
    if c_family_hash_duplicates:
        errors.append(f"C duplicate/missing fixed TPNs by family: {len(c_family_hash_duplicates)}")

    model_hash_counts = Counter(item["model_hash"] for item in generated.values())
    result = {
        "PASS": not errors,
        "errors": errors,
        "task_shards": len(shard_paths),
        "evaluation_runs": len(rows),
        "parameter_configurations": len(set(config_ids)),
        "distinct_bounded_problems": len(set(problem_ids)),
        "declared_model_ids": len(generated),
        "distinct_generated_tpn_hashes": len(model_hash_counts),
        "cross_experiment_hash_reuse": sum(count - 1 for count in model_hash_counts.values() if count > 1),
        "timing_replicates_beyond_primary": sum(int(row["replicate"]) > 0 for row in rows),
        "families": sorted({row["benchmark_family"] for row in rows}),
        "experiment_mode_counts": {
            f"{experiment}:{mode}": count
            for (experiment, mode), count in sorted(
                Counter((row["experiment"], row["mode"]) for row in rows).items()
            )
        },
        "A_levels": len(a_levels),
        "A_duplicate_levels": a_duplicate_levels,
        "A_control_failures": a_control_failures,
        "A_measured_size_ranges": a_size_ranges,
        "B_base_models": len(b_groups),
        "B_pair_failures": b_pair_failures,
        "B_family_hash_duplicates": b_family_hash_duplicates,
        "C_first_fixed_tpns": len(c_first_groups),
        "C_complete_fixed_tpns": len(c_complete_groups),
        "invalid_models": invalid_models,
        "semantic_witness_failures": semantic_witness_failures,
        "inconsistent_model_specs": inconsistent_model_specs,
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
        "commit_sha": os.environ.get("GITHUB_SHA"),
        "frozen_manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "models": generated,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: result[key] for key in (
        "PASS", "errors", "evaluation_runs", "parameter_configurations",
        "distinct_bounded_problems", "declared_model_ids",
        "distinct_generated_tpn_hashes", "cross_experiment_hash_reuse",
        "A_levels", "B_base_models", "C_first_fixed_tpns",
        "C_complete_fixed_tpns",
    )}, indent=2))
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
