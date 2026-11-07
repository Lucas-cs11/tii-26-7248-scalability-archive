#!/usr/bin/env python3
"""Exact synchronizer equivalence for the cells censored by the differential.

The frozen performance differential re-runs 48 Pilot cells at 60 s and, for
censored pairs, at 300 s.  Eighteen pairs still could not be compared because
the full temporal expansion of those cells does not finish in either version
(the probe at 900 s also timed out), so their full-cell verdicts cannot be
compared.  For those cells the synchronizer *does* finish quickly in both
versions, and the synchronizer is exactly the stage whose transition
memoisation was added.  This check builds the completion-truncated SCG and the
observation synchronizer with both versions and compares an exact automaton
fingerprint: every state (X_N, X_F) class-id set, every labelled edge and the
ordered candidate-word list.
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


def run_driver(root: Path, cell: dict, timeout: int) -> dict:
    spec = {
        "task_id": cell["task_id"],
        "params": json.loads(cell["params"]),
        "L": int(cell["L"]),
        "H": float(cell["H"]),
        "mode": cell["mode"],
    }
    env = dict(os.environ)
    env.update({"PYTHONHASHSEED": "0", "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
    process = subprocess.Popen(
        [sys.executable, str(DRIVER), "--root", str(root), "--spec", json.dumps(spec),
         "--sync-only"],
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


def check(payload):
    cell, baseline_root, optimized_root, timeout = payload
    base = run_driver(baseline_root, cell, timeout)
    opt = run_driver(optimized_root, cell, timeout)
    fields = ("sync_sha256", "sync_states", "sync_edges", "candidate_words",
              "scg_classes", "scg_edges")
    mismatches = []
    if base.get("execution_status") != "COMPLETED" or opt.get("execution_status") != "COMPLETED":
        status = "CENSORED"
    else:
        for field in fields:
            if base.get(field) != opt.get(field):
                mismatches.append({"field": field, "baseline": base.get(field),
                                   "optimized": opt.get(field)})
        status = "MATCH" if not mismatches else "MISMATCH"
    return {
        "task_id": cell["task_id"],
        "benchmark_family": cell["benchmark_family"],
        "stratum": cell["stratum"],
        "mode": cell["mode"],
        "status": status,
        "mismatches": mismatches,
        "sync_states": base.get("sync_states"),
        "sync_edges": base.get("sync_edges"),
        "candidate_words": base.get("candidate_words"),
        "baseline_wall_s": base.get("wall_s"),
        "optimized_wall_s": opt.get("wall_s"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, default=ROOT / "protocol/v6_perf_differential_1.json")
    parser.add_argument("--differential", type=Path,
                        default=ROOT / "work/v6_perf_differential_1/differential_results.json")
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--optimized-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text())
    census = json.loads(args.differential.read_text())
    censored_ids = {entry["task_id"] for entry in census.get("censored_detail", [])}
    cells = [cell for cell in protocol["cells_detail"] if cell["task_id"] in censored_ids]
    args.out.mkdir(parents=True, exist_ok=True)
    payloads = [(cell, args.baseline_root, args.optimized_root, args.timeout) for cell in cells]
    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for result in pool.map(check, payloads):
            print(f"{result['benchmark_family']}/{result['stratum']}: {result['status']} "
                  f"states={result['sync_states']} words={result['candidate_words']}", flush=True)
            results.append(result)
    matched = [r for r in results if r["status"] == "MATCH"]
    mismatched = [r for r in results if r["status"] == "MISMATCH"]
    censored = [r for r in results if r["status"] == "CENSORED"]
    summary = {
        "protocol": "v6-sync-equivalence-1",
        "source_differential": str(args.differential),
        "censored_cells": len(cells),
        "matched": len(matched),
        "mismatched": len(mismatched),
        "censored": len(censored),
        "mismatch_detail": mismatched,
        "censored_detail": censored,
        "gates": {
            "SYNCHRONIZER_EQUIVALENCE_PASS": (
                not mismatched and not censored and len(matched) == len(cells)
            ),
        },
    }
    (args.out / "sync_equivalence.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({key: summary[key] for key in
                      ("censored_cells", "matched", "mismatched", "censored", "gates")}, indent=2))
    return 0 if summary["gates"]["SYNCHRONIZER_EQUIVALENCE_PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
