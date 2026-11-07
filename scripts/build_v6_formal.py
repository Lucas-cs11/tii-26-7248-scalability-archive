#!/usr/bin/env python3
"""Build the frozen v6 full-scale matrix (8,280 cells); never runs verification.

The structural grid is the pre-registered v5 grid re-instantiated on the
semantics-corrected v6 generator: it is defined by declared sizes, declared
difficulty levels and ascending seeds with a frozen duplicate-hash rejection
rule.  No Pilot verdict, runtime or failure stage is read.

Quota (fixed): A structural 600, B WIP 1,080, C L-sweep 5,400, D difficult
1,200 -> 8,280 immutable configurations, 120 shards x 69 tasks.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.run_cell import model_hash, structure_hash  # noqa: E402
from src.benchmarks.v6_families import FAMILY_SPECS, generate_v6_model  # noqa: E402

PROTOCOL = "v6-formal-1"
OUT = ROOT / "protocol/v6_formal_tasks"
MANIFEST = ROOT / "protocol/v6_formal_manifest.json"
FAMILIES = list(FAMILY_SPECS)
SHARDS = 120
CELL_TIMEOUT = 60
FIELDS = [
    "task_id", "protocol_version", "experiment_id", "experiment",
    "benchmark_family", "base_model_id", "model_id", "bounded_problem_id",
    "configuration_id", "seed", "replicate", "L", "H", "mode", "timeout_s",
    "expected_model_hash", "expected_structure_hash", "params",
]
SHARD_NAME = re.compile(r"shard_[0-9]{3}\.csv\Z")


def stable_id(prefix: str, payload: dict) -> str:
    digest = hashlib.sha256(
        json.dumps({**payload, "protocol": PROTOCOL}, sort_keys=True,
                   separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return f"{prefix}-{digest}"


def add_task(tasks, *, experiment, family, base_model_id, seed, L, H, mode, params):
    tpn, _, generated_h, _ = generate_v6_model(**params)
    if H is None:
        H = generated_h
    full_hash = model_hash(tpn)
    struct_hash = structure_hash(tpn)
    model_id = f"v6f:{family}:{full_hash[:20]}"
    bounded = stable_id("bpf6", {"model_hash": full_hash, "L": int(L), "H": float(H)})
    configuration = stable_id(
        "cfgf6", {"bounded_problem": bounded, "mode": mode, "replicate": 0}
    )
    tasks.append({
        "task_id": configuration,
        "protocol_version": PROTOCOL,
        "experiment_id": f"{PROTOCOL}-{experiment}",
        "experiment": experiment,
        "benchmark_family": family,
        "base_model_id": base_model_id,
        "model_id": model_id,
        "bounded_problem_id": bounded,
        "configuration_id": configuration,
        "seed": int(seed),
        "replicate": 0,
        "L": int(L),
        "H": float(H),
        "mode": mode,
        "timeout_s": CELL_TIMEOUT,
        "expected_model_hash": full_hash,
        "expected_structure_hash": struct_hash,
        "params": json.dumps(params, sort_keys=True, separators=(",", ":")),
    })


def first_unique_seeds(family: str, count: int, overrides: dict) -> list[int]:
    """Ascending seeds, rejecting only duplicate generated TPN hashes."""
    accepted, hashes = [], set()
    for seed in range(10_000):
        tpn, _, _, _ = generate_v6_model(family, seed, **overrides)
        digest = model_hash(tpn)
        if digest in hashes:
            continue
        hashes.add(digest)
        accepted.append(seed)
        if len(accepted) == count:
            return accepted
    raise RuntimeError(f"could not generate {count} unique {family} models")


def build_tasks():
    tasks = []
    families = list(FAMILIES)

    # A: 3 controlled series x 4 levels x 50 independent seeds = 600.
    structural = []
    for level, ops in enumerate((4, 7, 10, 13)):
        structural.append((f"A1_depth:l{level:02d}", "F02_sequential_dense",
                           dict(n_parts=2, ops_per_part=ops, n_machines=4, wip_bound=1),
                           32, 240.0))
    for level, parts in enumerate((2, 4, 7, 10)):
        structural.append((f"A2_parts:l{level:02d}", "F06_parallel_parts",
                           dict(n_parts=parts, ops_per_part=4, n_machines=5, wip_bound=1),
                           32, 240.0))
    for level, routes in enumerate((0, 2, 5, 9)):
        structural.append((f"A3_resources:l{level:02d}", "F04_alternative_routes",
                           dict(n_parts=3, ops_per_part=4, n_machines=4, wip_bound=1,
                                route_choices=routes), 28, 210.0))
    for level_id, family, overrides, L, H in structural:
        for seed in first_unique_seeds(family, 50, overrides):
            add_task(tasks, experiment="A_structural", family=family,
                     base_model_id=f"{level_id}:s{seed:04d}", seed=seed,
                     L=L, H=H, mode="first",
                     params={"v6_family": family, "seed": seed, **overrides})

    # B: 12 structure groups x 30 independent seeds x WIP 1/2/3 = 1080.
    for family_index, family in enumerate(families):
        seed_overrides = {
            "n_parts": 3,
            "ops_per_part": 3 + family_index % 4,
            "n_machines": 1 + family_index % 3,
            "unobservable_ratio": (0.25, 0.50, 0.70)[family_index % 3],
            "wip_bound": 1,
        }
        for seed in first_unique_seeds(family, 30, seed_overrides):
            base = f"B:{family}:s{seed:02d}"
            common = {
                "v6_family": family, "seed": seed, "n_parts": 3,
                "ops_per_part": 3 + family_index % 4,
                "n_machines": 1 + family_index % 3,
                "unobservable_ratio": (0.25, 0.50, 0.70)[family_index % 3],
            }
            for wip in (1, 2, 3):
                add_task(tasks, experiment="B_wip", family=family,
                         base_model_id=base, seed=seed,
                         L=28, H=240.0, mode="first",
                         params={**common, "wip_bound": wip})

    # C1: 12 families x 25 fixed TPN seeds x 15 consecutive L levels = 4500.
    for family in families:
        for seed in range(25):
            _, default_l, default_h, _ = generate_v6_model(family, seed)
            start = max(2, default_l - 7)
            base = f"C:{family}:s{seed:02d}"
            params = {"v6_family": family, "seed": seed}
            for L in range(start, start + 15):
                add_task(tasks, experiment="C_Lsweep", family=family,
                         base_model_id=base, seed=seed, L=L, H=default_h,
                         mode="first", params=params)

    # C2: preregistered COMPLETE_SET pairing, 12 x 5 seeds x 15 = 900.
    for family in families:
        for seed in (0, 6, 12, 18, 24):
            _, default_l, default_h, _ = generate_v6_model(family, seed)
            start = max(2, default_l - 7)
            base = f"C:{family}:s{seed:02d}"
            params = {"v6_family": family, "seed": seed}
            for L in range(start, start + 15):
                add_task(tasks, experiment="C_Lsweep", family=family,
                         base_model_id=base, seed=seed, L=L, H=default_h,
                         mode="complete", params=params)

    # D: 12 families x 10 seeds x 10 monotone declared difficulty levels = 1200.
    for family in families:
        spec = FAMILY_SPECS[family]
        for seed in range(10):
            for level in range(10):
                params = {
                    "v6_family": family, "seed": seed,
                    "n_parts": max(3, spec["parts"]) + level // 4,
                    "ops_per_part": spec["ops"] + 1 + level // 2,
                    "n_machines": max(1, spec["machines"] - level // 5),
                    "wip_bound": min(3, 1 + level // 3),
                    "inspection_locations": min(10, spec["inspections"] + level),
                    "route_choices": spec.get("routes", 0) + level // 2,
                    "unobservable_ratio": min(0.90, spec["unobs"] + 0.03 * level),
                }
                add_task(tasks, experiment="D_difficult", family=family,
                         base_model_id=f"D:{family}:l{level:02d}:s{seed:02d}",
                         seed=seed, L=24 + 2 * level, H=240.0 + 15.0 * level,
                         mode="first", params=params)
    return sorted(tasks, key=lambda row: row["task_id"])


def main() -> int:
    tasks = build_tasks()
    if len(tasks) != 8280:
        raise RuntimeError(f"expected 8280 tasks, got {len(tasks)}")
    if len({task["configuration_id"] for task in tasks}) != len(tasks):
        raise RuntimeError("duplicate configuration identity")
    buckets = [[] for _ in range(SHARDS)]
    for index, task in enumerate(tasks):
        buckets[index % SHARDS].append(task)
    OUT.mkdir(parents=True, exist_ok=True)
    unexpected = [path.name for path in OUT.glob("*.csv")
                  if not SHARD_NAME.fullmatch(path.name)]
    if unexpected:
        raise RuntimeError(f"unexpected task CSV files: {unexpected}")
    for shard, rows in enumerate(buckets):
        with (OUT / f"shard_{shard:03d}.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    experiments = {
        name: sum(task["experiment"] == name for task in tasks)
        for name in ("A_structural", "B_wip", "C_Lsweep", "D_difficult")
    }
    manifest = {
        "version": PROTOCOL,
        "frozen_before_execution": True,
        "semantics_gate": "protocol/v6_semantics_gate.json",
        "selection_uses_verdicts": False,
        "selection_rule": (
            "pre-registered structural grid: declared sizes, declared difficulty "
            "levels and ascending seeds with frozen duplicate-hash rejection; no "
            "Pilot verdict, runtime or failure stage is read"
        ),
        "tasks": len(tasks),
        "unique_configurations": len({task["configuration_id"] for task in tasks}),
        "distinct_model_hashes": len({task["expected_model_hash"] for task in tasks}),
        "distinct_bounded_problems": len({task["bounded_problem_id"] for task in tasks}),
        "families": FAMILIES,
        "shards": SHARDS,
        "tasks_per_shard": len(buckets[0]),
        "experiments": experiments,
        "quota": {"structural": 600, "wip": 1080, "lsweep": 5400, "difficult": 1200},
        "cell_timeout_seconds": CELL_TIMEOUT,
        "max_parallel": 20,
        "job_timeout_minutes": 150,
        "worst_case_compute_runner_minutes": len(tasks) * CELL_TIMEOUT / 60,
        "runner": "GitHub-hosted ubuntu-22.04 standard runner only",
        "artifact_retention_days": 30,
        "identity_rule": (
            "bounded_problem_id hashes {protocol, model_hash, L, H}; "
            "configuration_id hashes the bounded problem plus mode; "
            "task_id equals configuration_id"
        ),
    }
    assert manifest["distinct_bounded_problems"] == 7380
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
