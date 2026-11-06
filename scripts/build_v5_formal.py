#!/usr/bin/env python3
"""Build the frozen v5 full-scale matrix; this script never runs verification."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.run_cell import model_hash
from src.benchmarks.v5_families import FAMILY_SPECS, generate_v5_model


OUT = ROOT / "protocol/v5_formal_tasks"
FAMILIES = list(FAMILY_SPECS)
FIELDS = [
    "task_id", "experiment_id", "experiment", "benchmark_family",
    "base_model_id", "model_id", "bounded_problem_id", "configuration_id",
    "seed", "replicate", "L", "H", "mode", "timeout_s", "params",
]
SHARD_NAME = re.compile(r"shard_[0-9]{3}\.csv\Z")


def stable_id(prefix: str, payload: dict) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return f"{prefix}-{digest}"


def add_task(tasks, *, experiment, family, base_model_id, seed, replicate,
             L, H, mode, timeout, params):
    model_id = f"{base_model_id}:w{params.get('wip_bound', FAMILY_SPECS[family]['wip'])}"
    bounded = stable_id("bp", {"model": model_id, "L": L, "H": H})
    configuration = stable_id(
        "cfg", {"bounded_problem": bounded, "mode": mode, "replicate": replicate}
    )
    tasks.append({
        "task_id": configuration,
        "experiment_id": f"v5-formal-{experiment}",
        "experiment": experiment,
        "benchmark_family": family,
        "base_model_id": base_model_id,
        "model_id": model_id,
        "bounded_problem_id": bounded,
        "configuration_id": configuration,
        "seed": seed,
        "replicate": replicate,
        "L": L,
        "H": H,
        "mode": mode,
        "timeout_s": timeout,
        "params": json.dumps(params, sort_keys=True, separators=(",", ":")),
    })


def first_unique_seeds(family: str, count: int, overrides: dict) -> list[int]:
    """Take ascending seeds, rejecting only duplicate generated TPN hashes."""
    accepted = []
    hashes = set()
    for seed in range(10_000):
        tpn, _, _, _ = generate_v5_model(family, seed, **overrides)
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

    # A: 3 controlled series x 4 levels x 50 independent seeds = 600.
    # Ascending seeds are accepted under a frozen hash-deduplication rule.
    structural = []
    for level, ops in enumerate((4, 7, 10, 13)):
        structural.append((f"A1_depth:l{level:02d}", "F02_sequential_dense",
                           dict(n_parts=2, ops_per_part=ops, n_machines=4, wip_bound=1),
                           32, 240.0))
    for level, parts in enumerate((2, 4, 7, 10)):
        structural.append((f"A2_parts:l{level:02d}", "F06_parallel_parts",
                           dict(n_parts=parts, ops_per_part=4, n_machines=5,
                                wip_bound=1), 32, 240.0))
    for level, routes in enumerate((0, 2, 5, 9)):
        structural.append((f"A3_resources:l{level:02d}", "F04_alternative_routes",
                           dict(n_parts=3, ops_per_part=4, n_machines=4,
                                wip_bound=1, route_choices=routes), 28, 210.0))
    for level_id, family, overrides, L, H in structural:
        for seed in first_unique_seeds(family, 50, overrides):
            params = {"v5_family": family, "seed": seed, **overrides}
            add_task(tasks, experiment="A_structural", family=family,
                     base_model_id=f"{level_id}:s{seed:04d}", seed=seed,
                     replicate=0, L=L, H=H, mode="first", timeout=60,
                     params=params)

    # B: 12 structure groups x 30 independent seeds x WIP 1/2/3 = 1080.
    # No timing repetition is counted as a new configuration.
    for family_index, family in enumerate(FAMILIES):
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
                "v5_family": family, "seed": seed, "n_parts": 3,
                "ops_per_part": 3 + family_index % 4,
                "n_machines": 1 + family_index % 3,
                "unobservable_ratio": (0.25, 0.50, 0.70)[family_index % 3],
            }
            for wip in (1, 2, 3):
                add_task(tasks, experiment="B_wip", family=family,
                         base_model_id=base, seed=seed, replicate=0,
                         L=28, H=240.0, mode="first", timeout=60,
                         params={**common, "wip_bound": wip})

    # C1: 12 families x 25 fixed TPN seeds x 15 consecutive L levels = 4500.
    # All families are retained. Each sweep holds the generated TPN and H fixed.
    for family in FAMILIES:
        for seed in range(25):
            _, default_L, default_H, _ = generate_v5_model(family, seed)
            start = max(2, default_L - 7)
            base = f"C:{family}:s{seed:02d}"
            params = {"v5_family": family, "seed": seed}
            for L in range(start, start + 15):
                add_task(tasks, experiment="C_Lsweep", family=family,
                         base_model_id=base, seed=seed, replicate=0,
                         L=L, H=default_H, mode="first", timeout=60,
                         params=params)

    # C2: preregistered complete-set pairing, 12 x 5 seeds x 15 = 900.
    # Seeds are evenly located in the C1 seed range and were not outcome-selected.
    for family in FAMILIES:
        for seed in (0, 6, 12, 18, 24):
            _, default_L, default_H, _ = generate_v5_model(family, seed)
            start = max(2, default_L - 7)
            base = f"C:{family}:s{seed:02d}"
            params = {"v5_family": family, "seed": seed}
            for L in range(start, start + 15):
                add_task(tasks, experiment="C_Lsweep", family=family,
                         base_model_id=base, seed=seed, replicate=0,
                         L=L, H=default_H, mode="complete", timeout=90,
                         params=params)

    # D: 12 families x 10 seeds x 10 monotone declared difficulty levels = 1200.
    for family in FAMILIES:
        spec = FAMILY_SPECS[family]
        for seed in range(10):
            for level in range(10):
                params = {
                    "v5_family": family, "seed": seed,
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
                         seed=seed, replicate=0, L=24 + 2 * level,
                         H=240.0 + 15.0 * level, mode="first", timeout=90,
                         params=params)
    return sorted(tasks, key=lambda row: row["task_id"])


def main():
    tasks = build_tasks()
    shards = 120
    OUT.mkdir(parents=True, exist_ok=True)
    unexpected = [
        path.name for path in OUT.glob("*.csv")
        if not SHARD_NAME.fullmatch(path.name)
    ]
    if unexpected:
        raise RuntimeError(
            f"unexpected task CSV filenames; quarantine before build: {unexpected}"
        )
    for shard in range(shards):
        path = OUT / f"shard_{shard:03d}.csv"
        if path.exists():
            path.unlink()
    buckets = [[] for _ in range(shards)]
    # Stable hash ordering distributes experiment families and timeout caps.
    for index, task in enumerate(tasks):
        buckets[index % shards].append(task)
    for shard, rows in enumerate(buckets):
        with (OUT / f"shard_{shard:03d}.csv").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    counts = {
        (name, mode): sum(t["experiment"] == name and t["mode"] == mode for t in tasks)
        for name, mode in (
            ("A_structural", "first"), ("B_wip", "first"),
            ("C_Lsweep", "first"), ("C_Lsweep", "complete"),
            ("D_difficult", "first"),
        )
    }
    manifest = {
        "version": "v5-formal-1",
        "frozen_before_execution": True,
        "pilot_run_id": "42052597272",
        "selection_policy": (
            "family-wide grids and preregistered seed strata; no outcome-based "
            "case removal or replacement"
        ),
        "tasks": len(tasks),
        "unique_configurations": len({t["configuration_id"] for t in tasks}),
        "distinct_bounded_problems": len({t["bounded_problem_id"] for t in tasks}),
        "distinct_base_models": len({t["base_model_id"] for t in tasks}),
        "timing_replicates_beyond_primary": sum(int(t["replicate"]) > 0 for t in tasks),
        "families": FAMILIES,
        "shards": shards,
        "experiments": {
            "A_structural": counts[("A_structural", "first")],
            "B_wip": counts[("B_wip", "first")],
            "C_first": counts[("C_Lsweep", "first")],
            "C_complete_paired": counts[("C_Lsweep", "complete")],
            "D_difficult": counts[("D_difficult", "first")],
        },
        "timeout_seconds": {"first": [60, 90], "complete": [90]},
        "worst_case_runner_hours": round(sum(int(t["timeout_s"]) for t in tasks) / 3600, 2),
        "max_tasks_per_shard": max(len(bucket) for bucket in buckets),
        "max_timeout_minutes_per_shard": round(
            max(sum(int(task["timeout_s"]) for task in bucket) for bucket in buckets) / 60, 2
        ),
        "estimated_artifact_mb_from_pilot": round(7.5 / 756 * len(tasks), 2),
        "runner": "GitHub-hosted ubuntu-22.04 standard runner only",
        "billing": (
            "repository visibility verified public via gh API; workflow uses only "
            "standard hosted labels, never larger or paid runners"
        ),
        "retention_days": 30,
    }
    assert manifest["tasks"] == 8280
    assert manifest["unique_configurations"] == 8280
    assert manifest["distinct_bounded_problems"] == 7380
    (ROOT / "protocol/v5_formal_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
