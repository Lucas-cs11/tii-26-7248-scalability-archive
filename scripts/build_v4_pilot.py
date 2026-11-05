#!/usr/bin/env python3
"""Build the preregistered v4 pilot matrix without inspecting outcomes."""
from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "protocol" / "v4_pilot_tasks"
FIELDS = [
    "task_id", "experiment", "model", "seed", "replicate",
    "L", "H", "mode", "timeout_s", "params",
]


def base_params(*, n_parts, ops, wip, seed, inspections=0):
    return {
        "n_machines": 4,
        "n_parts": n_parts,
        "ops_per_part": ops,
        "wip_bound": wip,
        "unobservable_ratio": 0.5,
        "fault_locations": min(2, n_parts * ops),
        "interval_width_factor": 1.0,
        "adjustable_ratio": 0.25,
        "seed": seed,
        "n_configs": 1,
        "inspection_locations": inspections,
    }


def build_tasks():
    tasks = []

    # L pilot: every seed and every consecutive L in the declared range is kept.
    # The two families differ in structure; selection is fixed before outcomes.
    l_families = {
        "LP1": {"n_parts": 1, "ops": 4, "inspections": 3, "L": range(4, 15), "H": 60.0},
        "LP2": {"n_parts": 2, "ops": 2, "inspections": 2, "L": range(4, 17), "H": 80.0},
    }
    for model, cfg in l_families.items():
        for seed in (0, 1, 2):
            params = base_params(
                n_parts=cfg["n_parts"], ops=cfg["ops"], wip=1,
                seed=seed, inspections=cfg["inspections"],
            )
            for L in cfg["L"]:
                tasks.append({
                    "task_id": f"pilotL:{model}:s{seed}:L{L}",
                    "experiment": "pilot_L",
                    "model": model,
                    "seed": seed,
                    "replicate": 0,
                    "L": L,
                    "H": cfg["H"],
                    "mode": "complete",
                    "timeout_s": 120,
                    "params": json.dumps(params, sort_keys=True),
                })

    # WIP pilot: full ops x WIP x seed factorial, including all difficult cells.
    for ops in (1, 2, 3):
        for wip in (1, 2, 3):
            for seed in (0, 1, 2):
                model = f"WP_o{ops}_w{wip}"
                params = base_params(
                    n_parts=3, ops=ops, wip=wip, seed=seed
                )
                tasks.append({
                    "task_id": f"pilotW:{model}:s{seed}",
                    "experiment": "pilot_WIP",
                    "model": model,
                    "seed": seed,
                    "replicate": 0,
                    "L": 20,
                    "H": 120.0,
                    "mode": "first",
                    "timeout_s": 120,
                    "params": json.dumps(params, sort_keys=True),
                })
    return sorted(tasks, key=lambda r: r["task_id"])


def main():
    tasks = build_tasks()
    shards = 30
    OUT.mkdir(parents=True, exist_ok=True)
    for path in OUT.glob("shard_*.csv"):
        path.unlink()
    buckets = [[] for _ in range(shards)]
    for index, task in enumerate(tasks):
        buckets[index % shards].append(task)
    for shard, rows in enumerate(buckets):
        with (OUT / f"shard_{shard:03d}.csv").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    manifest = {
        "phase": "pilot",
        "frozen_before_execution": True,
        "selection_rule": "all declared families, seeds, and consecutive L levels; no outcome filtering",
        "tasks": len(tasks),
        "shards": shards,
        "experiments": {
            "pilot_L": sum(t["experiment"] == "pilot_L" for t in tasks),
            "pilot_WIP": sum(t["experiment"] == "pilot_WIP" for t in tasks),
        },
        "scg_class_limit": 50000,
        "external_timeout_s": 120,
    }
    (ROOT / "protocol" / "v4_pilot_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
