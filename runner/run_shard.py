#!/usr/bin/env python3
"""Execute every task of one shard with a per-cell subprocess timeout."""
from __future__ import annotations
import csv, json, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
shard = sys.argv[1]
tasks = list(csv.DictReader(open(ROOT / "protocol" / "tasks" / f"shard_{int(shard):03d}.csv")))
outdir = ROOT / "results" / "raw"; outdir.mkdir(parents=True, exist_ok=True)
out = outdir / f"shard_{int(shard):03d}.jsonl"
done = set()
if out.exists():
    for line in out.open():
        try: done.add(json.loads(line)["task_id"])
        except Exception: pass
fields = ["task_id", "experiment", "model", "seed", "replicate", "L", "H", "mode", "timeout_s"]
with out.open("a") as fh:
    for i, t in enumerate(tasks, 1):
        if t["task_id"] in done:
            continue
        spec = {"tag": t["task_id"], "experiment": t["experiment"], "model": t["model"],
                "seed": int(t["seed"]), "replicate": int(t["replicate"]),
                "L": int(t["L"]), "H": float(t["H"]), "mode": t["mode"],
                "timeout_s": int(t["timeout_s"]), "params": json.loads(t["params"])}
        t0 = time.time()
        try:
            p = subprocess.run([sys.executable, str(HERE / "run_cell.py"), json.dumps(spec)],
                               capture_output=True, text=True, timeout=int(t["timeout_s"]))
            rec = json.loads(p.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            rec = {"tag": t["task_id"], "mode": t["mode"], "verdict": None,
                   "execution_status": "TIMEOUT", "error": None, "model_hash": None,
                   "n_places": None, "n_transitions": None, "counters": None,
                   "runtime_s": None, "L": int(t["L"]), "H": float(t["H"]),
                   "params": json.loads(t["params"])}
        except Exception as exc:  # noqa: BLE001
            rec = {"tag": t["task_id"], "mode": t["mode"], "verdict": None,
                   "execution_status": "ERROR", "error": f"{type(exc).__name__}: {exc}",
                   "counters": None, "runtime_s": None}
        rec.update({"task_id": t["task_id"], "experiment": t["experiment"],
                    "model": t["model"], "replicate": int(t["replicate"]),
                    "wall_s": round(time.time() - t0, 3)})
        fh.write(json.dumps(rec, default=str) + "\n"); fh.flush()
        if i % 10 == 0:
            print(f"shard {shard}: {i}/{len(tasks)}", flush=True)
print("done", shard, len(tasks))
