#!/usr/bin/env python3
"""Execute the frozen tasks of one v6 shard, with checkpointing and a ledger.

This module contains the *pure* execution logic used inside a Modal CPU
container.  It deliberately does not import ``modal`` so that it can be
exercised by the local simulation in ``scripts/run_v6_modal.py`` with a plain
directory standing in for the Modal Volume.

Layout of the volume subtree (``V6_VOLUME_ROOT``, default
``/vol/scalability_v6_formal``)::

    protocol/shards/shard_XXX.csv        frozen task rows (uploaded by the orchestrator)
    run_metadata/run_<run_id>.json       frozen run configuration (source SHA, image spec SHA, ...)
    run_metadata/attempts/shard_XXX_attempt_N.json   one marker per shard attempt
    checkpoints/shard_XXX/<task_id>.json last verifier checkpoint of the current attempt
    raw/shard_XXX/<task_id>.json         one final record per task (atomic write)
    completion_ledger/shard_XXX.jsonl    append-only ledger: task, sha256, status, attempt, invocation
    hashes/shard_XXX.jsonl               append-only file-hash manifest for raw records
    logs/shard_XXX.log                   human-readable shard progress log
    metrics/shard_XXX.json               final shard summary (written last, atomically)

Guarantees
---------
* A record is visible only after a complete atomic ``tmp + os.replace`` write;
  its SHA-256 is appended to the ledger only after the record is durable.
* The ledger is owned by exactly one worker per shard and appended with
  ``O_APPEND`` + ``fsync``; a task is skipped on restart only when its raw
  record exists *and* its SHA-256 matches the last ledger entry.
* A 60 s wall-clock kill (SIGKILL to the whole child process group) produces a
  ``TIMEOUT_WALL_CLOCK`` record built from the last atomic checkpoint.  Stages
  that never executed stay ``null``; they are never reported as zero work.
* A task-level technical failure is written as an ``ERROR`` record but is *not*
  final for resume purposes: the next shard attempt re-runs that task and
  appends a new ledger entry.  ``COMPLETED`` and ``TIMEOUT_WALL_CLOCK`` are
  final and are never repeated.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path

DEFAULT_VOLUME_ROOT = "/vol/scalability_v6_formal"
DEFAULT_CELL_TIMEOUT_S = 60.0
DEFAULT_SCG_CACHE_DIR = "/tmp/scg_cache"
REMOTE_REPO_ROOT = "/root/v6repo"

#: Statuses that are final for resume/dedupe purposes.  ``ERROR`` is a
#: technical failure of a single cell and may be retried by a later attempt.
FINAL_EXECUTION_STATUSES = frozenset({"COMPLETED", "TIMEOUT_WALL_CLOCK"})
SHARD_COMPLETE_STATUS = "COMPLETED"
SHARD_INCOMPLETE_STATUS = "INCOMPLETE"

FIELDS = [
    "task_id", "protocol_version", "experiment_id", "experiment",
    "benchmark_family", "stratum", "base_model_id", "model_id",
    "bounded_problem_id", "configuration_id", "seed", "replicate",
    "L", "H", "mode", "timeout_s", "expected_model_hash",
    "expected_structure_hash", "params",
]


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value) -> str:
    """The canonical serialisation used for every hash in this project."""
    return json.dumps(value, default=str, sort_keys=True)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, payload: bytes) -> str:
    """Write ``payload`` atomically; return the SHA-256 of the written bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with tmp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return sha256_bytes(payload)


def atomic_write_text(path: Path, text: str) -> str:
    return atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, value) -> str:
    return atomic_write_text(path, canonical_json(value) + "\n")


def append_jsonl(path: Path, entry) -> int:
    """Append one JSON line with O_APPEND + fsync; returns bytes appended."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json(entry) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(descriptor, line.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return len(line)


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def percentile(values, probability):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, int(-(-probability * len(ordered) // 1)) - 1)
    return ordered[index]


def repo_tree_sha256(root: Path, include_top=("src", "runner", "modal_app"),
                     extra_files=("requirements-v5.lock", "requirements-modal.lock")) -> str:
    """SHA-256 of the exact source snapshot baked into the Modal image.

    The allow-list mirrors ``_image_ignore`` in ``modal_app/v6_modal_runner.py`` so
    the hash changes whenever the executed source tree changes and never
    changes because of local ``work/``, ``results/`` or protocol artefacts.
    """
    root = Path(root)
    entries = []
    for top in include_top:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            parts = path.relative_to(root).parts
            if "__pycache__" in parts or path.suffix == ".pyc":
                continue
            entries.append((path.relative_to(root).as_posix(), sha256_file(path)))
    for name in extra_files:
        path = root / name
        if path.is_file():
            entries.append((name, sha256_file(path)))
    entries.sort(key=lambda item: item[0])
    payload = "".join(f"{name}\0{digest}\n" for name, digest in entries)
    return sha256_text(payload)


def image_spec_sha256(repo_root: Path, *, base="modal.Image.debian_slim(python_version='3.13')",
                      packages=("numpy==2.3.4", "scipy==1.16.2", "modal==1.6.3"),
                      remote_repo_root: str = REMOTE_REPO_ROOT) -> str:
    """Deterministic identifier for the execution image (the "image digest")."""
    material = {
        "base_image": base,
        "pip_packages": list(packages),
        "remote_repo_root": remote_repo_root,
        "repo_tree_sha256": repo_tree_sha256(Path(repo_root)),
        "schema": "v6-modal-image-spec-1",
    }
    return sha256_text(canonical_json(material))


def load_run_metadata(volume_root: Path, run_id: str) -> dict:
    path = Path(volume_root) / "run_metadata" / f"run_{run_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"run metadata missing on volume: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# container introspection
# --------------------------------------------------------------------------- #
def _module_version(name: str):
    try:
        module = __import__(name)
    except Exception:  # noqa: BLE001 - version reporting must never fail a shard
        return None
    return getattr(module, "__version__", None)


def executor_environment() -> dict:
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "numpy": _module_version("numpy"),
        "scipy": _module_version("scipy"),
        "modal": _module_version("modal"),
        "executor": "modal",
    }


def _process_rss_mb(pid: int):
    """Resident set size of ``pid`` in MiB from /proc (Linux containers)."""
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 3)
    except (OSError, ValueError):
        return None
    return None


def _run_monitored(command, env, timeout, cwd):
    """Run ``command`` with a hard wall-clock cap; SIGKILL the process group."""
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env, cwd=str(cwd), start_new_session=True,
    )
    deadline = started + timeout
    peak_rss = 0.0
    timed_out = False
    while True:
        if process.poll() is not None:
            break
        sample = _process_rss_mb(process.pid)
        if sample is not None:
            peak_rss = max(peak_rss, sample)
        if time.monotonic() >= deadline:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            break
        time.sleep(0.1)
    stdout, stderr = process.communicate()
    return {
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "peak_rss_mb": peak_rss or None,
        "timed_out": timed_out,
        "wall_s": round(time.monotonic() - started, 3),
    }


# --------------------------------------------------------------------------- #
# task -> verifier spec
# --------------------------------------------------------------------------- #
def task_spec(row: dict, override_timeout_s=None) -> dict:
    model_value = row.get("model_id") or row.get("model")
    spec = {
        "tag": row["task_id"],
        "protocol_version": row.get("protocol_version"),
        "experiment_id": row.get("experiment_id", row["experiment"]),
        "benchmark_family": row.get("benchmark_family"),
        "model_id": row.get("model_id") or model_value,
        "bounded_problem_id": row.get("bounded_problem_id"),
        "configuration_id": row.get("configuration_id", row["task_id"]),
        "experiment": row["experiment"],
        "model": model_value,
        "seed": int(row["seed"]),
        "replicate": int(row["replicate"]),
        "L": int(row["L"]),
        "H": float(row["H"]),
        "mode": row["mode"],
        "timeout_s": int(row["timeout_s"]),
        "params": json.loads(row["params"]),
        "expected_model_hash": row.get("expected_model_hash") or None,
        "expected_structure_hash": row.get("expected_structure_hash") or None,
    }
    return spec


def _cell_environment(spec, config, checkpoint_path: Path) -> dict:
    """Minimal, deterministic environment for one verifier subprocess."""
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "CONDA_PREFIX")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({
        "CHECKPOINT_PATH": str(checkpoint_path),
        "V6_PROFILE": "1",
        "SCG_CACHE_DIR": str(config.get("scg_cache_dir") or DEFAULT_SCG_CACHE_DIR),
        "EXECUTED_SOURCE_SHA": str(config.get("source_sha") or "unknown"),
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        # BLAS/OpenMP must stay single threaded: Modal ``cpu=1`` is a single
        # physical core and oversubscribing would corrupt timing comparisons.
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "PYTHONUNBUFFERED": "1",
    })
    return env


# --------------------------------------------------------------------------- #
# record construction
# --------------------------------------------------------------------------- #
def _base_record(row: dict, spec: dict) -> dict:
    model_value = row.get("model_id") or row.get("model")
    return {
        "tag": row["task_id"],
        "mode": row["mode"],
        "verdict": None,
        "execution_status": "ERROR",
        "error": None,
        "analysis_status": "NOT_RUN",
        "inconclusive_reason": None,
        "model_hash": None,
        "n_places": None,
        "n_transitions": None,
        "n_arcs": None,
        "structure_hash": None,
        "counters": None,
        "runtime_s": None,
        "timeout_s": int(row["timeout_s"]),
        "partial_workload": False,
        "failure_stage": "MODEL_GENERATION",
        "model_attributes": None,
        "validation_status": "NOT_RUN",
        "peak_rss_mb": None,
        "scg_cache_status": None,
        "scg_cache_key": None,
        "scg_cache_provenance": None,
        "runtime_scg_build_s": None,
        "runtime_scg_load_s": None,
        "L": int(row["L"]),
        "H": float(row["H"]),
        "params": spec["params"],
        "experiment": row["experiment"],
        "model": model_value,
        "seed": int(row["seed"]),
        "replicate": int(row["replicate"]),
    }


def timeout_record(row: dict, spec: dict, partial: dict, sampled_peak_mb) -> dict:
    """Build a ``TIMEOUT_WALL_CLOCK`` record from the last atomic checkpoint.

    Unexecuted counters stay at whatever the checkpoint contains (``null`` when
    the stage never ran).  Nothing is fabricated and no verdict is invented.
    """
    record = _base_record(row, spec)
    record.update(partial or {})
    peaks = [value for value in ((partial or {}).get("peak_rss_mb"), sampled_peak_mb)
             if value is not None]
    record.update({
        "tag": row["task_id"],
        "mode": row["mode"],
        "verdict": None,
        "execution_status": "TIMEOUT_WALL_CLOCK",
        "error": None,
        "termination_status": "TIMEOUT_WALL_CLOCK",
        "analysis_status": "NOT_RUN",
        "inconclusive_reason": None,
        "model_hash": (partial or {}).get("model_hash"),
        "partial_workload": bool((partial or {}).get("counters")),
        "failure_stage": (partial or {}).get("failure_stage", "MODEL_GENERATION"),
        "validation_status": (partial or {}).get("validation_status", "NOT_RUN"),
        "counters": (partial or {}).get("counters"),
        "n_places": (partial or {}).get("n_places"),
        "n_transitions": (partial or {}).get("n_transitions"),
        "n_arcs": (partial or {}).get("n_arcs"),
        "structure_hash": (partial or {}).get("structure_hash"),
        "model_attributes": (partial or {}).get("model_attributes"),
        "runtime_s": None,
        "peak_rss_mb": max(peaks) if peaks else None,
        "L": int(row["L"]),
        "H": float(row["H"]),
        "timeout_s": int(row["timeout_s"]),
        "params": spec["params"],
    })
    return record


def error_record(row: dict, spec: dict, error: str, sampled_peak_mb) -> dict:
    record = _base_record(row, spec)
    record.update({
        "tag": row["task_id"],
        "mode": row["mode"],
        "verdict": None,
        "execution_status": "ERROR",
        "error": error,
        "termination_status": "ERROR",
        "failure_stage": "SHARD_RUNNER",
        "peak_rss_mb": sampled_peak_mb,
    })
    return record


def completed_record(row: dict, spec: dict, payload: dict, sampled_peak_mb) -> dict:
    record = dict(payload)
    existing = record.get("peak_rss_mb")
    candidates = [value for value in (existing, sampled_peak_mb) if value is not None]
    record["peak_rss_mb"] = max(candidates) if candidates else None
    return record


def finalize_record(record: dict, row: dict, spec: dict, *, run_id: str, shard_num: int,
                    attempt: int, task_attempt: int, source_sha: str, invocation_id,
                    wall_s: float, wall_clock_cap_s: float, image_spec_sha: str,
                    resources: dict, environment: dict) -> dict:
    """Add every provenance / compatibility field required by the protocol."""
    model_value = row.get("model_id") or row.get("model")
    shard = f"{int(shard_num):03d}"
    record.update({
        "task_id": row["task_id"],
        "experiment": row["experiment"],
        "protocol_version": row.get("protocol_version"),
        "experiment_id": row.get("experiment_id", row["experiment"]),
        "benchmark_family": row.get("benchmark_family"),
        "model_id": row.get("model_id") or model_value,
        "bounded_problem_id": row.get("bounded_problem_id"),
        "configuration_id": row.get("configuration_id", row["task_id"]),
        "model": model_value,
        "seed": int(row["seed"]),
        "replicate": int(row["replicate"]),
        "replicate_id": int(row["replicate"]),
        "commit_sha": source_sha,
        "source_code_hash": source_sha,
        "workflow_run_id": run_id,
        "job_id": invocation_id,
        "logical_job_key": f"modal:shard_{shard}",
        "shard_execution_id": f"{run_id}:{shard}",
        "shard_id": int(shard_num),
        "raw_artifact": f"raw/shard_{shard}/{row['task_id']}.json",
        "wall_s": wall_s,
        "executor": "modal",
        "executor_environment": environment,
        "run_id": run_id,
        "attempt": int(attempt),
        "task_attempt": int(task_attempt),
        "invocation_id": invocation_id,
        "image_spec_sha256": image_spec_sha,
        "wall_clock_cap_s": wall_clock_cap_s,
        "resources": dict(resources),
    })
    # Compatibility aliases consumed by the v5/v6 analysis scripts.
    attributes = record.get("model_attributes") or {}
    counters = record.get("counters") or {}
    record.update({
        "number_of_places": record.get("n_places"),
        "number_of_transitions": record.get("n_transitions"),
        "number_of_arcs": record.get("n_arcs"),
        "number_of_parts": attributes.get("number_of_parts"),
        "operations_per_part": attributes.get("operations_per_part"),
        "resource_count": attributes.get("resource_count"),
        "WIP": attributes.get("WIP"),
        "verified_reachable_concurrency": attributes.get("verified_reachable_concurrency"),
        "cycle_presence": attributes.get("cycle_presence"),
        "cycle_semantics": attributes.get("cycle_semantics"),
        "fault_configuration": attributes.get("fault_configuration"),
        "observable_ratio": attributes.get("observable_ratio_target"),
        "runtime_sec": record.get("runtime_s"),
        "time_limit_sec": int(row["timeout_s"]),
        "scg_limit": 50000,
        "SCG_classes": counters.get("scg_classes"),
        "SCG_edges": counters.get("scg_edges"),
        "synchronizer_states": counters.get("sync_states"),
        "candidate_words": counters.get("candidate_words"),
        "retained_path_records": counters.get("trace_records"),
        "terminal_path_pairs": counters.get("terminal_pairs"),
        "LP_calls": counters.get("lp_calls"),
        "error_type": (record.get("error") or "").split(":", 1)[0] or None,
    })
    record["configuration_hash"] = sha256_text(json.dumps(spec, sort_keys=True))
    return record


def attach_record_hash(record: dict) -> dict:
    """Set ``raw_record_hash`` over every other field (canonical JSON)."""
    payload = {key: value for key, value in record.items() if key != "raw_record_hash"}
    record["raw_record_hash"] = sha256_text(canonical_json(payload))
    return record


def record_file_text(record: dict) -> str:
    return canonical_json(record) + "\n"


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
def read_ledger_state(ledger_path: Path, raw_dir: Path):
    """Return (entries, done, stale) for a shard ledger.

    ``done`` maps task_id -> ledger entry for tasks whose record file still
    exists and whose SHA-256 matches the last ledger entry.  Anything else is
    reported as ``stale`` and will be regenerated.
    """
    entries = read_jsonl(ledger_path)
    by_task: dict[str, list[dict]] = {}
    for entry in entries:
        task_id = entry.get("task_id")
        if task_id:
            by_task.setdefault(task_id, []).append(entry)
    done: dict[str, dict] = {}
    stale: list[dict] = []
    for task_id, task_entries in by_task.items():
        last = task_entries[-1]
        if last.get("status") not in FINAL_EXECUTION_STATUSES:
            stale.append(last)
            continue
        raw_path = Path(last.get("raw_path") or (Path(raw_dir) / f"{task_id}.json"))
        if not raw_path.is_absolute():
            raw_path = Path(raw_dir) / f"{task_id}.json"
        if not raw_path.is_file():
            stale.append(last)
            continue
        if sha256_file(raw_path) != last.get("sha256"):
            stale.append(last)
            continue
        done[task_id] = last
    return entries, done, stale


# --------------------------------------------------------------------------- #
# shard execution
# --------------------------------------------------------------------------- #
def execute_shard(shard_id: str, run_id: str, attempt: int, *, volume_root,
                  repo_root, config: dict | None = None, invocation_id: str | None = None,
                  log=None, python_executable: str | None = None,
                  commit=None) -> dict:
    """Execute one frozen shard; returns the shard metrics document."""
    config = dict(config or {})
    log = log or (lambda message: print(message, flush=True))
    volume_root = Path(volume_root)
    repo_root = Path(repo_root)
    python_executable = python_executable or sys.executable
    shard_number = int(str(shard_id))
    shard = f"{shard_number:03d}"

    protocol_dir = volume_root / "protocol"
    shard_csv = protocol_dir / "shards" / f"shard_{shard}.csv"
    if not shard_csv.is_file():
        raise FileNotFoundError(f"frozen shard CSV missing on volume: {shard_csv}")
    expected_shard_sha = (config.get("shard_sha256") or {}).get(shard)
    if expected_shard_sha:
        actual = sha256_file(shard_csv)
        if actual != expected_shard_sha:
            raise RuntimeError(
                f"shard_{shard}.csv SHA mismatch: expected {expected_shard_sha}, got {actual}"
            )

    source_sha = str(config.get("source_sha") or "unknown")
    image_spec_sha = str(config.get("image_spec_sha256") or "unknown")
    resources = dict(config.get("resources") or {"cpu": 1.0, "memory_mib": 4096})
    environment = executor_environment()
    environment["executor"] = "modal"
    cache_dir = Path(config.get("scg_cache_dir") or DEFAULT_SCG_CACHE_DIR)

    raw_dir = volume_root / "raw" / f"shard_{shard}"
    checkpoint_dir = volume_root / "checkpoints" / f"shard_{shard}"
    ledger_path = volume_root / "completion_ledger" / f"shard_{shard}.jsonl"
    hash_path = volume_root / "hashes" / f"shard_{shard}.jsonl"
    log_path = volume_root / "logs" / f"shard_{shard}.log"
    metrics_path = volume_root / "metrics" / f"shard_{shard}.json"
    attempt_dir = volume_root / "run_metadata" / "attempts"

    for directory in (raw_dir, checkpoint_dir, ledger_path.parent, hash_path.parent,
                      log_path.parent, metrics_path.parent, attempt_dir):
        directory.mkdir(parents=True, exist_ok=True)

    with shard_csv.open("r", encoding="utf-8", newline="") as handle:
        tasks = list(csv.DictReader(handle))

    started_at = utc_now()
    started_monotonic = time.monotonic()
    atomic_write_json(attempt_dir / f"shard_{shard}_attempt_{int(attempt)}.json", {
        "shard": shard, "run_id": run_id, "attempt": int(attempt),
        "invocation_id": invocation_id, "source_sha": source_sha,
        "image_spec_sha256": image_spec_sha, "started_at": started_at,
        "tasks_total": len(tasks), "task_csv_sha256": sha256_file(shard_csv),
    })

    ledger_entries, done, stale = read_ledger_state(ledger_path, raw_dir)
    if stale:
        log(f"shard {shard}: {len(stale)} stale ledger entr(ies) will be regenerated")
    prior_task_attempts: dict[str, int] = {}
    for entry in ledger_entries:
        task_id = entry.get("task_id")
        if task_id:
            prior_task_attempts[task_id] = prior_task_attempts.get(task_id, 0) + 1

    default_cap = float(config.get("cell_timeout_override_s") or DEFAULT_CELL_TIMEOUT_S)
    counts = {"executed": 0, "skipped": 0, "completed": 0, "timeout": 0,
              "inc": 0, "ver": 0, "amb": 0, "error": 0}
    task_walls: list[float] = []
    peak_rss_samples: list[float] = []
    cache_counts = {"hit": 0, "miss": 0, "disabled": 0, "unknown": 0}
    error_task_ids: list[str] = []
    timeout_task_ids: list[str] = []

    def append_log(message: str) -> None:
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{utc_now()} {message}\n")
        except OSError:
            pass

    append_log(
        f"run={run_id} attempt={attempt} invocation={invocation_id} "
        f"tasks={len(tasks)} resumed={len(done)} source_sha={source_sha}"
    )

    for index, row in enumerate(tasks, 1):
        task_id = row["task_id"]
        spec = task_spec(row)
        if task_id in done:
            counts["skipped"] += 1
            continue
        task_attempt = prior_task_attempts.get(task_id, 0) + 1
        checkpoint_path = checkpoint_dir / f"{task_id}.json"
        if checkpoint_path.exists():
            checkpoint_path.unlink()
        # The frozen per-task cap is authoritative (60 s); the simulation
        # uses a shorter override so the local self-test is cheap.
        wall_clock_cap = float(
            config.get("cell_timeout_override_s")
            or row.get("timeout_s")
            or DEFAULT_CELL_TIMEOUT_S
        )
        wall_started = time.monotonic()
        timed_out_record = False
        try:
            monitored = _run_monitored(
                [python_executable, str(repo_root / "runner" / "run_cell.py"),
                 json.dumps(spec)],
                _cell_environment(spec, config, checkpoint_path),
                wall_clock_cap,
                repo_root,
            )
            if monitored["timed_out"]:
                partial = {}
                if checkpoint_path.is_file():
                    try:
                        partial = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        partial = {}
                record = timeout_record(row, spec, partial, monitored["peak_rss_mb"])
                timed_out_record = True
            else:
                if monitored["returncode"] != 0 or not monitored["stdout"].strip():
                    raise RuntimeError(
                        f"run_cell exit={monitored['returncode']}; "
                        f"stderr_tail={monitored['stderr'][-800:]}"
                    )
                payload = json.loads(monitored["stdout"].strip().splitlines()[-1])
                if not isinstance(payload, dict):
                    raise RuntimeError("run_cell did not print a JSON object")
                record = completed_record(row, spec, payload, monitored["peak_rss_mb"])
                timed_out_record = False
            wall_s = monitored["wall_s"]
        except Exception as exc:  # noqa: BLE001 - a cell failure must not stop the shard
            record = error_record(row, spec, f"{type(exc).__name__}: {exc}",
                                  None)
            wall_s = round(time.monotonic() - wall_started, 3)
            timed_out_record = False

        status = record.get("execution_status")
        record = finalize_record(
            record, row, spec,
            run_id=run_id, shard_num=shard_number, attempt=int(attempt),
            task_attempt=task_attempt, source_sha=source_sha,
            invocation_id=invocation_id, wall_s=wall_s,
            wall_clock_cap_s=wall_clock_cap, image_spec_sha=image_spec_sha,
            resources=resources, environment=environment,
        )
        record = attach_record_hash(record)
        payload_text = record_file_text(record)
        payload_bytes = payload_text.encode("utf-8")
        payload_sha = sha256_bytes(payload_bytes)
        raw_path = raw_dir / f"{task_id}.json"
        atomic_write_bytes(raw_path, payload_bytes)
        # The ledger SHA-256 is computed from the exact bytes that ended up on
        # disk (never from a re-serialization of the parsed object).
        file_sha = sha256_file(raw_path)
        if file_sha != payload_sha:
            raise RuntimeError(
                f"raw record {raw_path} does not match the written payload bytes "
                f"({file_sha} != {payload_sha})"
            )
        append_jsonl(ledger_path, {
            "task_id": task_id,
            "sha256": file_sha,
            "bytes": len(payload_bytes),
            "raw_record_hash": record["raw_record_hash"],
            "status": status,
            "attempt": int(attempt),
            "task_attempt": task_attempt,
            "invocation_id": invocation_id,
            "run_id": run_id,
            "raw_path": f"raw/shard_{shard}/{task_id}.json",
            "wall_s": wall_s,
            "final": status in FINAL_EXECUTION_STATUSES,
            "recorded_at": utc_now(),
        })
        append_jsonl(hash_path, {
            "task_id": task_id,
            "path": f"raw/shard_{shard}/{task_id}.json",
            "sha256": file_sha,
            "bytes": len(payload_bytes),
            "attempt": int(attempt),
            "task_attempt": task_attempt,
            "invocation_id": invocation_id,
            "run_id": run_id,
            "recorded_at": utc_now(),
        })
        if timed_out_record:
            # The checkpoint that produced the timeout record stays on the
            # volume as evidence and as the null-semantics source.
            append_log(f"{task_id}: timeout at stage {record.get('failure_stage')}")
        counts["executed"] += 1
        task_walls.append(wall_s)
        if record.get("peak_rss_mb") is not None:
            peak_rss_samples.append(float(record["peak_rss_mb"]))
        cache_status = record.get("scg_cache_status")
        if cache_status in cache_counts:
            cache_counts[cache_status] += 1
        if status == "COMPLETED":
            counts["completed"] += 1
            verdict = record.get("verdict")
            if verdict == "VER":
                counts["ver"] += 1
            elif verdict == "AMB":
                counts["amb"] += 1
            elif verdict == "INC":
                counts["inc"] += 1
        elif status == "TIMEOUT_WALL_CLOCK":
            counts["timeout"] += 1
            timeout_task_ids.append(task_id)
        elif status == "ERROR":
            counts["error"] += 1
            error_task_ids.append(task_id)
        append_log(
            f"{index}/{len(tasks)} {task_id} status={status} wall={wall_s:.3f}s "
            f"peak_rss={record.get('peak_rss_mb')} cache={record.get('scg_cache_status')}"
        )
        if commit is not None:
            try:
                commit()
            except Exception as exc:  # noqa: BLE001 - commit is best effort per task
                append_log(f"volume commit after {task_id} failed: {type(exc).__name__}: {exc}")

    finished_at = utc_now()
    wall_total = round(time.monotonic() - started_monotonic, 3)
    # Shard totals are the *final* state of the shard, not just what this
    # invocation executed: a resumed shard must report the same totals as an
    # uninterrupted one.  Recomputed from the durable raw records.
    final_ledger_entries, final_done, _ = read_ledger_state(ledger_path, raw_dir)
    final_status: Counter = Counter()
    final_verdicts: Counter = Counter()
    final_errors: list[str] = []
    unfinished: list[str] = []
    for row in tasks:
        task_id = row["task_id"]
        path = raw_dir / f"{task_id}.json"
        if not path.is_file():
            unfinished.append(task_id)
            continue
        try:
            finished = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            unfinished.append(task_id)
            continue
        task_status = finished.get("execution_status")
        final_status[task_status] += 1
        if task_status == "COMPLETED":
            final_verdicts[str(finished.get("verdict"))] += 1
        elif task_status == "ERROR":
            final_errors.append(task_id)
    error_task_ids = final_errors
    timeout_task_ids = sorted(
        entry["task_id"] for entry in (final_done.values())
        if entry.get("status") == "TIMEOUT_WALL_CLOCK"
    )
    status = (SHARD_COMPLETE_STATUS if not final_errors and not unfinished
              else SHARD_INCOMPLETE_STATUS)
    metrics = {
        "schema": "v6-modal-shard-metrics-1",
        "shard": shard,
        "shard_id": shard_number,
        "run_id": run_id,
        "attempt": int(attempt),
        "status": status,
        "invocation_id": invocation_id,
        "source_sha": source_sha,
        "image_spec_sha256": image_spec_sha,
        "tasks_total": len(tasks),
        "tasks_executed": counts["executed"],
        "tasks_skipped_resumed": counts["skipped"],
        "tasks_completed": final_status["COMPLETED"],
        "tasks_timeout": final_status["TIMEOUT_WALL_CLOCK"],
        "tasks_error": final_status["ERROR"],
        "tasks_inc": final_verdicts["INC"],
        "tasks_ver": final_verdicts["VER"],
        "tasks_amb": final_verdicts["AMB"],
        "tasks_executed_this_attempt": counts["executed"],
        "tasks_unfinished": len(unfinished),
        "unfinished_task_ids": unfinished,
        "error_task_ids": error_task_ids,
        "timeout_task_ids": timeout_task_ids,
        "wall_s": wall_total,
        "task_wall_s_sum": round(sum(task_walls), 3),
        "peak_rss_mb_max": max(peak_rss_samples) if peak_rss_samples else None,
        "peak_rss_mb_p95": percentile(peak_rss_samples, 0.95),
        "scg_cache": cache_counts,
        "cell_timeout_s": default_cap,
        "cpu": resources.get("cpu"),
        "memory_mib": resources.get("memory_mib"),
        "scg_cache_dir": str(cache_dir),
        "environment": environment,
        "started_at": started_at,
        "finished_at": finished_at,
        "ledger_entries": len(final_ledger_entries),
    }
    atomic_write_json(metrics_path, metrics)
    if commit is not None:
        try:
            commit()
        except Exception as exc:  # noqa: BLE001
            append_log(f"final volume commit failed: {type(exc).__name__}: {exc}")
    append_log(f"finished status={status} wall={wall_total}s")
    return metrics


# --------------------------------------------------------------------------- #
# local debugging entry point (also used by the simulation)
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume-root", type=Path, default=Path(DEFAULT_VOLUME_ROOT))
    parser.add_argument("--repo-root", type=Path, default=Path(REMOTE_REPO_ROOT))
    parser.add_argument("--shard", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", type=int, default=0)
    parser.add_argument("--cell-timeout", type=float, default=None)
    parser.add_argument("--scg-cache-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    config = {}
    if args.cell_timeout is not None:
        config["cell_timeout_override_s"] = args.cell_timeout
    if args.scg_cache_dir is not None:
        config["scg_cache_dir"] = str(args.scg_cache_dir)
    metrics = execute_shard(
        args.shard, args.run_id, args.attempt,
        volume_root=args.volume_root, repo_root=args.repo_root,
        config=config, invocation_id="local-debug",
    )
    print(canonical_json(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
