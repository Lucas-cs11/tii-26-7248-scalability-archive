#!/usr/bin/env python3
"""Build and freeze the predeclared v6 cache-validation protocol.

The protocol is independent of the v6 Pilot: it is selected only from the
declared benchmark families, their frozen structural features, and one
deterministic seed per family.  No Pilot verdict, runtime, or completion
status is read while building it.

Each family is one shard (one worker), so tasks that share a model are
colocated and all but the first occurrence of a model can reuse the cached
SCG.  Row order inside a shard is the execution order and is frozen here.
"""
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


PROTOCOL = "v6-cache-validation-1"
OUT = ROOT / "protocol/v6_cache_validation_tasks"
MANIFEST = ROOT / "protocol/v6_cache_validation_manifest.json"
SHARDS = 12
TIMEOUT = 60
TASKS_PER_FAMILY = 8
EXPERIMENTS = {
    "C_Lsweep": "same default model at three structural L bounds plus one "
                "replica of the cold (L_low, first) cell",
    "B_wip": "same structural family at WIP bounds 1/2/3, each a cold model",
    "D_difficult": "Pilot-defined difficult parameter cell, a cold model",
}
FIELDS = [
    "task_id", "protocol_version", "experiment_id", "experiment",
    "benchmark_family", "stratum", "base_model_id", "model_id",
    "bounded_problem_id", "configuration_id", "seed", "replicate",
    "L", "H", "mode", "timeout_s", "expected_model_hash",
    "expected_structure_hash", "cache_group", "expected_cache_status", "params",
]


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _id(prefix, payload):
    return f"{prefix}-{_digest(payload)[:20]}"


def family_seed(family):
    """One deterministic seed per family; no run-time or verdict input."""
    return int(
        hashlib.sha256(f"{PROTOCOL}|{family}|0".encode()).hexdigest()[:8], 16
    ) % 100_000


def wip_parts_for(family_spec):
    """WIP cells use a part count strictly above the default one.

    The frozen Pilot used ``max(3, parts)``.  With a model-only cache key that
    value makes one WIP cell identical to the default model for families whose
    default WIP equals the WIP bound (F06/F07 with WIP=3, F08/F11 with WIP=2),
    which would turn an intended cold cell into a cache hit.  Raising the part
    count by one for factories that already have three parts keeps every WIP
    model distinct while leaving parts=1/2 families at the documented minimum
    of three.
    """
    return max(3, int(family_spec["parts"]) + 1)


def add_task(tasks, *, experiment, stratum, family, seed, params, L, H, mode,
             replicate, expected_cache_status):
    tpn, _, generated_h, _ = generate_v6_model(**params)
    if H is None:
        H = generated_h
    full_hash = model_hash(tpn)
    struct_hash = structure_hash(tpn)
    model_id = f"v6:{family}:{full_hash[:20]}"
    problem_payload = {
        "protocol": PROTOCOL, "model_hash": full_hash,
        "L": int(L), "H": float(H),
    }
    bounded_problem_id = _id("bp6", problem_payload)
    configuration_payload = {
        **problem_payload, "mode": mode, "replicate": int(replicate),
    }
    configuration_id = _id("cfg6", configuration_payload)
    tasks.append({
        "task_id": configuration_id,
        "protocol_version": PROTOCOL,
        "experiment_id": f"{PROTOCOL}-{experiment}",
        "experiment": experiment,
        "benchmark_family": family,
        "stratum": stratum,
        "base_model_id": model_id,
        "model_id": model_id,
        "bounded_problem_id": bounded_problem_id,
        "configuration_id": configuration_id,
        "seed": int(seed),
        "replicate": int(replicate),
        "L": int(L),
        "H": float(H),
        "mode": mode,
        "timeout_s": TIMEOUT,
        "expected_model_hash": full_hash,
        "expected_structure_hash": struct_hash,
        "cache_group": full_hash,
        "expected_cache_status": expected_cache_status,
        "params": json.dumps(params, sort_keys=True),
    })


def build_family_tasks(family, spec):
    """Build the eight frozen cells of one family in execution order."""
    seed = family_seed(family)
    default_params = {"v6_family": family, "seed": seed}
    _, default_l, default_h, _ = generate_v6_model(**default_params)
    parts = int(spec["parts"])
    ops = int(spec["ops"])
    wip_parts = wip_parts_for(spec)
    tasks = []

    # One model, four execution cells.  The CSV order is the cache order:
    # the first cell builds the SCG cold, the next three must reuse it.
    for stratum, L, mode, replicate, status in (
        ("L_low_first", max(2, parts * ops // 2), "first", 0, "miss"),
        ("L_mid_first", max(3, parts * ops), "first", 0, "hit"),
        ("L_high_complete", max(4, 2 * parts * ops + 2), "complete", 0, "hit"),
        ("L_low_first_replica", max(2, parts * ops // 2), "first", 1, "hit"),
    ):
        add_task(tasks, experiment="C_Lsweep", stratum=stratum, family=family,
                 seed=seed, params=default_params, L=L, H=default_h,
                 mode=mode, replicate=replicate,
                 expected_cache_status=status)

    # Three cold WIP models.  Their part count is frozen at build time only.
    for wip in (1, 2, 3):
        params = {**default_params, "n_parts": wip_parts, "wip_bound": wip}
        add_task(tasks, experiment="B_wip", stratum=f"WIP_{wip}",
                 family=family, seed=seed, params=params,
                 L=max(default_l, 2 * wip_parts * ops + 2), H=None,
                 mode="first", replicate=0, expected_cache_status="miss")

    # One cold difficult model; parameters mirror the frozen Pilot cell.
    difficult_parts = max(3, parts)
    difficult_params = {
        **default_params,
        "n_parts": difficult_parts,
        "ops_per_part": ops + 2,
        "wip_bound": 3,
        "inspection_locations": min(
            difficult_parts * (ops + 2), max(3, spec.get("inspections", 0) + 2)
        ),
        "route_choices": spec.get("routes", 0) + 2,
        "rework_locations": max(
            1, spec.get("rework_locations", spec.get("reworks", 0))
        ),
    }
    add_task(tasks, experiment="D_difficult", stratum="difficult_first",
             family=family, seed=seed, params=difficult_params,
             L=max(default_l, 2 * difficult_parts * (ops + 2) + 2), H=None,
             mode="first", replicate=0, expected_cache_status="miss")
    if len(tasks) != TASKS_PER_FAMILY:
        raise RuntimeError(
            f"{family}: expected {TASKS_PER_FAMILY} tasks, got {len(tasks)}"
        )
    return tasks


def build_tasks():
    tasks = []
    for family, spec in FAMILY_SPECS.items():
        tasks.extend(build_family_tasks(family, spec))
    return tasks


def cache_key_components():
    """The frozen cache key contract that the executor must implement."""
    return {
        "format": "v6-scg-cache",
        "format_version": 2,
        "model_hash": "<sha256 hex>",
        "source_sha": "<executed commit sha>",
        "scg_builder_sha": "<sha256 of src/scg/builder.py bytes>",
        "semantics_version": "v6-strong-time-1",
        "python": "3.13",
        "serialization": "json-fraction-v2",
        "key_material": (
            'key_material = {"format": "v6-scg-cache", "format_version": 2, '
            '"model_hash": <sha256 hex>, "source_sha": <executed commit sha>, '
            '"scg_builder_sha": <sha256 of src/scg/builder.py bytes>, '
            '"semantics_version": "v6-strong-time-1", "python": "3.13", '
            '"serialization": "json-fraction-v2"}'
        ),
        "cache_key": (
            'cache_key = sha256(json.dumps(key_material, sort_keys=True, '
            'separators=(",", ":")).encode()).hexdigest()'
        ),
    }


def cache_policy():
    return {
        "directory": (
            "cache directory is given by environment variable SCG_CACHE_DIR; "
            "the workflow sets SCG_CACHE_DIR=${{ runner.temp }}/scg_cache"
        ),
        "entry": "one single file <cache_key>.json per cached SCG",
        "atomic_write": "write a temporary file then atomically replace the entry",
        "corruption_policy": (
            "any deserialization or validation failure downgrades to miss"
        ),
        "max_entry_bytes": 64 * 1024 * 1024,
        "oversize_policy": (
            "an entry larger than 64 MiB is not written; the cell keeps status "
            '"miss" and the provenance records reason "entry_too_large" with '
            "store_ok=false"
        ),
        "disabled_policy": 'when the cache is disabled the status is "disabled"',
        "provenance_schema": {
            "hit": (
                "{cache_status: hit, key, key_material, reason: verified, "
                "load_s, build_s: null, store_s: null, payload_sha256, "
                "file_bytes, serializer}"
            ),
            "miss": (
                "{cache_status: miss, key, key_material, reason: stored | "
                "store_error:* | entry_too_large:*, store_ok, build_s, load_s: "
                "0.0, store_s, payload_sha256, file_bytes, serializer}"
            ),
            "disabled": "record scg_cache_provenance is null",
            "required_key_material": (
                "format, format_version, model_hash, source_sha, "
                "scg_builder_sha, semantics_version, python, serialization"
            ),
        },
    }


def build_manifest(tasks):
    per_family = {
        family: [task for task in tasks if task["benchmark_family"] == family]
        for family in FAMILY_SPECS
    }
    expected_cache = [
        task["expected_cache_status"] for task in tasks
    ]
    miss = expected_cache.count("miss")
    hit = expected_cache.count("hit")
    expected_per_family = {
        family: {
            "miss": sum(task["expected_cache_status"] == "miss" for task in rows),
            "hit": sum(task["expected_cache_status"] == "hit" for task in rows),
        }
        for family, rows in per_family.items()
    }
    if {frozenset(value.values()) for value in expected_per_family.values()} != {
        frozenset({5, 3})
    }:
        raise RuntimeError(f"unexpected per-family cache expectation: {expected_per_family}")
    differential_pairs = [
        [rows[0]["task_id"], rows[3]["task_id"]]
        for rows in per_family.values()
    ]
    experiments = {
        name: sum(task["experiment"] == name for task in tasks)
        for name in EXPERIMENTS
    }
    strata = {
        name: sum(task["stratum"] == name for task in tasks)
        for name in (
            "L_low_first", "L_mid_first", "L_high_complete",
            "L_low_first_replica", "WIP_1", "WIP_2", "WIP_3",
            "difficult_first",
        )
    }
    return {
        "version": PROTOCOL,
        "frozen_before_execution": True,
        "selection_uses_verdicts": False,
        "selection_rule": (
            "12 declared benchmark families x 8 preregistered structural "
            "strata; one sha256-derived deterministic seed per family; the "
            "family order is FAMILY_SPECS order. WIP cells are selected by a "
            "deterministic combination of n_parts and wip_bound (see "
            "wip_structure_rule), never by any observed result. No Pilot "
            "verdict, runtime, completion status, or prior result influences "
            "task selection."
        ),
        "wip_structure_rule": (
            "wip_parts = max(3, parts + 1); ensures WIP models are distinct "
            "from the default L-sweep model so the pre-registered miss/hit "
            "invariant holds"
        ),
        "semantics_gate": "protocol/v6_semantics_gate.json",
        "tasks": len(tasks),
        "families": len(FAMILY_SPECS),
        "family_names": list(FAMILY_SPECS),
        "tasks_per_family": TASKS_PER_FAMILY,
        "shards": SHARDS,
        "tasks_per_shard": TASKS_PER_FAMILY,
        "seeds": {family: family_seed(family) for family in FAMILY_SPECS},
        "experiments": experiments,
        "experiment_definitions": EXPERIMENTS,
        "strata": strata,
        "cell_timeout_seconds": TIMEOUT,
        "job_timeout_minutes": 30,
        "max_parallel": 6,
        "runner": "GitHub-hosted ubuntu-22.04 standard runner only",
        "artifact_retention_days": 30,
        "distinct_model_hashes": len({task["expected_model_hash"] for task in tasks}),
        "distinct_bounded_problems": len({task["bounded_problem_id"] for task in tasks}),
        "unique_configurations": len({task["configuration_id"] for task in tasks}),
        "duplicate_configurations": (
            len(tasks) - len({task["configuration_id"] for task in tasks})
        ),
        "expected_cache": {
            "total": {"miss": miss, "hit": hit},
            "per_shard": {"miss": 5, "hit": 3},
            "per_family": expected_per_family,
            "basis": (
                "Each family shard executes, in CSV order: the cold default "
                "model at L_low, the same model at L_mid, the same model at "
                "L_high_complete, and a replicate of the L_low cell. The "
                "first row builds the SCG (miss) and the next three reuse the "
                "same model_hash cache entry (hit). The three WIP bound cells "
                "and the difficult cell each introduce a distinct model "
                "(miss). Hence 5 misses and 3 hits per shard, 60/36 overall."
            ),
        },
        "differential_pairs": differential_pairs,
        "cache_key_components": cache_key_components(),
        "cache_policy": cache_policy(),
        "cache_group_rule": (
            "cache_group is the full model_hash; rows with the same value are "
            "one reuse group and must be colocated in one shard"
        ),
        "shard_planning_rule": (
            "one shard per benchmark family (12 shards x 8 tasks); every task "
            "with the same expected model_hash is placed in the same shard, "
            "i.e. the same worker; within each shard the CSV and execution "
            "order is fixed so the first row of a cache group is the cold "
            "build and all later rows of that group reuse it"
        ),
        "worst_case_compute_runner_minutes": len(tasks) * TIMEOUT / 60,
        "identity_rule": (
            "bounded_problem_id hashes {protocol, model_hash, L, H}; "
            "configuration_id hashes the bounded-problem payload plus mode "
            "and replicate; task_id equals configuration_id"
        ),
    }


def main():
    tasks = build_tasks()
    if len(tasks) != SHARDS * TASKS_PER_FAMILY:
        raise RuntimeError(
            f"expected {SHARDS * TASKS_PER_FAMILY} tasks, generated {len(tasks)}"
        )
    if len({task["configuration_id"] for task in tasks}) != len(tasks):
        raise RuntimeError("duplicate cache-validation configuration identity")
    family_names = list(FAMILY_SPECS)
    buckets = [
        [task for task in tasks if task["benchmark_family"] == family]
        for family in family_names
    ]
    if len(buckets) != SHARDS or any(len(rows) != TASKS_PER_FAMILY for rows in buckets):
        raise RuntimeError("shard plan does not match 12 x 8")

    OUT.mkdir(parents=True, exist_ok=True)
    expected_names = {f"shard_{index:03d}.csv" for index in range(SHARDS)}
    for old in OUT.glob("shard_*.csv"):
        if old.name not in expected_names:
            old.unlink()
    for index, rows in enumerate(buckets):
        with (OUT / f"shard_{index:03d}.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    manifest = build_manifest(tasks)
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
