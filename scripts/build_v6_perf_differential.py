#!/usr/bin/env python3
"""Build and freeze the v6 performance differential protocol (Layer A list).

Selection is structural and outcome-blind: for every model family take the
lexicographically first frozen Pilot task (replicate=0) in four strata that
exercise FIRST_CERTIFICATE, COMPLETE_SET, light structure and WIP-1.  Layer B
has no separate list: by pre-registered rule, every Layer A cell that is
censored on either side at the 60 s cap is re-run at 300 s.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PILOT_TASKS = ROOT / "protocol" / "v6_pilot_tasks"
OUT = ROOT / "protocol" / "v6_perf_differential_1.json"

PROTOCOL = "v6-performance-differential-1"
BASELINE_COMMIT = "10d2772c085076fe790d574eabd15d7423ac86d"
STRATA = ("depth_shallow", "L_mid", "L_high_complete", "WIP_1")
FIELDS = (
    "task_id", "benchmark_family", "stratum", "model_id", "seed", "replicate",
    "L", "H", "mode", "timeout_s", "expected_model_hash",
    "expected_structure_hash", "params",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--optimized-commit", required=True)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    rows = []
    for path in sorted(PILOT_TASKS.glob("shard_*.csv")):
        rows.extend(csv.DictReader(path.open()))
    families = sorted({row["benchmark_family"] for row in rows})
    cells = []
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
            cells.append({field: candidates[0][field] for field in FIELDS})
    if len(cells) != len(families) * len(STRATA):
        raise RuntimeError("unexpected cell count")
    payload = {
        "protocol_version": PROTOCOL,
        "frozen_before_execution": True,
        "baseline_commit": BASELINE_COMMIT,
        "optimized_commit": args.optimized_commit,
        "source": "protocol/v6_pilot_tasks",
        "selection_rule": (
            "one structural rule, no verdict or runtime input: for every family "
            "take the lexicographically first frozen Pilot task (replicate=0) in "
            "each of the four declared strata"
        ),
        "selection_uses_verdicts": False,
        "selection_uses_runtime": False,
        "layer_a": {
            "cells": len(cells),
            "cell_timeout_seconds": 60,
            "description": "exact functional differential at the Pilot wall-clock cap",
        },
        "layer_b_rule": {
            "rule": (
                "every Layer A cell that is censored (TIMEOUT_WALL_CLOCK or ERROR) "
                "on either side at 60 s is re-run on both sides at 300 s"
            ),
            "cell_timeout_seconds": 300,
            "note": (
                "Layer B results validate equivalence and performance only; they "
                "do not replace the formal 60 s budget and a pair still censored at "
                "300 s is not counted as PASS"
            ),
        },
        "comparison_fields": [
            "verdict", "inconclusive_reason", "certificate_mode",
            "enumeration_complete", "certificates", "critical_pair_count",
            "model_hash",
        ],
        "counters_compared": [
            "scg_classes", "scg_edges", "sync_states", "candidate_words",
            "candidate_words_processed", "temporal_states", "trace_records",
            "terminal_pairs", "lp_calls", "enclosure_rejected_pairs",
            "terminal_normal_records", "terminal_faulty_records",
        ],
        "acceptance": {
            "exact_match_required": True,
            "timeouts_count_as_pass": False,
            "one_sided_completion": "fail",
            "runtime_fields_compared": False,
            "note": (
                "verdicts, COMPLETE_SET certificates, enumeration completeness and "
                "all non-runtime workload counters must be identical; runtimes and "
                "instrumentation counters may differ"
            ),
        },
        "cells_detail": cells,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    args.out.write_text(encoded)
    print(json.dumps({
        "out": str(args.out),
        "cells": len(cells),
        "families": len(families),
        "strata": list(STRATA),
        "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
