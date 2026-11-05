#!/usr/bin/env python3
"""Build the frozen v4 formal matrix selected from the preregistered pilot."""
from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "protocol" / "v4_formal_tasks"
FIELDS = [
    "task_id", "experiment", "model", "seed", "replicate",
    "L", "H", "mode", "timeout_s", "params",
]


def params(*, n_parts, ops, wip, seed, inspections=0):
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


def add_replicates(tasks, *, experiment, model, seed, L, H, mode, timeout_s, model_params):
    for replicate in (0, 1, 2):
        tasks.append({
            "task_id": f"{experiment}:{model}:s{seed}:L{L}:{mode}:r{replicate}",
            "experiment": experiment,
            "model": model,
            "seed": seed,
            "replicate": replicate,
            "L": L,
            "H": H,
            "mode": mode,
            "timeout_s": timeout_s,
            "params": json.dumps(model_params, sort_keys=True),
        })


def build_tasks():
    tasks = []

    # Structural series: fixed WIP, L, H and verifier mode.  The plotting code
    # orders cells by measured |P|+|T|, never by these human-readable labels.
    structure = [
        ("S1", 1, 1), ("S2", 1, 2), ("S3", 1, 3),
        ("S4", 1, 4), ("S5", 2, 2), ("S6", 2, 3),
        ("S7", 2, 4),
    ]
    for model, n_parts, ops in structure:
        for seed in range(5):
            add_replicates(
                tasks, experiment="size", model=model, seed=seed,
                L=20, H=120.0, mode="first", timeout_s=60,
                model_params=params(n_parts=n_parts, ops=ops, wip=1, seed=seed),
            )

    # Concurrency boundary: full ops x WIP x seed factorial.  The 30 s budget
    # was frozen after the pilot: every ops=1 cell completed below 25 s, while
    # harder cells exceeded 120 s.  All outcomes remain in the denominator.
    for ops in (1, 2, 3):
        for wip in (1, 2, 3):
            model = f"W_o{ops}_w{wip}"
            for seed in range(5):
                add_replicates(
                    tasks, experiment="wip", model=model, seed=seed,
                    L=20, H=120.0, mode="first", timeout_s=30,
                    model_params=params(n_parts=3, ops=ops, wip=wip, seed=seed),
                )

    # L sensitivity: every seed and every consecutive level in the frozen
    # ranges.  COMPLETE_SET is required because first-certificate intentionally
    # stops at the first ambiguity and cannot measure total history workload.
    l_families = {
        "L1": {"n_parts": 1, "ops": 4, "inspections": 3, "levels": range(6, 14), "H": 60.0},
        "L2": {"n_parts": 2, "ops": 2, "inspections": 2, "levels": range(7, 14), "H": 80.0},
    }
    for model, cfg in l_families.items():
        for seed in range(5):
            model_params = params(
                n_parts=cfg["n_parts"], ops=cfg["ops"], wip=1,
                seed=seed, inspections=cfg["inspections"],
            )
            for L in cfg["levels"]:
                add_replicates(
                    tasks, experiment="Lsweep", model=model, seed=seed,
                    L=L, H=cfg["H"], mode="complete", timeout_s=60,
                    model_params=model_params,
                )

    return sorted(tasks, key=lambda row: row["task_id"])


def main():
    tasks = build_tasks()
    shards = 60
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
        "version": "v4-final",
        "frozen_before_execution": True,
        "pilot_run_id": 42004602864,
        "selection_rule": "all declared families, seeds, replicates and consecutive L levels; no outcome filtering",
        "tasks": len(tasks),
        "shards": shards,
        "replicates": 3,
        "seeds": [0, 1, 2, 3, 4],
        "experiments": {
            name: sum(task["experiment"] == name for task in tasks)
            for name in ("size", "wip", "Lsweep")
        },
        "limits": {
            "scg_class_limit": 50000,
            "wip_external_timeout_s": 30,
            "other_external_timeout_s": 60,
        },
        "timing_policy": "GitHub-hosted ubuntu-22.04 only; three repeats; no cross-source timing merge",
    }
    (ROOT / "protocol" / "v4_formal_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
