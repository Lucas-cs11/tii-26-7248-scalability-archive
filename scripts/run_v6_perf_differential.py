#!/usr/bin/env python3
"""Run the frozen v6 performance differential against two checkouts.

Each cell is executed by ``scripts/v6_diff_driver.py`` once against the frozen
baseline commit and once against the optimized commit, under the same wall-clock
cap and the same thread/hash environment.  Layer A uses the Pilot 60 s cap;
every cell censored on either side is then re-run at 300 s (Layer B), exactly as
the frozen protocol prescribes.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import signal
import subprocess
import sys
import time
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
        "benchmark_family": cell["benchmark_family"],
        "stratum": cell["stratum"],
    }
    env = dict(os.environ)
    env.update({
        "V6_PROFILE": "0",
        "PYTHONHASHSEED": "0",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    })
    env.pop("SCG_CACHE_DIR", None)
    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, str(DRIVER), "--root", str(root), "--spec", json.dumps(spec)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        wall = time.perf_counter() - started
        if process.returncode != 0 or not stdout.strip():
            raise RuntimeError(
                f"driver exit={process.returncode}; stderr={stderr[-800:]}"
            )
        record = json.loads(stdout.strip().splitlines()[-1])
        record["root"] = str(root)
        record["cell_timeout_s"] = timeout
        record["runner_wall_s"] = round(wall, 3)
        return record
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return {
            "task_id": cell["task_id"],
            "root": str(root),
            "execution_status": "TIMEOUT_WALL_CLOCK",
            "verdict": None,
            "wall_s": None,
            "cell_timeout_s": timeout,
            "runner_wall_s": round(time.perf_counter() - started, 3),
            "error": None,
        }


def run_cell(payload) -> tuple:
    cell, baseline_root, optimized_root, timeout = payload
    baseline = run_driver(baseline_root, cell, timeout)
    optimized = run_driver(optimized_root, cell, timeout)
    print(
        f"{cell['benchmark_family']}/{cell['stratum']}: "
        f"baseline={baseline.get('execution_status')} {baseline.get('wall_s')}s "
        f"optimized={optimized.get('execution_status')} {optimized.get('wall_s')}s",
        flush=True,
    )
    return cell["task_id"], baseline, optimized


def run_layer(cells, baseline_root, optimized_root, timeout, jobs, out_path):
    payloads = [(cell, baseline_root, optimized_root, timeout) for cell in cells]
    results = {}
    with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as pool:
        for task_id, baseline, optimized in pool.map(run_cell, payloads):
            results[task_id] = {"baseline": baseline, "optimized": optimized}
    with out_path.open("w") as handle:
        for task_id in sorted(results):
            handle.write(json.dumps({"task_id": task_id, **results[task_id]},
                                    default=str, sort_keys=True) + "\n")
    return results


def censored(result: dict) -> bool:
    return result.get("execution_status") != "COMPLETED"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, default=ROOT / "protocol/v6_perf_differential_1.json")
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--optimized-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    cells = protocol["cells_detail"]
    cap_a = int(protocol["layer_a"]["cell_timeout_seconds"])
    cap_b = int(protocol["layer_b_rule"]["cell_timeout_seconds"])
    started = time.time()
    print(f"Layer A: {len(cells)} cells, cap {cap_a}s, jobs {args.jobs}", flush=True)
    layer_a = run_layer(
        cells, args.baseline_root, args.optimized_root, cap_a, args.jobs,
        args.out / "layer_a_results.jsonl",
    )
    layer_b_cells = [
        cell for cell in cells
        if censored(layer_a[cell["task_id"]]["baseline"])
        or censored(layer_a[cell["task_id"]]["optimized"])
    ]
    print(f"Layer B: {len(layer_b_cells)} censored cells, cap {cap_b}s", flush=True)
    layer_b = {}
    if layer_b_cells:
        layer_b = run_layer(
            layer_b_cells, args.baseline_root, args.optimized_root, cap_b, args.jobs,
            args.out / "layer_b_results.jsonl",
        )
    summary = {
        "protocol_version": protocol["protocol_version"],
        "baseline_commit": protocol["baseline_commit"],
        "optimized_commit": protocol["optimized_commit"],
        "baseline_root": str(args.baseline_root),
        "optimized_root": str(args.optimized_root),
        "layer_a_cells": len(cells),
        "layer_a_cap_s": cap_a,
        "layer_b_cells": len(layer_b_cells),
        "layer_b_cap_s": cap_b,
        "jobs": args.jobs,
        "wall_seconds": round(time.time() - started, 2),
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    (args.out / "run_manifest.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
