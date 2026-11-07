#!/usr/bin/env python3
"""Exact SCG-build equivalence between the frozen baseline and the optimized tree.

The optimized commit rewrites the DBM Floyd-Warshall inner loop, copies DBM
matrices by slice, caches marking-derived enabling data and caches DBM hashes.
Those changes are only acceptable if the constructed State Class Graph is
bit-for-bit identical.  This checker builds the SCG for a frozen model subset
(``depth_shallow`` and ``WIP_1`` strata of the frozen profiling sample, i.e.
one representative of every family plus its WIP-1 variant) with both versions
and compares an exact fingerprint: every class marking, enabled set, DBM
variable order and DBM matrix entry (as exact rationals), the edge list, the
completion set, the initial class and both SCG flags.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts" / "v6_diff_driver.py"
STRATA = ("depth_shallow", "L_mid")


def run_driver(root: Path, cell: dict, timeout: int) -> dict:
    spec = {
        "task_id": cell["task_id"],
        "params": json.loads(cell["params"]),
        "benchmark_family": cell["benchmark_family"],
        "stratum": cell["stratum"],
    }
    env = dict(os.environ)
    env.update({"PYTHONHASHSEED": "0", "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
    process = subprocess.Popen(
        [sys.executable, str(DRIVER), "--root", str(root), "--spec", json.dumps(spec),
         "--scg-only"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        if process.returncode != 0 or not stdout.strip():
            raise RuntimeError(f"driver exit={process.returncode}; stderr={stderr[-800:]}")
        return json.loads(stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return {"task_id": cell["task_id"], "execution_status": "TIMEOUT_WALL_CLOCK"}


def check_cell(payload):
    cell, baseline_root, optimized_root, timeout = payload
    baseline = run_driver(baseline_root, cell, timeout)
    optimized = run_driver(optimized_root, cell, timeout)
    fields = ("scg_sha256", "classes", "edges", "completion_ids",
              "state_limit_reached", "zero_time_unobservable_cycle")
    mismatches = []
    if baseline.get("execution_status") != "COMPLETED" or optimized.get("execution_status") != "COMPLETED":
        status = "CENSORED"
    else:
        for field in fields:
            if baseline.get(field) != optimized.get(field):
                mismatches.append({"field": field, "baseline": baseline.get(field),
                                   "optimized": optimized.get(field)})
        status = "MATCH" if not mismatches else "MISMATCH"
    return {
        "task_id": cell["task_id"],
        "benchmark_family": cell["benchmark_family"],
        "stratum": cell["stratum"],
        "status": status,
        "mismatches": mismatches,
        "baseline_wall_s": baseline.get("wall_s"),
        "optimized_wall_s": optimized.get("wall_s"),
        "classes": baseline.get("classes"),
        "edges": baseline.get("edges"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, default=ROOT / "protocol/v6_profile_sample_1.json")
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--optimized-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text())
    cells = [cell for cell in protocol["cells_detail"] if cell["stratum"] in STRATA]
    args.out.mkdir(parents=True, exist_ok=True)
    payloads = [(cell, args.baseline_root, args.optimized_root, args.timeout) for cell in cells]
    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for result in pool.map(check_cell, payloads):
            print(f"{result['benchmark_family']}/{result['stratum']}: {result['status']} "
                  f"classes={result['classes']} baseline={result['baseline_wall_s']}s "
                  f"optimized={result['optimized_wall_s']}s", flush=True)
            results.append(result)
    matched = [r for r in results if r["status"] == "MATCH"]
    mismatched = [r for r in results if r["status"] == "MISMATCH"]
    censored = [r for r in results if r["status"] == "CENSORED"]
    speedups = sorted(
        r["baseline_wall_s"] / r["optimized_wall_s"]
        for r in matched
        if r["baseline_wall_s"] and r["optimized_wall_s"]
    )
    summary = {
        "protocol": "v6-scg-equivalence-1",
        "source_sample": "protocol/v6_profile_sample_1.json",
        "strata": list(STRATA),
        "cells": len(results),
        "matched": len(matched),
        "mismatched": len(mismatched),
        "censored": len(censored),
        "mismatch_detail": mismatched,
        "censored_detail": censored,
        "scg_build_speedup_p50": speedups[len(speedups) // 2] if speedups else None,
        "scg_build_speedup_mean": round(sum(speedups) / len(speedups), 2) if speedups else None,
        "gates": {
            "SCG_BUILD_EQUIVALENCE_PASS": (
                not mismatched and not censored and len(matched) == len(results)
            ),
        },
    }
    (args.out / "scg_equivalence.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({key: summary[key] for key in (
        "cells", "matched", "mismatched", "censored",
        "scg_build_speedup_p50", "scg_build_speedup_mean", "gates")}, indent=2))
    return 0 if summary["gates"]["SCG_BUILD_EQUIVALENCE_PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
