#!/usr/bin/env python3
"""Build and freeze the pre-registered v6 profiling sample.

Selection is structural and outcome-blind: from the frozen 288-cell Pilot task
list, for every model family take the first task (sorted by task id) in each of
six declared strata.  No Pilot verdict, runtime, completion status or failure
stage influences the choice.

The six strata are chosen to span the analysis space rather than to favour easy
cases: one light structural model (depth_shallow), one mid L-sweep cell
(L_mid), one COMPLETE_SET history cell (L_high_complete), the two heavy WIP
bounds (WIP_2, WIP_3) and the difficult cell.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PILOT_TASKS = ROOT / "protocol" / "v6_pilot_tasks"
OUT = ROOT / "protocol" / "v6_profile_sample_1.json"

PROTOCOL = "v6-profile-sample-1"
STRATA = (
    "depth_shallow",
    "L_mid",
    "L_high_complete",
    "WIP_2",
    "WIP_3",
    "difficult_first",
)
FIELDS = (
    "task_id", "benchmark_family", "stratum", "model_id", "seed", "replicate",
    "L", "H", "mode", "timeout_s", "expected_model_hash",
    "expected_structure_hash", "params",
)


def main() -> int:
    rows = []
    for path in sorted(PILOT_TASKS.glob("shard_*.csv")):
        rows.extend(csv.DictReader(path.open()))
    families = sorted({row["benchmark_family"] for row in rows})
    sample = []
    for family in families:
        for stratum in STRATA:
            candidates = sorted(
                (
                    row for row in rows
                    if row["benchmark_family"] == family
                    and row["stratum"] == stratum
                    and int(row["replicate"]) == 0
                ),
                key=lambda row: row["task_id"],
            )
            if not candidates:
                raise RuntimeError(f"no frozen Pilot task for {family}/{stratum}")
            source = candidates[0]
            sample.append({field: source[field] for field in FIELDS})
    if len(sample) != len(families) * len(STRATA):
        raise RuntimeError(f"expected {len(families) * len(STRATA)} cells, got {len(sample)}")
    if len({cell["task_id"] for cell in sample}) != len(sample):
        raise RuntimeError("duplicate task identity in profiling sample")
    payload = {
        "protocol_version": PROTOCOL,
        "frozen_before_execution": True,
        "selection_uses_verdicts": False,
        "selection_uses_runtime": False,
        "selection_rule": (
            "for each model family take the lexicographically first frozen Pilot "
            "task (replicate=0) in each of the six declared strata; no verdict or "
            "runtime is read"
        ),
        "source": "protocol/v6_pilot_tasks",
        "families": families,
        "strata": list(STRATA),
        "cells": len(sample),
        "cell_timeout_seconds": 60,
        "cells_detail": sample,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    OUT.write_text(encoded)
    print(json.dumps({
        "out": str(OUT.relative_to(ROOT)),
        "cells": len(sample),
        "families": len(families),
        "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
