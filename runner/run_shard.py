#!/usr/bin/env python3
"""Execute every task of one shard with a per-cell subprocess timeout."""
from __future__ import annotations
import csv, hashlib, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = Path(__import__("os").environ.get("TASKS_DIR", ROOT / "protocol" / "tasks"))
HERE = Path(__file__).resolve().parent
shard = sys.argv[1]
tasks = list(csv.DictReader(open(TASKS_DIR / f"shard_{int(shard):03d}.csv")))
outdir = ROOT / "results" / "raw"; outdir.mkdir(parents=True, exist_ok=True)
checkpoint_dir = ROOT / "results" / "checkpoints"; checkpoint_dir.mkdir(parents=True, exist_ok=True)
out = outdir / f"shard_{int(shard):03d}.jsonl"
done = set()
records = []
if out.exists():
    for line in out.open():
        try:
            record = json.loads(line)
            done.add(record["task_id"])
            records.append(record)
        except Exception: pass
fields = ["task_id", "experiment", "model", "seed", "replicate", "L", "H", "mode", "timeout_s"]
for i, t in enumerate(tasks, 1):
        if t["task_id"] in done:
            continue
        model_value = t.get("model") or t.get("model_id")
        spec = {"tag": t["task_id"], "experiment_id": t.get("experiment_id", t["experiment"]),
                "benchmark_family": t.get("benchmark_family"),
                "model_id": t.get("model_id", t["model"]),
                "bounded_problem_id": t.get("bounded_problem_id"),
                "configuration_id": t.get("configuration_id", t["task_id"]),
                "experiment": t["experiment"], "model": model_value,
                "seed": int(t["seed"]), "replicate": int(t["replicate"]),
                "L": int(t["L"]), "H": float(t["H"]), "mode": t["mode"],
                "timeout_s": int(t["timeout_s"]), "params": json.loads(t["params"])}
        t0 = time.time()
        checkpoint_path = checkpoint_dir / f"{t['task_id']}.json"
        try:
            env = dict(os.environ, CHECKPOINT_PATH=str(checkpoint_path))
            p = subprocess.run([sys.executable, str(HERE / "run_cell.py"), json.dumps(spec)],
                               capture_output=True, text=True, timeout=int(t["timeout_s"]), env=env)
            if p.returncode != 0 or not p.stdout.strip():
                raise RuntimeError(
                    f"run_cell exit={p.returncode}; stderr={p.stderr[-1000:]}"
                )
            rec = json.loads(p.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            partial = {}
            if checkpoint_path.is_file():
                try:
                    partial = json.loads(checkpoint_path.read_text())
                except Exception:
                    partial = {}
            rec = {**partial, "tag": t["task_id"], "mode": t["mode"], "verdict": None,
                   "execution_status": "TIMEOUT_WALL_CLOCK", "error": None,
                   "termination_status": "TIMEOUT_WALL_CLOCK",
                   "model_hash": partial.get("model_hash"),
                   "analysis_status": "NOT_RUN", "inconclusive_reason": None,
                   "partial_workload": bool(partial.get("counters")),
                   "failure_stage": partial.get("failure_stage", "MODEL_GENERATION"),
                   "counters": partial.get("counters"),
                   "n_places": partial.get("n_places"),
                   "n_transitions": partial.get("n_transitions"),
                   "n_arcs": partial.get("n_arcs"),
                   "runtime_s": None, "L": int(t["L"]), "H": float(t["H"]),
                   "timeout_s": int(t["timeout_s"]),
                   "params": json.loads(t["params"])}
        except Exception as exc:  # noqa: BLE001
            rec = {"tag": t["task_id"], "mode": t["mode"], "verdict": None,
                   "execution_status": "ERROR", "error": f"{type(exc).__name__}: {exc}",
                   "termination_status": "ERROR",
                   "analysis_status": "NOT_RUN", "inconclusive_reason": None,
                   "counters": None, "runtime_s": None,
                   "partial_workload": False, "failure_stage": "SHARD_RUNNER",
                   "L": int(t["L"]), "H": float(t["H"]),
                   "timeout_s": int(t["timeout_s"]),
                   "params": json.loads(t["params"])}
        rec.update({"task_id": t["task_id"], "experiment": t["experiment"],
                    "experiment_id": t.get("experiment_id", t["experiment"]),
                    "benchmark_family": t.get("benchmark_family"),
                    "model_id": t.get("model_id", t["model"]),
                    "bounded_problem_id": t.get("bounded_problem_id"),
                    "configuration_id": t.get("configuration_id", t["task_id"]),
                    "model": model_value, "seed": int(t["seed"]),
                    "replicate": int(t["replicate"]),
                    "replicate_id": int(t["replicate"]),
                    "commit_sha": os.environ.get("GITHUB_SHA"),
                    "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
                    "job_id": os.environ.get("GITHUB_JOB"),
                    "shard_id": int(shard),
                    "raw_artifact": f"raw/{out.name}",
                    "wall_s": round(time.time() - t0, 3)})
        rec["configuration_hash"] = hashlib.sha256(
            json.dumps(spec, sort_keys=True).encode()
        ).hexdigest()
        payload = json.dumps(rec, default=str, sort_keys=True)
        rec["raw_record_hash"] = hashlib.sha256(payload.encode()).hexdigest()
        records.append(rec)
        tmp_out = out.with_suffix(out.suffix + ".tmp")
        tmp_out.write_text("".join(
            json.dumps(record, default=str, sort_keys=True) + "\n"
            for record in records
        ))
        tmp_out.replace(out)
        if i % 10 == 0:
            print(f"shard {shard}: {i}/{len(tasks)}", flush=True)
print("done", shard, len(tasks))
