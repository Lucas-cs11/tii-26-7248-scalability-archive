#!/usr/bin/env python3
"""Build the pre-registered Modal environment pilot (64 tasks).

Selection is structural and reproducible:
* 48 cells = the frozen differential v1 cells (12 families x 4 strata), whose
  reference verdicts already exist in the audited GitHub pilot run
  42348132809;
* 16 cells = one per family from the frozen 96-cell difficulty protocol,
  preferring the first cell whose audited reference is INC (SCG state limit),
  falling back to the first timeout cell in that family, plus four extra
  F06/F07/F11/F12 cells with INC/timeout references.

Shards are grouped by model identity so that same-model cells exercise the
per-container SCG cache (hit and miss).

No Modal call happens here; this only writes protocol/v6_modal_pilot_tasks.json.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
OUT = ROOT / "protocol" / "v6_modal_pilot_tasks.json"
CELL_TIMEOUT = 60


def load_records(artifacts: Path) -> dict:
    records = {}
    for path in sorted(artifacts.glob("*/raw/shard_*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                records[record.get("task_id")] = record
    return records


def read_tasks(directory: Path) -> dict:
    tasks = {}
    for path in sorted(directory.glob("shard_*.csv")):
        for row in csv.DictReader(path.open()):
            tasks[row["task_id"]] = row
    return tasks


def reference(record) -> dict:
    return {
        "verdict": record.get("verdict"),
        "execution_status": record.get("execution_status"),
        "inconclusive_reason": record.get("inconclusive_reason"),
        "wall_s": record.get("wall_s"),
    }


def cell_from_row(row, record, source_run: str, protocol: str) -> dict:
    return {
        "pilot_task_id": f"pilot:{row['task_id']}",
        "source_task_id": row["task_id"],
        "protocol": protocol,
        "benchmark_family": row["benchmark_family"],
        "stratum": row.get("stratum") or row.get("experiment"),
        "experiment": row.get("experiment"),
        "mode": row["mode"],
        "L": int(row["L"]),
        "H": float(row["H"]),
        "timeout_s": CELL_TIMEOUT,
        "params": json.loads(row["params"]),
        "model_hash": row.get("expected_model_hash"),
        "reference": reference(record),
        "reference_run": source_run,
    }


def main() -> int:
    differential = json.loads((ROOT / "protocol/v6_perf_differential_1.json").read_text())
    pilot_tasks = read_tasks(ROOT / "protocol/v6_pilot_tasks")
    pilot_records = load_records(WORK / "v6_optimized_pilot_run_42348132809")
    cache_tasks = read_tasks(ROOT / "protocol/v6_cache_validation_tasks")
    cache_records = load_records(WORK / "v6_cache_validation_opt_42347953385")

    cells = []
    for entry in differential["cells_detail"]:
        row = pilot_tasks[entry["task_id"]]
        record = pilot_records[entry["task_id"]]
        cells.append(cell_from_row(row, record, "42348132809", "v6-pilot-1"))

    by_family = {}
    for task_id, row in sorted(cache_tasks.items()):
        by_family.setdefault(row["benchmark_family"], []).append((task_id, row))
    extra_families = {"F06_parallel_parts", "F07_shared_contention",
                      "F11_resource_cycle", "F12_sparse_cycle"}
    chosen = []
    seen = set()
    for family in sorted(by_family):
        candidates = by_family[family]
        inc = [item for item in candidates
               if (cache_records.get(item[0]) or {}).get("verdict") == "INC"]
        timeout = [item for item in candidates
                   if (cache_records.get(item[0]) or {}).get("execution_status") == "TIMEOUT_WALL_CLOCK"]
        pick = (inc or timeout or candidates)[0]
        if pick[0] in seen:
            continue
        seen.add(pick[0])
        chosen.append(pick)
    for family in sorted(extra_families):
        candidates = [item for item in by_family[family] if item[0] not in seen]
        inc = [item for item in candidates
               if (cache_records.get(item[0]) or {}).get("verdict") == "INC"]
        pick = (inc or candidates)[0]
        if pick[0] in seen:
            continue
        seen.add(pick[0])
        chosen.append(pick)
    while len(chosen) < 16:
        for task_id, row in sorted(cache_tasks.items()):
            if task_id not in seen:
                chosen.append((task_id, row))
                seen.add(task_id)
                if len(chosen) == 16:
                    break
    for task_id, row in chosen[:16]:
        record = cache_records[task_id]
        cells.append(cell_from_row(row, record, "42347953385", "v6-cache-validation-1"))

    if len(cells) != 64:
        raise RuntimeError(f"expected 64 pilot cells, got {len(cells)}")
    if len({cell["pilot_task_id"] for cell in cells}) != 64:
        raise RuntimeError("duplicate pilot task id")

    # Shard by model identity so same-model L/mode cells share one container cache.
    by_model = {}
    for cell in cells:
        by_model.setdefault(cell["model_hash"] or cell["pilot_task_id"], []).append(cell)
    shards = []
    current = []
    for model, group in sorted(by_model.items(), key=lambda item: item[0]):
        if len(current) + len(group) > 12 and current:
            shards.append(current)
            current = []
        current.extend(group)
    if current:
        shards.append(current)
    shard_payload = [
        {"shard_id": f"pilot_{index:03d}", "tasks": group}
        for index, group in enumerate(shards)
    ]
    payload = {
        "protocol": "v6-modal-pilot-1",
        "frozen_before_execution": True,
        "purpose": (
            "Modal environment portability check before the formal 8,280 matrix: "
            "verdict consistency against audited GitHub records, memory behaviour, "
            "timeout semantics, cache hit/miss, checkpoint recovery"
        ),
        "cell_timeout_seconds": CELL_TIMEOUT,
        "cells": len(cells),
        "shards": len(shard_payload),
        "reference_runs": {"42348132809": "optimized 288-cell GitHub pilot",
                           "42347953385": "optimized 96-cell difficulty sample"},
        "cells_detail": cells,
        "shard_plan": shard_payload,
        "selection_rule": (
            "48 frozen differential cells plus 16 frozen difficulty cells per family "
            "(prefer INC reference, else timeout, plus four heavy-family extras)"
        ),
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "out": str(OUT.relative_to(ROOT)),
        "cells": len(cells),
        "shards": len(shard_payload),
        "tasks_per_shard": [len(shard["tasks"]) for shard in shard_payload],
        "reference_status": {
            status: sum(1 for cell in cells if cell["reference"]["execution_status"] == status)
            for status in ("COMPLETED", "TIMEOUT_WALL_CLOCK", "ERROR")
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
