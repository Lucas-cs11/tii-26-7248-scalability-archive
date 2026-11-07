#!/usr/bin/env python3
"""Build and freeze the predeclared 288-cell v6 correctness/budget Pilot."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.run_cell import model_hash, structure_hash
from src.benchmarks.v6_families import FAMILY_SPECS, generate_v6_model


PROTOCOL = "v6-pilot-1"
OUT = ROOT / "protocol/v6_pilot_tasks"
SHARDS = 24
TIMEOUT = 60
FIELDS = [
    "task_id", "protocol_version", "experiment_id", "experiment",
    "benchmark_family", "stratum", "base_model_id", "model_id",
    "bounded_problem_id", "configuration_id", "seed", "replicate",
    "L", "H", "mode", "timeout_s", "expected_model_hash",
    "expected_structure_hash", "params",
]


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _id(prefix, payload):
    return f"{prefix}-{_digest(payload)[:20]}"


def fixed_seeds(family):
    return tuple(
        int(hashlib.sha256(f"{PROTOCOL}|{family}|{index}".encode()).hexdigest()[:8], 16)
        % 100_000
        for index in range(2)
    )


def observation_strata(total_transfers, default_ratio):
    """Choose three distinct transfer-label counts, excluding the default model."""
    default_count = int(total_transfers * default_ratio)
    available = [count for count in range(total_transfers + 1) if count != default_count]
    chosen = []
    for target in (0.2 * total_transfers, 0.5 * total_transfers, 0.8 * total_transfers):
        count = min((value for value in available if value not in chosen),
                    key=lambda value: (abs(value - target), value))
        chosen.append(count)
    return tuple((count + 0.5) / total_transfers for count in chosen)


def add_task(tasks, *, experiment, stratum, family, seed, params, L, H, mode,
             base_model_id=None):
    tpn, _, generated_h, _ = generate_v6_model(**params)
    if H is None:
        H = generated_h
    full_hash = model_hash(tpn)
    struct_hash = structure_hash(tpn)
    model_id = f"v6:{family}:{full_hash[:20]}"
    base_model_id = base_model_id or model_id
    problem_payload = {
        "protocol": PROTOCOL, "model_hash": full_hash, "L": int(L), "H": float(H),
    }
    bounded_problem_id = _id("bp6", problem_payload)
    configuration_payload = {
        **problem_payload, "mode": mode, "replicate": 0,
    }
    configuration_id = _id("cfg6", configuration_payload)
    tasks.append({
        "task_id": configuration_id,
        "protocol_version": PROTOCOL,
        "experiment_id": f"{PROTOCOL}-{experiment}",
        "experiment": experiment,
        "benchmark_family": family,
        "stratum": stratum,
        "base_model_id": base_model_id,
        "model_id": model_id,
        "bounded_problem_id": bounded_problem_id,
        "configuration_id": configuration_id,
        "seed": seed,
        "replicate": 0,
        "L": int(L),
        "H": float(H),
        "mode": mode,
        "timeout_s": TIMEOUT,
        "expected_model_hash": full_hash,
        "expected_structure_hash": struct_hash,
        "params": json.dumps(params, sort_keys=True),
    })


def build_tasks():
    tasks = []
    for family, spec in FAMILY_SPECS.items():
        for seed_index, seed in enumerate(fixed_seeds(family)):
            default_params = {"v6_family": family, "seed": seed}
            _, default_l, default_h, _ = generate_v6_model(**default_params)
            parts = spec["parts"]
            ops = spec["ops"]

            # Two controlled structural endpoints; all other family parameters fixed.
            for label, depth in (("shallow", max(2, ops - 2)), ("deep", ops + 2)):
                params = {**default_params, "ops_per_part": depth}
                add_task(tasks, experiment="A_structural", stratum=f"depth_{label}",
                         family=family, seed=seed, params=params,
                         L=max(4, 2 * parts * depth + 2), H=None, mode="first")

            # Three observation-density strata on the same default structure.
            ratios = observation_strata(parts * ops, spec["unobs"])
            for label, ratio in zip(("low", "mid", "high"), ratios):
                params = {**default_params, "unobservable_ratio": ratio}
                add_task(tasks, experiment="A_structural", stratum=f"observation_{label}",
                         family=family, seed=seed, params=params,
                         L=default_l, H=default_h, mode="first")

            # Paired WIP=1/2/3; base identity excludes the controlled WIP value.
            wip_parts = max(3, parts)
            wip_base = f"v6pilot:B:{family}:seed{seed}:paired"
            for wip in (1, 2, 3):
                params = {
                    **default_params, "n_parts": wip_parts,
                    "wip_bound": wip,
                }
                add_task(tasks, experiment="B_wip", stratum=f"WIP_{wip}",
                         family=family, seed=seed, params=params,
                         L=max(default_l, 2 * wip_parts * ops + 2), H=None,
                         mode="first", base_model_id=wip_base)

            # Fixed-model L scan: same model/H; three structurally defined depths.
            l_values = sorted({max(2, parts * ops // 2), max(3, parts * ops),
                               max(4, 2 * parts * ops + 2)})
            while len(l_values) < 3:
                l_values.append(l_values[-1] + 1)
            for index, value in enumerate(l_values[:3]):
                mode = "complete" if index == 2 else "first"
                add_task(tasks, experiment="C_Lsweep",
                         stratum=("L_low", "L_mid", "L_high_complete")[index],
                         family=family, seed=seed, params=default_params,
                         L=value, H=default_h, mode=mode)

            # One difficult cell per seed; the two seeds split analysis modes.
            difficult_parts = max(3, parts)
            difficult_params = {
                **default_params,
                "n_parts": difficult_parts,
                "ops_per_part": ops + 2,
                "wip_bound": 3,
                "inspection_locations": min(difficult_parts * (ops + 2),
                                            max(3, spec.get("inspections", 0) + 2)),
                "route_choices": spec.get("routes", 0) + 2,
                "rework_locations": max(1, spec.get("reworks", 0)),
            }
            add_task(tasks, experiment="D_difficult",
                     stratum="difficult_complete" if seed_index else "difficult_first",
                     family=family, seed=seed, params=difficult_params,
                     L=max(default_l, 2 * difficult_parts * (ops + 2) + 2),
                     H=None, mode="complete" if seed_index else "first")

    tasks.sort(key=lambda row: row["task_id"])
    return tasks


def main():
    tasks = build_tasks()
    if len(tasks) != 288:
        raise RuntimeError(f"expected 288 tasks, generated {len(tasks)}")
    if len({task["configuration_id"] for task in tasks}) != len(tasks):
        raise RuntimeError("duplicate Pilot configuration identity")
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("shard_*.csv"):
        old.unlink()
    buckets = [[] for _ in range(SHARDS)]
    for index, task in enumerate(tasks):
        buckets[index % SHARDS].append(task)
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
        "tasks": len(tasks),
        "unique_configurations": len({task["configuration_id"] for task in tasks}),
        "distinct_model_hashes": len({task["expected_model_hash"] for task in tasks}),
        "distinct_bounded_problems": len({task["bounded_problem_id"] for task in tasks}),
        "duplicate_configurations": len(tasks) - len({task["configuration_id"] for task in tasks}),
        "families": list(FAMILY_SPECS),
        "seeds": {family: list(fixed_seeds(family)) for family in FAMILY_SPECS},
        "tasks_per_family": 24,
        "shards": SHARDS,
        "tasks_per_shard": 12,
        "experiments": experiments,
        "cell_timeout_seconds": TIMEOUT,
        "job_timeout_minutes": 30,
        "max_parallel": 6,
        "worst_case_compute_runner_minutes": len(tasks) * TIMEOUT / 60,
        "runner": "GitHub-hosted ubuntu-22.04 standard runner only",
        "billing": "public repository standard runner; no larger or paid runner",
        "artifact_retention_days": 30,
        "selection_rule": "12 families x 2 deterministic seeds x 12 preregistered strata; no verdict-based selection",
    }
    (ROOT / "protocol/v6_pilot_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
