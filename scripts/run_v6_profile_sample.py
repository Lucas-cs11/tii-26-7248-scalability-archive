#!/usr/bin/env python3
"""Execute the frozen v6 profiling sample and keep every raw record.

Each cell runs as its own ``runner/run_cell.py`` subprocess under the same
60 s external wall-clock cap as the Pilot, with stage instrumentation enabled
(``V6_PROFILE=1``) and a per-run SCG cache directory.  A wall-clock kill is
recorded as ``TIMEOUT_WALL_CLOCK`` with the last atomic checkpoint, never as a
completed cell and never with zero-filled later-stage counters.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_CELL = ROOT / "runner" / "run_cell.py"


def peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / (1024 * 1024 if sys.platform == "darwin" else 1024), 3)


def run_monitored(command, env, timeout):
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env, start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if time.monotonic() >= deadline:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
        time.sleep(0.05)
    stdout, stderr = process.communicate()
    return process.returncode, stdout, stderr


def execute_cell(cell, out_dir: Path, cache_dir: Path, timeout: int):
    spec = {
        "tag": cell["task_id"],
        "protocol_version": cell.get("protocol_version"),
        "experiment_id": cell.get("experiment", "profile"),
        "benchmark_family": cell["benchmark_family"],
        "model_id": cell.get("model_id"),
        "bounded_problem_id": cell["task_id"],
        "configuration_id": cell["task_id"],
        "experiment": cell.get("experiment", "profile"),
        "model": cell.get("model", cell["model_id"]),
        "seed": int(cell["seed"]),
        "replicate": int(cell.get("replicate", 0)),
        "L": int(cell["L"]),
        "H": float(cell["H"]),
        "mode": cell["mode"],
        "timeout_s": timeout,
        "params": json.loads(cell["params"]),
        "expected_model_hash": cell.get("expected_model_hash"),
        "expected_structure_hash": cell.get("expected_structure_hash"),
    }
    checkpoint_path = out_dir / "checkpoints" / f"{cell['task_id']}.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
    env = dict(os.environ)
    env.update({
        "CHECKPOINT_PATH": str(checkpoint_path),
        "V6_PROFILE": "1",
        "SCG_CACHE_DIR": str(cache_dir),
        "EXECUTED_SOURCE_SHA": os.environ.get("EXECUTED_SOURCE_SHA", "local"),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "PYTHONHASHSEED": "0",
    })
    started = time.time()
    try:
        returncode, stdout, stderr = run_monitored(
            [sys.executable, str(RUN_CELL), json.dumps(spec)], env, timeout
        )
        if returncode != 0 or not stdout.strip():
            raise RuntimeError(f"run_cell exit={returncode}; stderr={stderr[-1500:]}")
        record = json.loads(stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        partial = {}
        if checkpoint_path.is_file():
            try:
                partial = json.loads(checkpoint_path.read_text())
            except Exception:  # noqa: BLE001
                partial = {}
        record = dict(partial)
        record.setdefault("tag", cell["task_id"])
        record.setdefault("mode", cell["mode"])
        record.update({
            "verdict": None,
            "execution_status": "TIMEOUT_WALL_CLOCK",
            "termination_status": "TIMEOUT_WALL_CLOCK",
            "analysis_status": "NOT_RUN",
            "timeout_reason": partial.get("failure_stage", "UNKNOWN"),
            "partial_workload": bool(partial.get("counters")),
        })
    except Exception as exc:  # noqa: BLE001
        record = {
            "tag": cell["task_id"], "execution_status": "ERROR",
            "verdict": None, "error": f"{type(exc).__name__}: {exc}",
            "termination_status": "ERROR", "failure_stage": "PROFILE_RUNNER",
        }
    record["wall_s"] = round(time.time() - started, 3)
    record["task_id"] = cell["task_id"]
    record["benchmark_family"] = cell["benchmark_family"]
    record["stratum"] = cell["stratum"]
    for key in ("L", "H", "mode", "params", "seed", "replicate"):
        record.setdefault(key, cell.get(key))
    return record


def run_family(payload):
    family, cells, out_dir, cache_dir, timeout = payload
    records = [execute_cell(cell, out_dir, cache_dir, timeout) for cell in cells]
    path = out_dir / "raw" / f"{family}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
    return family, records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=Path, default=ROOT / "protocol/v6_profile_sample_1.json")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    sample = json.loads(args.sample.read_text())
    out_dir = args.out or (ROOT / "work" / f"v6_profile_{time.strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "scg_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    grouped = {}
    for cell in sample["cells_detail"]:
        grouped.setdefault(cell["benchmark_family"], []).append(cell)
    started = time.time()
    payloads = [
        (family, cells, out_dir, cache_dir, args.timeout)
        for family, cells in sorted(grouped.items())
    ]
    results = {}
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for family, records in pool.map(run_family, payloads):
            results[family] = records
            print(f"{family}: {len(records)} cells", flush=True)
    summary = {
        "protocol_version": sample["protocol_version"],
        "sample_sha256": __import__("hashlib").sha256(args.sample.read_bytes()).hexdigest(),
        "cells": sum(len(records) for records in results.values()),
        "timeout_seconds": args.timeout,
        "jobs": args.jobs,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "executed_source_sha": os.environ.get("EXECUTED_SOURCE_SHA", "local"),
        "wall_seconds": round(time.time() - started, 2),
        "execution_status": {
            status: sum(
                1 for records in results.values() for record in records
                if record.get("execution_status") == status
            )
            for status in ("COMPLETED", "TIMEOUT_WALL_CLOCK", "ERROR")
        },
        "peak_rss_mib": peak_rss_mb(),
    }
    (out_dir / "profile_run.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
