#!/usr/bin/env python3
"""Build the preregistered v5 pilot matrix; no verification is run locally."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.benchmarks.v5_families import FAMILY_SPECS, generate_v5_model


OUT = ROOT / "protocol/v5_pilot_tasks"
FAMILIES = list(FAMILY_SPECS)
FIELDS = [
    "task_id", "experiment_id", "experiment", "benchmark_family",
    "base_model_id", "model_id", "bounded_problem_id", "configuration_id",
    "seed", "replicate", "L", "H", "mode", "timeout_s", "params",
]


def stable_id(prefix: str, payload: dict) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return f"{prefix}-{digest}"


def add_task(tasks, *, experiment, family, base_model_id, seed, L, H, mode, timeout, params):
    model_id = f"{base_model_id}:w{params.get('wip_bound', FAMILY_SPECS[family]['wip'])}"
    problem = stable_id("bp", {"model": model_id, "L": L, "H": H})
    config = stable_id("cfg", {"problem": problem, "mode": mode, "replicate": 0})
    tasks.append({
        "task_id": config,
        "experiment_id": f"v5-pilot-{experiment}",
        "experiment": experiment,
        "benchmark_family": family,
        "base_model_id": base_model_id,
        "model_id": model_id,
        "bounded_problem_id": problem,
        "configuration_id": config,
        "seed": seed,
        "replicate": 0,
        "L": L,
        "H": H,
        "mode": mode,
        "timeout_s": timeout,
        "params": json.dumps(params, sort_keys=True),
    })


def build_tasks():
    tasks = []
    # A: three controlled structural subseries, four pilot levels each.
    structural = []
    for ops in (2, 5, 8, 11):
        structural.append(("A1_depth", "F02_sequential_dense", dict(n_parts=2, ops_per_part=ops, n_machines=4, wip_bound=1), 24, 180.0))
    for parts in (1, 2, 3, 4):
        structural.append(("A2_parts", "F06_parallel_parts", dict(n_parts=parts, ops_per_part=4, n_machines=5, wip_bound=1), 24, 180.0))
    for machines, routes in ((1, 0), (2, 1), (4, 2), (6, 3)):
        structural.append(("A3_resources", "F04_alternative_routes", dict(n_parts=3, ops_per_part=4, n_machines=machines, wip_bound=1, route_choices=routes), 20, 150.0))
    for level, (series, family, overrides, L, H) in enumerate(structural):
        for seed in range(3):
            params = {"v5_family": family, "seed": seed, **overrides}
            add_task(tasks, experiment="A_structural", family=family,
                     base_model_id=f"{series}:level{level}:s{seed}", seed=seed,
                     L=L, H=H, mode="first", timeout=45, params=params)

    # B: 12 fixed difficulty groups, paired WIP=1/2/3 and identical base identity.
    b_groups = []
    for index, family in enumerate(FAMILIES):
        b_groups.append((family, {
            "n_parts": 3,
            "ops_per_part": 2 + index % 4,
            "n_machines": (1, 2, 3)[index % 3],
            "unobservable_ratio": (0.25, 0.50, 0.70)[index % 3],
        }))
    for group, (family, overrides) in enumerate(b_groups):
        for seed in range(3):
            base = f"B:g{group:02d}:{family}:s{seed}"
            for wip in (1, 2, 3):
                params = {"v5_family": family, "seed": seed, **overrides, "wip_bound": wip}
                add_task(tasks, experiment="B_wip", family=family, base_model_id=base,
                         seed=seed, L=20, H=180.0, mode="first", timeout=45, params=params)

    # C: dense family-wide pilot, both official and complete-set modes.
    for family in FAMILIES:
        for seed in range(3):
            _, default_L, default_H, _ = generate_v5_model(family, seed)
            start = max(4, default_L - 10)
            base = f"C:{family}:s{seed}"
            params = {"v5_family": family, "seed": seed}
            for L in range(start, start + 8):
                for mode in ("first", "complete"):
                    add_task(tasks, experiment="C_Lsweep", family=family,
                             base_model_id=base, seed=seed, L=L, H=default_H,
                             mode=mode, timeout=45, params=params)

    # D: declared difficult cells; pilot establishes stage and budget behavior.
    for family in FAMILIES:
        for seed in range(3):
            spec = FAMILY_SPECS[family]
            params = {
                "v5_family": family, "seed": seed,
                "n_parts": max(3, spec["parts"]),
                "ops_per_part": spec["ops"] + 2,
                "wip_bound": min(3, max(3, spec["parts"])),
                "inspection_locations": min(8, max(3, spec["inspections"] + 2)),
                "route_choices": spec.get("routes", 0) + 2,
            }
            add_task(tasks, experiment="D_difficult", family=family,
                     base_model_id=f"D:{family}:s{seed}", seed=seed,
                     L=28, H=240.0, mode="first", timeout=45, params=params)
    return sorted(tasks, key=lambda row: row["task_id"])


def main():
    tasks = build_tasks()
    shards = 48
    OUT.mkdir(parents=True, exist_ok=True)
    for path in OUT.glob("shard_*.csv"):
        path.unlink()
    buckets = [[] for _ in range(shards)]
    for index, task in enumerate(tasks):
        buckets[index % shards].append(task)
    for shard, rows in enumerate(buckets):
        with (OUT / f"shard_{shard:03d}.csv").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    counts = {name: sum(t["experiment"] == name for t in tasks)
              for name in ("A_structural", "B_wip", "C_Lsweep", "D_difficult")}
    manifest = {
        "version": "v5-pilot-1",
        "frozen_before_execution": True,
        "purpose": "protocol, semantics, checkpoints, L coverage and resource-budget acceptance; no outcome-based model selection",
        "tasks": len(tasks),
        "unique_configurations": len({t["configuration_id"] for t in tasks}),
        "distinct_base_models": len({t["base_model_id"] for t in tasks}),
        "families": FAMILIES,
        "shards": shards,
        "experiments": counts,
        "timeout_s": 45,
        "worst_case_runner_minutes": sum(int(t["timeout_s"]) for t in tasks) / 60,
        "estimated_raw_and_checkpoint_mb": round(len(tasks) * 12 / 1024, 2),
        "runner": "GitHub-hosted ubuntu-22.04 standard runner only",
        "billing": "public repository; standard hosted runner is free; no larger or paid runner",
        "selection_rule": "all declared families/seeds/cells retained; semantic-invalid cells reported, not silently replaced",
    }
    (ROOT / "protocol/v5_pilot_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
