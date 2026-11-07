#!/usr/bin/env python3
"""Recompute and verdict-drift audit of the 555 historical conflict-tier bounded
problems (3,645 archived Proposed evaluations) under the v6 (strong-time
corrected) semantics.

This is the execution step that follows ``scripts/audit_v6_historical_impact.py``.
It is deliberately separated from the formal 8,280-cell v6 experiment: the
historical 36,450-evaluation baseline is an add-on dataset that is never mixed
with the formal matrix budget, protocol, or artifacts.

Pipeline (all local, no Modal):

    manifest   freeze protocol/v6_historical_recompute_tasks.json (3,645 tasks)
    run        wall-clock-capped parallel recomputation, JSONL + resume
    audit      per-row drift table, transition matrix, SCG fingerprints,
               legacy-semantics replay sample and MSCG-LP reference plan

Commands
--------
    .venv/bin/python scripts/recompute_v6_historical_impact.py manifest
    .venv/bin/python scripts/recompute_v6_historical_impact.py run --phase first
    .venv/bin/python scripts/recompute_v6_historical_impact.py run --phase complete
    .venv/bin/python scripts/recompute_v6_historical_impact.py run --phase legacy_complete
    .venv/bin/python scripts/recompute_v6_historical_impact.py audit
    .venv/bin/python scripts/recompute_v6_historical_impact.py all

Interpretation notes
--------------------
* ``first`` pass: ``complete_set=False`` (FIRST_CERTIFICATE), the archived
  Proposed protocol.  This produces the verdict-drift denominator.
* ``complete`` pass: ``complete_set=True`` on the archived-AMB rows only, needed
  for the per-row critical-pair count and the certificate overlap endpoints.
  VER rows enumerate completely already in the first pass, so they are not
  repeated.
* ``legacy_complete`` pass: the pre-repair ``_compute_successor`` sourced from
  git history is monkeypatched back into ``src.scg.builder`` to reconstruct the
  archived-semantics SCG; complete enumeration then yields the archived
  critical-pair count / overlap endpoints for a deterministic sample.
* Every task runs in its own forked child of a pool worker: a true 60 s wall
  cap enforced with SIGKILL, per-task peak RSS from the fresh child, and the
  last completed verifier stage captured through a stage pipe before the kill.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import multiprocessing as mp
import os
import re
import resource
import select
import signal
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner.run_cell import model_hash as v6_model_hash  # noqa: E402
from runner.run_cell import structure_hash as v6_structure_hash  # noqa: E402
from src.benchmarks.rmt_fms_generator import generate_rmt_fms  # noqa: E402
from src.scg import builder as scg_builder  # noqa: E402
from src.verifier import verify  # noqa: E402

# ---------------------------------------------------------------------------
# Frozen paths and constants
# ---------------------------------------------------------------------------
HIST_DIR = ROOT / "work" / "v6_historical_impact"
WORK_DIR = ROOT / "work" / "v6_historical_recompute"
RAW_DIR = WORK_DIR / "raw"
CKPT_DIR = WORK_DIR / "checkpoints"
SUM_DIR = WORK_DIR / "summary"
LOG_DIR = WORK_DIR / "logs"
PROTOCOL_MANIFEST = ROOT / "protocol" / "v6_historical_recompute_tasks.json"

RECOVERED_CSV = HIST_DIR / "historical_rows_recovered.csv"
SOURCE_MANIFEST_CSV = HIST_DIR / "source_artifacts_manifest.csv"
SCG_IMPACT_JSON = HIST_DIR / "scg_core_impact.json"

SOURCE_ROOT = Path(
    "/Users/lucas/Documents/Codex/2026-11-05/"
    "referenced-chatgpt-conversation-this-is-an/work/tii-scalability-v4"
)
PROPOSED_ARTIFACTS = SOURCE_ROOT / "work" / "v5_baseline_proposed_artifacts"
REFERENCE_CSV_NAME = "fullscale_results_mscg_lp_complete.csv"

SCHEMA_VERSION = "v6-historical-recompute-tasks-v1"
DRIFT_SCHEMA_VERSION = "v6-historical-drift-v1"
TIER_CONFLICT = "A_static_urgency_conflict_detected"

EXPECTED_TOTAL_ROWS = 36_450
EXPECTED_TIER_ROWS = 3_645
EXPECTED_TIER_PROBLEMS = 555

WALL_CAP_S = 60.0
DEFAULT_WORKERS = 8

# Pre-repair commit: parent of the first v6 strong-time repair commit.
PRE_REPAIR_SPEC = "f9bac23^"
V6_REPAIR_COMMIT = "f9bac23"

SCG_FP_SAMPLE_SIZE = 120
LEGACY_REPLAY_SAMPLE_SIZE = 120

# Archived reference regeneration planning figures (Modal public rates).
MODAL_CPU_USD_PER_CORE_HOUR = 0.0473
MODAL_MEMORY_USD_PER_GIB_HOUR = 0.008
V6_FORMAL_MATRIX_CELLS = 8_280

RECORDED_STATUSES = {
    "COMPLETED",
    "TIMEOUT_WALL_CLOCK",
    "TIMEOUT_HARD_KILL",
    "MODEL_IDENTITY_MISMATCH",
}

PHASE_FILES = {
    "first": "recompute_first.jsonl",
    "complete": "recompute_complete.jsonl",
    "legacy_complete": "recompute_legacy_complete.jsonl",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def log(message: str, phase: str = "main") -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, file=sys.stderr, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / f"{phase}.log").open("a") as fh:
        fh.write(line + "\n")
        fh.flush()


def ensure_dirs() -> None:
    for path in (WORK_DIR, RAW_DIR, CKPT_DIR, SUM_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(value / divisor, 3)


def percentile(values, q: float):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(q * len(ordered)))
    return ordered[index]


# ---------------------------------------------------------------------------
# Frozen manifest: recover the conflict-tier task set
# ---------------------------------------------------------------------------
def parse_model_id(model_id: str) -> dict:
    body = model_id.split("_", 1)[1]
    pattern = re.compile(r"(ur|fl|wf|ar|m|j|k|w|s)(\d+)$")
    parsed: dict[str, int] = {}
    for token in body.split("_"):
        match = pattern.match(token)
        if not match:
            raise ValueError(f"cannot parse model_id token {token!r}")
        key, value = match.group(1), int(match.group(2))
        if key in parsed:
            raise ValueError(f"duplicate token {key!r} in {model_id!r}")
        parsed[key] = value
    expected = {"m", "j", "k", "w", "ur", "fl", "wf", "ar", "s"}
    if set(parsed) != expected:
        raise ValueError(f"unexpected token set for {model_id!r}: {parsed}")
    return parsed


def model_params_from_id(model_id: str) -> dict:
    parsed = parse_model_id(model_id)
    return {
        "n_machines": parsed["m"],
        "n_parts": parsed["j"],
        "ops_per_part": parsed["k"],
        "wip_bound": parsed["w"],
        "unobservable_ratio": parsed["ur"] / 100.0,
        "fault_locations": parsed["fl"],
        "interval_width_factor": parsed["wf"] / 10.0,
        "adjustable_ratio": parsed["ar"] / 100.0,
        "seed": parsed["s"],
        "n_configs": 2,
        "inspection_locations": 0,
    }


def discover_reference_csv() -> Path:
    candidates = sorted(
        path
        for path in (SOURCE_ROOT / "work").rglob(REFERENCE_CSV_NAME)
        if path.is_file()
    )
    if not candidates:
        raise FileNotFoundError(f"archived reference CSV {REFERENCE_CSV_NAME!r} not found")
    preferred = [path for path in candidates if "v5_final_build_rebuilt" in str(path)]
    return (preferred or candidates)[0]


def load_source_shards() -> dict[str, dict]:
    if not SOURCE_MANIFEST_CSV.is_file():
        raise FileNotFoundError(SOURCE_MANIFEST_CSV)
    by_shard: dict[str, dict] = {}
    with SOURCE_MANIFEST_CSV.open(newline="") as fh:
        for row in csv.DictReader(fh):
            by_shard[str(int(row["shard_id"]))] = row
    return by_shard


def load_reference_records(path: Path) -> dict[str, dict]:
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) != EXPECTED_TOTAL_ROWS:
        raise RuntimeError(f"reference CSV rows {len(rows)} != {EXPECTED_TOTAL_ROWS}")
    return {row["model_id"]: row for row in rows}


def task_id_for(model_id: str, L: int, H: float, mode: str) -> str:
    payload = f"{SCHEMA_VERSION}|{model_id}|L={L}|H={H!r}|mode={mode}"
    return sha256_text(payload)


def build_frozen_manifest(force: bool = False) -> dict:
    ensure_dirs()
    if PROTOCOL_MANIFEST.exists() and not force:
        raise RuntimeError(
            f"{PROTOCOL_MANIFEST} already exists; refusing to overwrite a frozen "
            "manifest (pass --force only before any recomputation)"
        )
    started = time.perf_counter()
    with RECOVERED_CSV.open(newline="") as fh:
        recovered = list(csv.DictReader(fh))
    if len(recovered) != EXPECTED_TOTAL_ROWS:
        raise RuntimeError(f"recovered rows {len(recovered)} != {EXPECTED_TOTAL_ROWS}")
    tier_rows = [row for row in recovered if row["scg_tier"] == TIER_CONFLICT]
    if len(tier_rows) != EXPECTED_TIER_ROWS:
        raise RuntimeError(f"tier rows {len(tier_rows)} != {EXPECTED_TIER_ROWS}")
    problems = {(row["historical_model_hash"], row["L"], row["H"]) for row in tier_rows}
    if len(problems) != EXPECTED_TIER_PROBLEMS:
        raise RuntimeError(f"bounded problems {len(problems)} != {EXPECTED_TIER_PROBLEMS}")

    reference_path = discover_reference_csv()
    reference = load_reference_records(reference_path)
    shards = load_source_shards()

    tasks = []
    errors = []
    for row in tier_rows:
        model_id = row["model_id"]
        try:
            params = model_params_from_id(model_id)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        observed = {
            "n_machines": int(row["n_machines"]),
            "n_parts": int(row["n_parts"]),
            "ops_per_part": int(row["ops_per_part"]),
            "wip_bound": int(row["wip_bound"]),
            "unobservable_ratio": float(row["unobservable_ratio"]),
            "fault_locations": int(row["fault_locations"]),
            "interval_width_factor": float(row["interval_width_factor"]),
            "adjustable_ratio": float(row["adjustable_ratio"]),
            "seed": int(row["seed"]),
        }
        for key, value in observed.items():
            if abs(float(params[key]) - float(value)) > 1e-12:
                errors.append(f"{model_id}: {key} id/column mismatch")
        if model_id not in reference:
            errors.append(f"{model_id}: missing archived reference record")
            continue
        ref = reference[model_id]
        if (ref["model_hash"], ref["L"], float(ref["H"])) != (
            row["historical_model_hash"],
            row["L"],
            float(row["H"]),
        ):
            errors.append(f"{model_id}: reference bounded identity mismatch")
        if row["proposed_verdict"] not in {"VER", "AMB"}:
            errors.append(f"{model_id}: unexpected archived verdict")
        if ref["verdict"] != row["proposed_verdict"]:
            errors.append(f"{model_id}: proposed/reference verdict mismatch")
        shard = shards.get(str(int(row["source_shard_id"])))
        if shard is None:
            errors.append(f"{model_id}: source shard manifest missing")
            continue
        L = int(row["L"])
        H = float(row["H"])
        mode = "first"
        tasks.append(
            {
                "task_id": task_id_for(model_id, L, H, mode),
                "evaluation_id": model_id,
                "bounded_problem_id": f"{row['historical_model_hash']}:L{L}:H{H}",
                "tier": row["scg_tier"],
                "historical_model_hash": row["historical_model_hash"],
                "v6_model_hash": row["v6_model_hash"],
                "v6_structure_hash": row["v6_structure_hash"],
                "n_places": int(row["n_places"]),
                "n_transitions": int(row["n_transitions"]),
                "L": L,
                "H": H,
                "mode": mode,
                "complete_set": False,
                "model_params": params,
                "fault_config": {
                    "fault_locations": params["fault_locations"],
                    "interval_width_factor": params["interval_width_factor"],
                    "unobservable_ratio": params["unobservable_ratio"],
                    "adjustable_ratio": params["adjustable_ratio"],
                },
                "archived": {
                    "verdict": row["proposed_verdict"],
                    "execution_status": row["proposed_execution_status"],
                    "proposed_runtime_total_median_s": float(
                        row["proposed_runtime_total_median_s"]
                    ),
                    "n_repeats": int(row["proposed_n_repeats"]),
                    "critical_pair_count": int(ref["terminal_pairs"]),
                    "critical_pair_count_source": (
                        "archived MSCG-LP reference record .terminal_pairs "
                        "(complete enumeration, v4 semantics); the archived "
                        "Proposed shard CSV stores no certificate counters"
                    ),
                    "reference_verdict": ref["verdict"],
                    "reference_runtime_total_median_s": float(
                        ref["runtime_total_median"]
                    ),
                    "reference_candidate_words": int(ref["candidate_words"]),
                    "reference_scg_classes": int(ref["scg_classes"]),
                    "reference_scg_edges": int(ref["scg_edges"]),
                    "reference_peak_memory_mb": float(ref["peak_memory_mb"]),
                    "reference_enumeration_complete": ref["enumeration_complete"]
                    == "True",
                },
                "source": {
                    "workflow_run_id": row["source_workflow_run_id"],
                    "shard_id": int(row["source_shard_id"]),
                    "artifact_name": row["source_artifact"],
                    "shard_sha256": shard["sha256"],
                },
            }
        )
    if errors:
        raise RuntimeError("manifest recovery failed:\n" + "\n".join(errors))

    tasks.sort(key=lambda task: task["task_id"])
    if len({task["task_id"] for task in tasks}) != EXPECTED_TIER_ROWS:
        raise RuntimeError("task_id collision")
    verdict_counts = Counter(task["archived"]["verdict"] for task in tasks)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "description": (
            "Frozen recompute task list for the evidence-driven minimum set of "
            "historical Proposed evaluations exposed to the v6 shared SCG "
            "strong-time repair (static urgency conflict tier)."
        ),
        "frozen": True,
        "selection_rule": (
            "work/v6_historical_impact/historical_rows_recovered.csv rows with "
            "scg_tier == 'A_static_urgency_conflict_detected' (all m in "
            "{2,3,4} x j2/k2/w2 configurations); no outcome filtering; task_id = "
            "sha256(schema|model_id|L|H|mode)."
        ),
        "tier_definition": (
            "Tier A: the untimed marking graph has a reachable pair of "
            "simultaneously enabled transitions (t,u) with t.earliest > u.latest "
            "(finite). This is a necessary-condition exposure screen, not a "
            "certificate that the archived verdict changes."
        ),
        "scope_separation": {
            "formal_v6_matrix_cells": V6_FORMAL_MATRIX_CELLS,
            "historical_rows_this_manifest": EXPECTED_TIER_ROWS,
            "conservative_upper_bound_rows": EXPECTED_TOTAL_ROWS,
            "statement": (
                "This manifest is an add-on historical recompute; it is disjoint "
                "from the formal 8,280-cell v6 matrix, does not consume its "
                "budget, and must never be pooled with its results."
            ),
        },
        "recompute_protocol": {
            "verifier": "src.verifier.verify",
            "mode": "first",
            "complete_set": False,
            "wall_cap_seconds_per_task": WALL_CAP_S,
            "parallel_workers_max": DEFAULT_WORKERS,
            "interpreter": ".venv/bin/python",
            "run_location": "local (no Modal access)",
            "resume": "by task_id from raw/recompute_<phase>.jsonl",
            "mode_assumption": (
                "The archived Proposed shards carry no mode column. "
                "FIRST_CERTIFICATE is the documented archived default and is "
                "supported by the archived proposed/reference runtime ratio "
                "(median 0.108 for tier A): the Proposed pass stops at the first "
                "certificate while the archived reference enumerates completely."
            ),
        },
        "source_inputs": {
            "recovered_csv": {
                "path": str(RECOVERED_CSV.relative_to(ROOT)),
                "sha256": sha256_file(RECOVERED_CSV),
            },
            "source_artifacts_manifest_csv": {
                "path": str(SOURCE_MANIFEST_CSV.relative_to(ROOT)),
                "sha256": sha256_file(SOURCE_MANIFEST_CSV),
            },
            "archived_reference_csv": {
                "path": str(reference_path),
                "sha256": sha256_file(reference_path),
            },
            "source_artifacts_root": str(PROPOSED_ARTIFACTS),
        },
        "counts": {
            "tasks": len(tasks),
            "bounded_problems": len(problems),
            "distinct_historical_model_hashes": len(
                {task["historical_model_hash"] for task in tasks}
            ),
            "distinct_v6_model_hashes": len({task["v6_model_hash"] for task in tasks}),
            "archived_verdicts": dict(sorted(verdict_counts.items())),
            "source_shards": len({task["source"]["shard_id"] for task in tasks}),
            "L_values": sorted({task["L"] for task in tasks}),
            "H_values": sorted({task["H"] for task in tasks}),
        },
        "tasks": tasks,
    }
    PROTOCOL_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, indent=1, sort_keys=False) + "\n"
    PROTOCOL_MANIFEST.write_text(payload)
    digest = sha256_file(PROTOCOL_MANIFEST)
    (SUM_DIR / "frozen_manifest.sha256").write_text(
        f"{digest}  {PROTOCOL_MANIFEST.relative_to(ROOT)}\n"
    )
    build_info = {
        "sha256": digest,
        "bytes": PROTOCOL_MANIFEST.stat().st_size,
        "tasks": len(tasks),
        "built_at_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_s": round(time.perf_counter() - started, 3),
        "python": sys.version.split()[0],
        "host": os.uname().nodename,
    }
    (SUM_DIR / "frozen_manifest_build.json").write_text(
        json.dumps(build_info, indent=2) + "\n"
    )
    log(
        f"manifest frozen: {len(tasks)} tasks / {len(problems)} bounded problems, "
        f"sha256={digest}",
        phase="manifest",
    )
    return manifest


def load_manifest_and_verify_freeze() -> dict:
    if not PROTOCOL_MANIFEST.is_file():
        raise FileNotFoundError(
            f"{PROTOCOL_MANIFEST} missing; run the manifest command first"
        )
    digest = sha256_file(PROTOCOL_MANIFEST)
    record_path = SUM_DIR / "frozen_manifest.sha256"
    if record_path.is_file():
        recorded = record_path.read_text().split()[0]
        if recorded != digest:
            raise RuntimeError(
                "frozen manifest sha256 changed: "
                f"recorded={recorded} current={digest}; refusing to recompute"
            )
    else:
        record_path.write_text(
            f"{digest}  {PROTOCOL_MANIFEST.relative_to(ROOT)}\n"
        )
    manifest = json.loads(PROTOCOL_MANIFEST.read_text())
    if len(manifest["tasks"]) != EXPECTED_TIER_ROWS:
        raise RuntimeError("frozen manifest task count changed")
    return manifest


def legacy_sample_tasks(tasks: list[dict]) -> list[dict]:
    """Deterministic archived-semantics replay sample (archived-AMB rows)."""
    eligible = [task for task in tasks if task["archived"]["verdict"] == "AMB"]
    eligible.sort(
        key=lambda task: sha256_text(f"legacy-replay-v1|{task['task_id']}")
    )
    return eligible[:LEGACY_REPLAY_SAMPLE_SIZE]


# ---------------------------------------------------------------------------
# Legacy (pre-repair) SCG successor for archived-semantics replay
# ---------------------------------------------------------------------------
_LEGACY_CACHE: dict = {}


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def legacy_successor():
    """Return the pre-repair ``_compute_successor`` (cached per process)."""
    if _LEGACY_CACHE.get("fn") is not None:
        return _LEGACY_CACHE["fn"]
    info = {"method": None, "source": "src/scg/builder.py"}
    pre_repair = _git("rev-parse", PRE_REPAIR_SPEC).strip()
    text = _git("show", f"{pre_repair}:src/scg/builder.py")
    start = text.index("def _compute_successor")
    end = text.index("\ndef ", start + 1)
    legacy_source = text[start:end]
    namespace = dict(scg_builder.__dict__)
    exec(compile(legacy_source, "<legacy_scg_builder>", "exec"), namespace)
    info.update(
        {
            "method": "git_history",
            "pre_repair_commit": pre_repair,
            "legacy_function_sha256": sha256_text(legacy_source),
            "legacy_function_lines": len(legacy_source.splitlines()),
        }
    )
    legacy_fn = namespace["_compute_successor"]

    def legacy_with_cache_compat(tpn, cls, t_name, cache=None):  # noqa: ANN001
        # The pre-repair successor has no successor-cache argument; the cache is
        # a v6 performance optimisation, so bypassing it preserves semantics.
        return legacy_fn(tpn, cls, t_name)

    legacy_with_cache_compat.__name__ = "_compute_successor"
    _LEGACY_CACHE["fn"] = legacy_with_cache_compat
    _LEGACY_CACHE["raw_fn"] = legacy_fn
    _LEGACY_CACHE["info"] = info
    return _LEGACY_CACHE["fn"]


def legacy_successor_info() -> dict:
    legacy_successor()
    return dict(_LEGACY_CACHE["info"])


def build_scg_with(successor_fn, tpn):
    original = scg_builder._compute_successor
    scg_builder._compute_successor = successor_fn
    try:
        return scg_builder.build_scg(tpn)
    finally:
        scg_builder._compute_successor = original


def dbm_signature(dbm) -> str:
    parts = []
    for left in sorted(dbm.variables):
        for right in sorted(dbm.variables):
            value = dbm.get(left, right)
            parts.append("INF" if isinstance(value, float) and math.isinf(value) else str(value))
    return sha256_text("|".join(parts))


def scg_fingerprint(scg) -> dict:
    classes_full = []
    classes_structure = []
    for class_id in sorted(scg.classes):
        cls = scg.classes[class_id]
        marking = tuple(sorted(cls.marking_key))
        enabled = tuple(sorted(cls.enabled))
        classes_structure.append(marking + ("|",) + enabled)
        classes_full.append(marking + ("|",) + enabled + ("|", dbm_signature(cls.dbm)))
    edges = sorted(
        (
            tuple(sorted(scg.classes[source].marking_key)),
            transition,
            tuple(sorted(scg.classes[target].marking_key)),
        )
        for source, transition, target in scg.edges
    )
    return {
        "n_classes": len(scg.classes),
        "n_edges": len(scg.edges),
        "classes_structure_digest": sha256_text(repr(classes_structure)),
        "classes_full_digest": sha256_text(repr(classes_full)),
        "edges_digest": sha256_text(repr(edges)),
    }


# ---------------------------------------------------------------------------
# Per-task execution (runs inside a forked child of a pool worker)
# ---------------------------------------------------------------------------
def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _execute_task_in_child(task: dict, phase: str, wall_cap_s: float) -> dict:
    """Fork a child per task; enforce a hard wall cap with SIGKILL."""
    if phase == "legacy_complete":
        legacy_successor()  # load once in the worker before forking
    data_r, data_w = os.pipe()
    stage_r, stage_w = os.pipe()
    started = time.perf_counter()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child branch
        try:
            os.close(data_r)
            os.close(stage_r)
            holder = {"fd": stage_w}
            record = _task_payload_with_stage(task, phase, stage_w)
            _write_all(data_w, json.dumps(record, default=str).encode())
            os.close(data_w)
            os.close(stage_w)
        except BaseException:  # noqa: BLE001
            try:
                os._exit(3)
            except BaseException:
                pass
        os._exit(0)

    os.close(data_w)
    os.close(stage_w)
    data_buf = bytearray()
    stage_buf = bytearray()
    data_closed = stage_closed = False
    deadline = started + wall_cap_s
    while time.monotonic() < deadline and not (data_closed and stage_closed):
        timeout = max(0.0, min(0.25, deadline - time.monotonic()))
        try:
            readable, _, _ = select.select([data_r, stage_r], [], [], timeout)
        except InterruptedError:
            continue
        for fd in readable:
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError:
                chunk = b""
            if fd == data_r:
                if chunk:
                    data_buf += chunk
                else:
                    data_closed = True
            else:
                if chunk:
                    stage_buf += chunk
                else:
                    stage_closed = True
    timed_out = not data_closed
    if timed_out:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    # Drain both pipes after the child is gone.
    for fd in (data_r, stage_r):
        while True:
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError:
                break
            if not chunk:
                break
            if fd == data_r:
                data_buf += chunk
            else:
                stage_buf += chunk
    for fd in (data_r, stage_r):
        try:
            os.close(fd)
        except OSError:
            pass
    last_stage = None
    last_counters = {}
    for line in stage_buf.splitlines():
        try:
            parsed = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        last_stage = parsed.get("stage") or last_stage
        last_counters = parsed.get("counters") or last_counters
    if timed_out or not data_buf:
        status = "TIMEOUT_WALL_CLOCK" if timed_out and not data_buf else "ERROR"
        if not timed_out and not data_buf:
            status = "ERROR"
        return {
            "task_id": task["task_id"],
            "phase": phase,
            "evaluation_id": task["evaluation_id"],
            "historical_model_hash": task["historical_model_hash"],
            "v6_model_hash": task["v6_model_hash"],
            "L": task["L"],
            "H": task["H"],
            "mode": task["mode"],
            "complete_set": phase == "complete",
            "semantics": "legacy" if phase == "legacy_complete" else "v6",
            "execution_status": status,
            "verdict": None,
            "inconclusive_reason": None,
            "enumeration_complete": None,
            "certificates_count": None,
            "overlap_min": None,
            "overlap_max": None,
            "overlap_width": None,
            "overlap_inside_window": None,
            "counters": last_counters,
            "stage": last_stage,
            "stage_history": [last_stage] if last_stage else [],
            "wall_s": round(min(wall_cap_s, time.perf_counter() - started), 6),
            "peak_rss_mb": None,
            "error": None if timed_out else "child produced no result payload",
        }
    try:
        return json.loads(bytes(data_buf).decode("utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "task_id": task["task_id"],
            "phase": phase,
            "evaluation_id": task["evaluation_id"],
            "historical_model_hash": task["historical_model_hash"],
            "v6_model_hash": task["v6_model_hash"],
            "L": task["L"],
            "H": task["H"],
            "mode": task["mode"],
            "complete_set": phase == "complete",
            "semantics": "legacy" if phase == "legacy_complete" else "v6",
            "execution_status": "ERROR",
            "verdict": None,
            "inconclusive_reason": None,
            "enumeration_complete": None,
            "certificates_count": None,
            "overlap_min": None,
            "overlap_max": None,
            "overlap_width": None,
            "overlap_inside_window": None,
            "counters": last_counters,
            "stage": last_stage,
            "stage_history": [last_stage] if last_stage else [],
            "wall_s": round(time.perf_counter() - started, 6),
            "peak_rss_mb": peak_rss_mb(),
            "error": f"child payload not JSON: {exc}",
        }


def _task_payload_with_stage(task: dict, phase: str, stage_fd: int) -> dict:
    """Same as :func:`_task_payload` but wires the stage pipe writer."""
    holder = {"fd": stage_fd}

    started = time.perf_counter()
    record = {
        "task_id": task["task_id"],
        "phase": phase,
        "evaluation_id": task["evaluation_id"],
        "historical_model_hash": task["historical_model_hash"],
        "v6_model_hash": task["v6_model_hash"],
        "L": task["L"],
        "H": task["H"],
        "mode": task["mode"],
        "complete_set": phase == "complete",
        "semantics": "legacy" if phase == "legacy_complete" else "v6",
        "execution_status": "ERROR",
        "verdict": None,
        "inconclusive_reason": None,
        "enumeration_complete": None,
        "certificates_count": None,
        "overlap_min": None,
        "overlap_max": None,
        "overlap_width": None,
        "overlap_inside_window": None,
        "counters": {},
        "stage": None,
        "stage_history": [],
        "wall_s": None,
        "peak_rss_mb": None,
        "error": None,
    }
    state = {"stage": None, "history": [], "counters": {}}

    def hook(stage: str, counters) -> None:
        state["stage"] = stage
        state["history"].append(stage)
        state["counters"] = counters or {}
        try:
            os.write(
                stage_fd,
                (json.dumps({"stage": stage, "counters": counters or {}}) + "\n").encode(),
            )
        except OSError:
            pass

    try:
        params = task["model_params"]
        tpn, default_L, default_H = generate_rmt_fms(**params)
        record["generated_L"] = int(default_L)
        record["generated_H"] = float(default_H)
        record["model_LH_matches_archived"] = (
            int(default_L) == task["L"] and abs(float(default_H) - task["H"]) < 1e-12
        )
        regenerated_hash = v6_model_hash(tpn)
        record["regenerated_v6_model_hash"] = regenerated_hash
        record["regenerated_structure_hash"] = v6_structure_hash(tpn)
        if regenerated_hash != task["v6_model_hash"]:
            record["execution_status"] = "MODEL_IDENTITY_MISMATCH"
            record["error"] = (
                "regenerated v6 model hash differs from frozen recovered hash"
            )
            record["wall_s"] = round(time.perf_counter() - started, 6)
            record["peak_rss_mb"] = peak_rss_mb()
            return record
        if phase == "legacy_complete":
            scg_builder._compute_successor = legacy_successor()
        out = verify(
            tpn,
            task["L"],
            task["H"],
            complete_set=(phase == "complete" or phase == "legacy_complete"),
            progress_hook=hook,
        )
        certificates = out.get("certificates") or []
        stats = out.get("stats") or {}
        lower = upper = None
        if certificates:
            try:
                lower = min(float(cert.overlap[0]) for cert in certificates)
                upper = max(float(cert.overlap[1]) for cert in certificates)
            except Exception:  # noqa: BLE001 - defensive, keep the verdict
                lower = upper = None
        record.update(
            {
                "execution_status": "COMPLETED",
                "verdict": out.get("result"),
                "inconclusive_reason": out.get("inconclusive_reason"),
                "enumeration_complete": out.get("enumeration_complete"),
                "certificates_count": len(certificates),
                "overlap_min": lower,
                "overlap_max": upper,
                "overlap_width": None
                if lower is None or upper is None
                else round(upper - lower, 6),
                "overlap_inside_window": None
                if lower is None or upper is None
                else bool(0.0 <= lower <= upper <= task["H"] + 1e-9),
                "counters": stats,
                "stage": state["stage"],
                "stage_history": state["history"],
            }
        )
    except BaseException as exc:  # noqa: BLE001 - child must always return a record
        record["execution_status"] = "ERROR"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["stage"] = state["stage"]
        record["stage_history"] = state["history"]
        record["counters"] = state["counters"]
    record["wall_s"] = round(time.perf_counter() - started, 6)
    record["peak_rss_mb"] = peak_rss_mb()
    return record


# ---------------------------------------------------------------------------
# Parallel run with resume
# ---------------------------------------------------------------------------
def raw_path(phase: str) -> Path:
    return RAW_DIR / PHASE_FILES[phase]


def load_raw_records(phase: str) -> dict[str, dict]:
    records: dict[str, dict] = {}
    path = raw_path(phase)
    if not path.is_file():
        return records
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "task_id" in record:
                records[record["task_id"]] = record
    return records


def select_phase_tasks(phase: str, manifest: dict, subset: str) -> list[dict]:
    tasks = list(manifest["tasks"])
    if phase == "first":
        return tasks
    if phase == "complete":
        amb = (
            [task for task in tasks if task["archived"]["verdict"] == "AMB"]
            if subset == "amb"
            else tasks
        )
        # Prioritise the legacy-replay sample so that the archived-vs-v6 overlap
        # endpoint comparison is available on the largest possible intersection.
        priority = {task["task_id"] for task in legacy_sample_tasks(tasks)}
        return sorted(
            amb, key=lambda task: (task["task_id"] not in priority, task["task_id"])
        )
    if phase == "legacy_complete":
        return legacy_sample_tasks(tasks)
    raise ValueError(f"unknown phase {phase!r}")


def write_checkpoint(phase: str, done: int, total: int, started: float) -> None:
    payload = {
        "phase": phase,
        "done": done,
        "total": total,
        "coverage_fraction": round(done / total, 6) if total else None,
        "elapsed_s": round(time.perf_counter() - started, 3),
        "updated_at_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "raw_path": str(raw_path(phase).relative_to(ROOT)),
    }
    (CKPT_DIR / f"{phase}_progress.json").write_text(json.dumps(payload, indent=2) + "\n")


def run_phase(
    phase: str,
    workers: int,
    wall_cap_s: float,
    subset: str,
    resume: bool,
    limit: int | None,
) -> dict:
    ensure_dirs()
    manifest = load_manifest_and_verify_freeze()
    all_tasks = select_phase_tasks(phase, manifest, subset)
    tasks = all_tasks[:limit] if limit is not None else all_tasks
    recorded = load_raw_records(phase) if resume else {}
    if not resume and recorded:
        backup = raw_path(phase).with_suffix(
            f".jsonl.bak-{time.strftime('%Y%m%d%H%M%S')}"
        )
        raw_path(phase).replace(backup)
        log(f"existing raw file moved to {backup.name}", phase=phase)
        recorded = {}
    pending = [
        task
        for task in tasks
        if recorded.get(task["task_id"], {}).get("execution_status")
        not in RECORDED_STATUSES
    ]
    log(
        f"phase={phase}: planned={len(tasks)} already_recorded={len(recorded)} "
        f"pending={len(pending)} workers={workers} wall_cap={wall_cap_s}s",
        phase=phase,
    )
    if not pending:
        write_checkpoint(phase, len(tasks) - len(pending), len(tasks), time.perf_counter())
        return {"planned": len(tasks), "ran": 0, "already_records": len(recorded)}

    ctx = mp.get_context("spawn")
    started = time.perf_counter()
    results_q = ctx.Queue()
    done = [0]
    total = len(all_tasks)
    raw_file = raw_path(phase).open("a")

    def handle(record: dict) -> None:
        raw_file.write(json.dumps(record, default=str) + "\n")
        raw_file.flush()
        recorded[record["task_id"]] = record
        done[0] += 1
        if done[0] % 25 == 0 or done[0] == len(pending):
            write_checkpoint(phase, len(recorded), total, started)
            log(
                f"phase={phase}: {len(recorded)}/{total} recorded "
                f"({done[0]}/{len(pending)} this run, "
                f"{time.perf_counter() - started:.1f}s)",
                phase=phase,
            )

    def submit_all(pool, task_list):
        for task in task_list:
            pool.apply_async(
                _execute_task_in_child,
                (task, phase, wall_cap_s),
                callback=results_q.put,
                error_callback=lambda exc, tid=task["task_id"]: results_q.put(
                    {
                        "task_id": tid,
                        "phase": phase,
                        "execution_status": "ERROR",
                        "verdict": None,
                        "error": f"worker callback: {type(exc).__name__}: {exc}",
                    }
                ),
            )

    pool = ctx.Pool(processes=workers)
    submit_list = list(pending)
    submitted = {task["task_id"] for task in submit_list}
    collected: set[str] = set()
    hard_kill_retry = False
    try:
        submit_all(pool, submit_list)
        while len(collected) < len(submit_list):
            try:
                record = results_q.get(timeout=wall_cap_s + 120)
            except Exception:  # noqa: BLE001 - mp queue Empty
                lost = sorted(submitted - collected)
                log(
                    f"phase={phase}: hard hang detected; terminating pool with "
                    f"{len(lost)} lost tasks",
                    phase=phase,
                )
                pool.terminate()
                pool.join()
                if hard_kill_retry:
                    for task_id in lost:
                        handle(
                            {
                                "task_id": task_id,
                                "phase": phase,
                                "execution_status": "TIMEOUT_HARD_KILL",
                                "verdict": None,
                                "error": "pool terminated after a second hard hang",
                            }
                        )
                        collected.add(task_id)
                    break
                hard_kill_retry = True
                pool = ctx.Pool(processes=workers)
                lost_ids = set(lost)
                lost_tasks = [task for task in submit_list if task["task_id"] in lost_ids]
                submitted = set(lost_ids)
                submit_all(pool, lost_tasks)
                continue
            task_id = record.get("task_id")
            if task_id is None:
                continue
            if task_id in collected:
                continue
            collected.add(task_id)
            handle(record)
    finally:
        raw_file.close()
        try:
            pool.close()
            pool.join()
        except Exception:  # noqa: BLE001
            try:
                pool.terminate()
            except Exception:  # noqa: BLE001
                pass
    write_checkpoint(phase, len(recorded), total, started)
    elapsed = round(time.perf_counter() - started, 3)
    summary = {
        "phase": phase,
        "planned": len(tasks),
        "ran_this_invocation": done[0],
        "recorded_total": len(recorded),
        "total_planned": total,
        "coverage_fraction": round(len(recorded) / total, 6) if total else None,
        "elapsed_s": elapsed,
        "workers": workers,
        "wall_cap_s": wall_cap_s,
        "subset": subset,
        "completed_at_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (SUM_DIR / f"run_{phase}.json").write_text(json.dumps(summary, indent=2) + "\n")
    log(f"phase={phase} finished: {summary}", phase=phase)
    return summary


# ---------------------------------------------------------------------------
# SCG fingerprint sample (archived vs v6 semantics)
# ---------------------------------------------------------------------------
def run_scg_fingerprint_sample(manifest: dict) -> dict:
    tasks = [task for task in manifest["tasks"]]
    ordered = sorted(
        tasks, key=lambda task: sha256_text(f"scg-fingerprint-v1|{task['task_id']}")
    )
    # One representative task per distinct regenerated v6 model.
    seen: set[str] = set()
    sample = []
    for task in ordered:
        if task["v6_model_hash"] in seen:
            continue
        seen.add(task["v6_model_hash"])
        sample.append(task)
        if len(sample) >= SCG_FP_SAMPLE_SIZE:
            break
    legacy_info = legacy_successor_info()
    rows = []
    for index, task in enumerate(sample, start=1):
        tpn, _, _ = generate_rmt_fms(**task["model_params"])
        legacy = scg_fingerprint(build_scg_with(legacy_successor(), tpn))
        current = scg_fingerprint(build_scg_with(scg_builder._compute_successor, tpn))
        row = {
            "sample_index": index,
            "task_id": task["task_id"],
            "evaluation_id": task["evaluation_id"],
            "historical_model_hash": task["historical_model_hash"],
            "v6_model_hash": task["v6_model_hash"],
            "n_transitions": task["n_transitions"],
            "tier": task["tier"],
            "archived_verdict": task["archived"]["verdict"],
            "n_classes_archived_semantics": legacy["n_classes"],
            "n_classes_v6": current["n_classes"],
            "n_edges_archived_semantics": legacy["n_edges"],
            "n_edges_v6": current["n_edges"],
            "classes_structure_equal": legacy["classes_structure_digest"]
            == current["classes_structure_digest"],
            "classes_full_dbm_equal": legacy["classes_full_digest"]
            == current["classes_full_digest"],
            "edges_equal": legacy["edges_digest"] == current["edges_digest"],
            "archived_semantics_classes_digest": legacy["classes_full_digest"],
            "v6_classes_digest": current["classes_full_digest"],
            "archived_semantics_edges_digest": legacy["edges_digest"],
            "v6_edges_digest": current["edges_digest"],
        }
        rows.append(row)
        if index % 25 == 0:
            log(f"scg fingerprint sample {index}/{len(sample)}", phase="audit")

    def mean(values):
        return round(statistics.mean(values), 3) if values else None

    summary = {
        "rule": (
            "deterministic ascending sha256('scg-fingerprint-v1'|task_id); one "
            "representative task per distinct regenerated v6 model; first "
            f"{SCG_FP_SAMPLE_SIZE} distinct v6 models in tier A"
        ),
        "sample_size": len(rows),
        "distinct_v6_models_in_sample": len({row["v6_model_hash"] for row in rows}),
        "legacy_semantics_source": legacy_info,
        "counts": {
            "classes_structure_equal": sum(row["classes_structure_equal"] for row in rows),
            "classes_full_dbm_equal": sum(row["classes_full_dbm_equal"] for row in rows),
            "edges_equal": sum(row["edges_equal"] for row in rows),
            "smaller_v6_class_count": sum(
                row["n_classes_v6"] < row["n_classes_archived_semantics"] for row in rows
            ),
            "larger_v6_class_count": sum(
                row["n_classes_v6"] > row["n_classes_archived_semantics"] for row in rows
            ),
            "equal_class_count": sum(
                row["n_classes_v6"] == row["n_classes_archived_semantics"] for row in rows
            ),
        },
        "means": {
            "n_classes_archived_semantics": mean(
                [row["n_classes_archived_semantics"] for row in rows]
            ),
            "n_classes_v6": mean([row["n_classes_v6"] for row in rows]),
            "n_edges_archived_semantics": mean(
                [row["n_edges_archived_semantics"] for row in rows]
            ),
            "n_edges_v6": mean([row["n_edges_v6"] for row in rows]),
        },
        "interpretation": (
            "classes_full_dbm_equal compares the canonical DBM signature of every "
            "state class; a difference means the strong-time successor semantics "
            "changed the archived SCG, so the archived SCG workload counters for "
            "the conflict tier are not reusable."
        ),
    }
    with (SUM_DIR / "scg_fingerprint_sample.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    (SUM_DIR / "scg_fingerprint_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    return summary


# ---------------------------------------------------------------------------
# Drift audit
# ---------------------------------------------------------------------------
def _outcome(record: dict | None) -> str:
    if record is None:
        return "NOT_RUN"
    status = record.get("execution_status")
    if status != "COMPLETED":
        return status or "UNKNOWN"
    return record.get("verdict") or "UNKNOWN"


def _counter(record: dict | None, name: str):
    if not record:
        return None
    counters = record.get("counters") or {}
    value = counters.get(name)
    return value


def _all_pairs(record: dict | None):
    terminal = _counter(record, "terminal_pairs")
    rejected = _counter(record, "enclosure_rejected_pairs")
    if terminal is None:
        return None
    return int(terminal) + int(rejected or 0)


def build_drift_tables(manifest: dict) -> dict:
    tasks = manifest["tasks"]
    first = load_raw_records("first")
    complete = load_raw_records("complete")
    legacy = load_raw_records("legacy_complete")
    detail_rows = []
    matrix = {
        "VER": Counter(),
        "AMB": Counter(),
    }
    problems = defaultdict(list)
    complete_counts = {"VER": 0, "AMB": 0}
    first_counts = Counter()
    for task in tasks:
        tid = task["task_id"]
        rec_first = first.get(tid)
        rec_complete = complete.get(tid)
        rec_legacy = legacy.get(tid)
        archived = task["archived"]
        archived_verdict = archived["verdict"]
        outcome = _outcome(rec_first)
        first_counts[outcome] += 1
        matrix[archived_verdict][outcome] += 1
        complete_ok = rec_complete is not None and rec_complete.get("execution_status") == "COMPLETED"
        # VER rows are fully enumerated in the first pass (no early exit), so
        # their first-pass counters are already complete.  AMB rows stop at the
        # first certificate, so their critical-pair universe is only available
        # from the complete pass; without it the row is not comparable.
        if archived_verdict == "VER":
            counters_pairs = rec_first if _outcome(rec_first) == "VER" else None
            recomputed_feasible = 0 if counters_pairs is not None else None
        else:
            counters_pairs = rec_complete if complete_ok else None
            recomputed_feasible = (
                rec_complete.get("certificates_count") if complete_ok else None
            )
        recomputed_all_pairs = _all_pairs(counters_pairs)
        archived_pairs = archived["critical_pair_count"]
        delta_pairs = (
            None
            if recomputed_all_pairs is None or archived_pairs is None
            else recomputed_all_pairs - archived_pairs
        )
        scg_classes_v6 = _counter(rec_first, "scg_classes")
        scg_edges_v6 = _counter(rec_first, "scg_edges")
        row = {
            "task_id": tid,
            "evaluation_id": task["evaluation_id"],
            "bounded_problem_id": task["bounded_problem_id"],
            "historical_model_hash": task["historical_model_hash"],
            "v6_model_hash": task["v6_model_hash"],
            "tier": task["tier"],
            "L": task["L"],
            "H": task["H"],
            "n_transitions": task["n_transitions"],
            "archived_verdict": archived_verdict,
            "archived_reference_verdict": archived["reference_verdict"],
            "archived_reference_runtime_s": archived["reference_runtime_total_median_s"],
            "archived_proposed_runtime_s": archived["proposed_runtime_total_median_s"],
            "archived_scg_classes": archived["reference_scg_classes"],
            "archived_scg_edges": archived["reference_scg_edges"],
            "archived_critical_pairs": archived_pairs,
            "recomputed_status": rec_first.get("execution_status") if rec_first else "NOT_RUN",
            "recomputed_verdict": _outcome(rec_first),
            "recomputed_inconclusive_reason": rec_first.get("inconclusive_reason") if rec_first else None,
            "recomputed_wall_s": rec_first.get("wall_s") if rec_first else None,
            "recomputed_peak_rss_mb": rec_first.get("peak_rss_mb") if rec_first else None,
            "recomputed_stage": rec_first.get("stage") if rec_first else None,
            "verdict_drift": _outcome(rec_first) != archived_verdict,
            "verdict_transition": f"{archived_verdict}->{_outcome(rec_first)}",
            "complete_pass_status": rec_complete.get("execution_status") if rec_complete else None,
            "complete_pass_wall_s": rec_complete.get("wall_s") if rec_complete else None,
            "complete_pass_timeout": bool(
                rec_complete and rec_complete.get("execution_status") == "TIMEOUT_WALL_CLOCK"
            ),
            "recomputed_scg_classes": scg_classes_v6,
            "recomputed_scg_edges": scg_edges_v6,
            "scg_classes_delta": None
            if scg_classes_v6 is None
            else int(scg_classes_v6) - int(archived["reference_scg_classes"]),
            "scg_edges_delta": None
            if scg_edges_v6 is None
            else int(scg_edges_v6) - int(archived["reference_scg_edges"]),
            "recomputed_all_pairs": recomputed_all_pairs,
            "recomputed_feasible_pairs": recomputed_feasible,
            "critical_pairs_delta": delta_pairs,
            "critical_pairs_ratio": None
            if delta_pairs is None or archived_pairs in (None, 0)
            else round(recomputed_all_pairs / archived_pairs, 6),
            "complete_overlap_min": rec_complete.get("overlap_min") if complete_ok else None,
            "complete_overlap_max": rec_complete.get("overlap_max") if complete_ok else None,
            "complete_overlap_width": rec_complete.get("overlap_width") if complete_ok else None,
            "complete_overlap_inside_window": rec_complete.get("overlap_inside_window")
            if complete_ok
            else None,
            "complete_certificates": rec_complete.get("certificates_count") if complete_ok else None,
            "legacy_replay_recorded": rec_legacy is not None,
            "legacy_replay_verdict": _outcome(rec_legacy) if rec_legacy else None,
            "legacy_replay_all_pairs": _all_pairs(rec_legacy) if rec_legacy else None,
            "legacy_replay_feasible_pairs": rec_legacy.get("certificates_count")
            if rec_legacy
            else None,
            "legacy_replay_overlap_min": rec_legacy.get("overlap_min") if rec_legacy else None,
            "legacy_replay_overlap_max": rec_legacy.get("overlap_max") if rec_legacy else None,
            "legacy_replay_wall_s": rec_legacy.get("wall_s") if rec_legacy else None,
            "legacy_replay_status": rec_legacy.get("execution_status") if rec_legacy else None,
        }
        detail_rows.append(row)
        problems[task["historical_model_hash"]].append(row)
        if complete_ok:
            complete_counts[archived_verdict] += 1

    with (SUM_DIR / "drift_detail.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(detail_rows[0].keys()))
        writer.writeheader()
        writer.writerows(detail_rows)

    matrix_out = {
        archived: {outcome: count for outcome, count in sorted(counter.items())}
        for archived, counter in matrix.items()
    }
    changed = [row for row in detail_rows if row["verdict_drift"]]
    unchanged = [row for row in detail_rows if not row["verdict_drift"]]
    conclusive_pairs = [
        row
        for row in detail_rows
        if row["verdict_drift"]
        and row["recomputed_verdict"] in {"VER", "AMB"}
    ]
    problem_level = {"problems": len(problems), "with_any_row_drift": 0, "all_rows_match": 0}
    for rows in problems.values():
        if any(row["verdict_drift"] for row in rows):
            problem_level["with_any_row_drift"] += 1
        else:
            problem_level["all_rows_match"] += 1
    problem_level["with_complete_pass_censored_rows"] = len(
        {
            row["historical_model_hash"]
            for row in detail_rows
            if row["recomputed_status"] == "TIMEOUT_WALL_CLOCK"
        }
    )
    drift = {
        "denominator_tasks": len(tasks),
        "denominator_bounded_problems": len(problems),
        "archived_verdict_counts": {
            "VER": sum(1 for task in tasks if task["archived"]["verdict"] == "VER"),
            "AMB": sum(1 for task in tasks if task["archived"]["verdict"] == "AMB"),
        },
        "recomputed_outcome_counts": dict(sorted(first_counts.items())),
        "transition_matrix": matrix_out,
        "drift_counts": {
            "rows_with_verdict_change": len(conclusive_pairs),
            "rows_unchanged_verdict": len(tasks) - len(changed),
            "rows_changed_or_not_verdict": len(changed),
            "rows_not_run": first_counts.get("NOT_RUN", 0),
            "rows_timeout_wall_clock": first_counts.get("TIMEOUT_WALL_CLOCK", 0),
            "rows_error": first_counts.get("ERROR", 0)
            + first_counts.get("MODEL_IDENTITY_MISMATCH", 0),
            "rows_inconclusive_inc": first_counts.get("INC", 0),
            "rows_not_decidable": first_counts.get("NOT_RUN", 0)
            + first_counts.get("TIMEOUT_WALL_CLOCK", 0)
            + first_counts.get("ERROR", 0)
            + first_counts.get("MODEL_IDENTITY_MISMATCH", 0)
            + first_counts.get("INC", 0),
        },
        "problem_level": problem_level,
        "complete_pass": {
            "scope": "archived-AMB rows (VER rows enumerate completely in first pass)",
            "archived_amb_rows": sum(
                1 for task in tasks if task["archived"]["verdict"] == "AMB"
            ),
            "recorded": len(complete),
            "completed": sum(
                1 for rec in complete.values() if rec.get("execution_status") == "COMPLETED"
            ),
            "timed_out": sum(
                1
                for rec in complete.values()
                if rec.get("execution_status") == "TIMEOUT_WALL_CLOCK"
            ),
            "completed_by_archived_verdict": complete_counts,
        },
        "changed_verdict_rows": [
            {
                "evaluation_id": row["evaluation_id"],
                "bounded_problem_id": row["bounded_problem_id"],
                "archived_verdict": row["archived_verdict"],
                "recomputed_verdict": row["recomputed_verdict"],
                "n_transitions": row["n_transitions"],
                "archived_reference_runtime_s": row["archived_reference_runtime_s"],
                "recomputed_wall_s": row["recomputed_wall_s"],
            }
            for row in conclusive_pairs
        ],
        "not_decidable_rows": [
            {
                "evaluation_id": row["evaluation_id"],
                "archived_verdict": row["archived_verdict"],
                "recomputed_status": row["recomputed_status"],
                "recomputed_verdict": row["recomputed_verdict"],
                "recomputed_stage": row["recomputed_stage"],
                "recomputed_wall_s": row["recomputed_wall_s"],
            }
            for row in detail_rows
            if row["recomputed_verdict"] not in {"VER", "AMB"}
        ],
    }
    return {"detail_rows": detail_rows, "drift": drift}


def critical_pair_analysis(rows: list[dict]) -> dict:
    comparable = [
        row
        for row in rows
        if row["recomputed_all_pairs"] is not None and row["archived_critical_pairs"] is not None
    ]
    deltas = [row["recomputed_all_pairs"] - row["archived_critical_pairs"] for row in comparable]
    ratios = [
        row["recomputed_all_pairs"] / row["archived_critical_pairs"]
        for row in comparable
        if row["archived_critical_pairs"]
    ]
    by_verdict = {}
    for verdict in ("VER", "AMB"):
        subset = [row for row in comparable if row["archived_verdict"] == verdict]
        subset_deltas = [row["recomputed_all_pairs"] - row["archived_critical_pairs"] for row in subset]
        by_verdict[verdict] = {
            "rows": len(subset),
            "mean_archived": round(
                statistics.mean([row["archived_critical_pairs"] for row in subset]), 2
            )
            if subset
            else None,
            "mean_recomputed": round(
                statistics.mean([row["recomputed_all_pairs"] for row in subset]), 2
            )
            if subset
            else None,
            "equal": sum(1 for delta in subset_deltas if delta == 0),
            "increased": sum(1 for delta in subset_deltas if delta > 0),
            "decreased": sum(1 for delta in subset_deltas if delta < 0),
            "median_delta": statistics.median(subset_deltas) if subset_deltas else None,
        }
    zero_archived = sum(1 for row in comparable if row["archived_critical_pairs"] == 0)
    feasible = [row for row in comparable if row["recomputed_feasible_pairs"] is not None]
    return {
        "definition": {
            "archived": (
                "archived_reference_record.terminal_pairs = sum over candidate "
                "words of |normal paths| * |fault paths| (complete enumeration, "
                "v4 semantics, no enclosure screen)"
            ),
            "recomputed": (
                "recomputed counters terminal_pairs + enclosure_rejected_pairs = "
                "the same normal x fault pair universe under v6 semantics "
                "(first-pass counters for VER rows, complete pass for AMB rows)"
            ),
            "feasible_recomputed": (
                "len(certificates) in the complete pass = LP-feasible ambiguity "
                "critical pairs under v6 semantics; the archived CSV does not "
                "persist this per-row count, so it has no archived counterpart"
            ),
        },
        "rows_comparable": len(comparable),
        "rows_with_zero_archived_pairs": zero_archived,
        "rows_by_archived_verdict": by_verdict,
        "rows_equal": sum(1 for delta in deltas if delta == 0),
        "rows_increased": sum(1 for delta in deltas if delta > 0),
        "rows_decreased": sum(1 for delta in deltas if delta < 0),
        "median_delta": statistics.median(deltas) if deltas else None,
        "mean_ratio_recomputed_over_archived": round(statistics.mean(ratios), 6)
        if ratios
        else None,
        "median_ratio_recomputed_over_archived": round(statistics.median(ratios), 6)
        if ratios
        else None,
        "feasible_pairs": {
            "rows": len(feasible),
            "rows_feasible_positive": sum(
                1 for row in feasible if row["recomputed_feasible_pairs"] > 0
            ),
            "rows_feasible_zero": sum(
                1 for row in feasible if row["recomputed_feasible_pairs"] == 0
            ),
            "mean_recomputed_feasible": round(
                statistics.mean([row["recomputed_feasible_pairs"] for row in feasible]), 2
            )
            if feasible
            else None,
            "amb_rows_with_zero_feasible": sum(
                1
                for row in feasible
                if row["archived_verdict"] == "AMB" and row["recomputed_feasible_pairs"] == 0
            ),
            "ver_rows_with_positive_feasible": sum(
                1
                for row in feasible
                if row["archived_verdict"] == "VER" and row["recomputed_feasible_pairs"] > 0
            ),
        },
    }


def time_domain_analysis(rows: list[dict]) -> dict:
    complete_rows = [
        row
        for row in rows
        if row["archived_verdict"] == "AMB"
        and row["complete_pass_status"] == "COMPLETED"
        and row["complete_overlap_min"] is not None
    ]
    widths = [row["complete_overlap_width"] for row in complete_rows if row["complete_overlap_width"] is not None]
    inside = sum(1 for row in complete_rows if row["complete_overlap_inside_window"] is True)
    legacy_rows = [
        row
        for row in rows
        if row["legacy_replay_status"] == "COMPLETED"
        and row["legacy_replay_overlap_min"] is not None
        and row["complete_overlap_min"] is not None
    ]
    endpoint_deltas = []
    width_deltas = []
    for row in legacy_rows:
        endpoint_deltas.append(
            {
                "task_id": row["task_id"],
                "evaluation_id": row["evaluation_id"],
                "archived_semantics_overlap": [
                    row["legacy_replay_overlap_min"],
                    row["legacy_replay_overlap_max"],
                ],
                "v6_overlap": [row["complete_overlap_min"], row["complete_overlap_max"]],
                "delta_min": row["complete_overlap_min"] - row["legacy_replay_overlap_min"],
                "delta_max": row["complete_overlap_max"] - row["legacy_replay_overlap_max"],
            }
        )
        if row["complete_overlap_min"] is not None and row["legacy_replay_overlap_min"] is not None:
            width_deltas.append(
                (row["complete_overlap_max"] - row["complete_overlap_min"])
                - (row["legacy_replay_overlap_max"] - row["legacy_replay_overlap_min"])
            )
    return {
        "archived_overlap_endpoints_available": False,
        "archived_overlap_endpoints_note": (
            "the archived Proposed shard CSVs and the archived MSCG-LP reference "
            "CSV persist only verdicts/counters, not certificate overlap "
            "endpoints; archived-vs-v6 endpoint comparison is therefore done "
            "with the legacy-semantics replay sample, which reconstructs the "
            "archived SCG successor from git history"
        ),
        "recomputed_amb_rows_with_certificate_overlap": len(complete_rows),
        "recomputed_amb_rows_complete_pass_censored": sum(
            1
            for row in rows
            if row["archived_verdict"] == "AMB"
            and row["complete_pass_status"] == "TIMEOUT_WALL_CLOCK"
        ),
        "overlap_inside_archived_window": inside,
        "overlap_inside_archived_window_fraction": round(inside / len(complete_rows), 6)
        if complete_rows
        else None,
        "overlap_width_p50": percentile(widths, 0.5),
        "overlap_width_p95": percentile(widths, 0.95),
        "overlap_width_max": max(widths) if widths else None,
        "legacy_replay_sample": {
            "rows_recorded": sum(1 for row in rows if row["legacy_replay_recorded"]),
            "rows_completed": sum(
                1
                for row in rows
                if row["legacy_replay_status"] == "COMPLETED"
            ),
            "rows_censored": sum(
                1
                for row in rows
                if row["legacy_replay_status"] == "TIMEOUT_WALL_CLOCK"
            ),
            "rows_with_old_and_new_overlap": len(legacy_rows),
            "endpoint_comparisons": endpoint_deltas,
            "median_delta_min": statistics.median(
                [item["delta_min"] for item in endpoint_deltas]
            )
            if endpoint_deltas
            else None,
            "median_delta_max": statistics.median(
                [item["delta_max"] for item in endpoint_deltas]
            )
            if endpoint_deltas
            else None,
            "median_width_delta": statistics.median(width_deltas) if width_deltas else None,
        },
    }


def legacy_replay_analysis(rows: list[dict]) -> dict:
    sampled = [row for row in rows if row["legacy_replay_recorded"]]
    completed = [row for row in sampled if row["legacy_replay_status"] == "COMPLETED"]
    verdict_agree_archived = sum(
        1 for row in completed if row["legacy_replay_verdict"] == row["archived_verdict"]
    )
    pair_rows = [
        row
        for row in completed
        if row["legacy_replay_all_pairs"] is not None and row["recomputed_all_pairs"] is not None
    ]
    pair_deltas = [
        row["recomputed_all_pairs"] - row["legacy_replay_all_pairs"] for row in pair_rows
    ]
    return {
        "purpose": (
            "archived-semantics replay: the pre-repair _compute_successor is "
            "monkeypatched back into the SCG builder; complete enumeration then "
            "reconstructs the archived critical-pair universe and certificate "
            "overlap endpoints. It validates the pipeline (legacy verdicts "
            "should reproduce the archived verdicts) and supplies the archived "
            "side of the critical-pair/overlap comparison."
        ),
        "sample_size_planned": LEGACY_REPLAY_SAMPLE_SIZE,
        "sample_rule": (
            "deterministic ascending sha256('legacy-replay-v1'|task_id) over "
            "archived-AMB tier-A tasks"
        ),
        "rows_recorded": len(sampled),
        "rows_completed": len(completed),
        "rows_censored": sum(
            1 for row in sampled if row["legacy_replay_status"] == "TIMEOUT_WALL_CLOCK"
        ),
        "verdict_agreement_with_archived": verdict_agree_archived,
        "verdict_agreement_fraction": round(verdict_agree_archived / len(completed), 6)
        if completed
        else None,
        "critical_pair_rows_compared": len(pair_rows),
        "critical_pair_equal": sum(1 for delta in pair_deltas if delta == 0),
        "critical_pair_increased_under_v6": sum(1 for delta in pair_deltas if delta > 0),
        "critical_pair_decreased_under_v6": sum(1 for delta in pair_deltas if delta < 0),
        "critical_pair_median_delta_recomputed_minus_legacy": statistics.median(pair_deltas)
        if pair_deltas
        else None,
    }


# ---------------------------------------------------------------------------
# MSCG-LP reference regeneration plan (planning only, never executed here)
# ---------------------------------------------------------------------------
def mscg_lp_reference_plan(manifest: dict) -> dict:
    reference_path = discover_reference_csv()
    with reference_path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    tier_ids = {task["evaluation_id"] for task in manifest["tasks"]}
    runtimes = [float(row["runtime_total_median"]) for row in rows]
    tier_runtimes = [
        float(row["runtime_total_median"]) for row in rows if row["model_id"] in tier_ids
    ]
    peak_memory = [
        float(row["peak_memory_mb"]) for row in rows if row["peak_memory_mb"]
    ]
    tier_peak_memory = [
        float(row["peak_memory_mb"])
        for row in rows
        if row["model_id"] in tier_ids and row["peak_memory_mb"]
    ]
    total_sum = sum(runtimes)
    tier_sum = sum(tier_runtimes)

    def scenario(label, values, cap_s, workers):
        capped = [min(value, cap_s) if cap_s else value for value in values]
        core_seconds = sum(capped)
        overhead = 1.15
        efficiency = 0.85
        effective_core_seconds = core_seconds * overhead
        wall_hours = effective_core_seconds / (workers * efficiency) / 3600.0
        memory_gib = 4.0
        cpu_cost = effective_core_seconds / 3600.0 * MODAL_CPU_USD_PER_CORE_HOUR
        memory_cost = workers * memory_gib * wall_hours * MODAL_MEMORY_USD_PER_GIB_HOUR
        return {
            "label": label,
            "cells": len(values),
            "wall_cap_s": cap_s,
            "cells_censored_by_cap": sum(1 for value in values if cap_s and value > cap_s),
            "single_core_wall_sum_s": round(core_seconds, 3),
            "single_core_wall_sum_h": round(core_seconds / 3600.0, 3),
            "assumed_workers": workers,
            "assumed_parallel_efficiency": efficiency,
            "assumed_overhead_factor": overhead,
            "assumed_memory_gib_per_worker": memory_gib,
            "estimated_wall_hours": round(wall_hours, 3),
            "estimated_cpu_cost_usd": round(cpu_cost, 4),
            "estimated_memory_cost_usd": round(memory_cost, 4),
            "estimated_total_cost_usd_range": [
                round((cpu_cost + memory_cost) * 0.7, 4),
                round((cpu_cost + memory_cost) * 1.6, 4),
            ],
        }

    scenarios = []
    for workers in (16, 32, 64):
        scenarios.append(scenario(f"minimum_555_problems_300s_cap_{workers}w", tier_runtimes, 300.0, workers))
    scenarios.append(scenario("minimum_555_problems_60s_cap_32w", tier_runtimes, 60.0, 32))
    scenarios.append(scenario("minimum_555_problems_120s_cap_32w", tier_runtimes, 120.0, 32))
    scenarios.append(scenario("full_36450_rows_300s_cap_64w", runtimes, 300.0, 64))

    return {
        "status": "PLANNING_ONLY_NOT_EXECUTED",
        "archived_reference": {
            "path": str(reference_path),
            "sha256": sha256_file(reference_path),
            "rows": len(rows),
            "method": sorted({row["method"] for row in rows}),
            "certificate_mode": sorted({row["certificate_mode"] for row in rows}),
            "n_repeats": sorted({row["n_repeats"] for row in rows}),
            "n_warmup": sorted({row["n_warmup"] for row in rows}),
        },
        "published_estimate_review": {
            "published_estimate_s": 97_962.6,
            "recomputed_from_archived_csv_s": round(total_sum, 3),
            "match": abs(round(total_sum, 3) - 97_962.6) < 0.5,
            "method": (
                "sum of runtime_total_median over the 36,450 archived reference "
                "records; the reference protocol recorded a single pass "
                "(n_repeats=1, n_warmup=0), so the sum is already the per-cell "
                "single-core wall clock and needs no repeat correction. It "
                "excludes orchestration, checkout, artifact upload and retries."
            ),
            "tier_A_subset_s": round(tier_sum, 3),
            "tier_A_share_of_total": round(tier_sum / total_sum, 6),
            "cells_over_60s": sum(1 for value in runtimes if value > 60),
            "cells_over_120s": sum(1 for value in runtimes if value > 120),
            "cells_over_300s": sum(1 for value in runtimes if value > 300),
            "tier_A_cells_over_60s": sum(1 for value in tier_runtimes if value > 60),
            "peak_memory_mb": {
                "p50": round(statistics.median(peak_memory), 1),
                "p95": percentile(peak_memory, 0.95),
                "max": round(max(peak_memory), 1),
                "tier_A_p50": round(statistics.median(tier_peak_memory), 1),
                "tier_A_p95": percentile(tier_peak_memory, 0.95),
                "tier_A_max": round(max(tier_peak_memory), 1),
            },
        },
        "affected_reference_rows": {
            "evidence_driven_minimum": len(tier_runtimes),
            "evidence_driven_minimum_problems": EXPECTED_TIER_PROBLEMS,
            "correspondence": (
                "the 555 conflict-tier bounded problems / 3,645 Proposed rows map "
                "1:1 to 3,645 archived MSCG-LP reference evaluation ids; the "
                "reference rows carry the same model_hash/L/H identities and the "
                "same runtime-affected SCG build, so the minimum reference "
                "recompute set is exactly 3,645 rows"
            ),
            "conservative_upper_bound": len(rows),
            "conservative_reason": (
                "every archived reference record calls the same shared "
                "build_scg/_compute_successor code path, so the 32,805 "
                "no-conflict rows stay inside the conservative bound"
            ),
        },
        "modal_execution_plan": {
            "pricing": {
                "cpu_usd_per_core_hour": MODAL_CPU_USD_PER_CORE_HOUR,
                "memory_usd_per_gib_hour": MODAL_MEMORY_USD_PER_GIB_HOUR,
                "source": "Modal public on-demand rates as provided for this task",
            },
            "cell_execution": (
                "one Modal function invocation per bounded problem (or small "
                "batch of cells per invocation); the reference baseline "
                "verify_mscg_lp_complete(tpn, L, H) runs on a single CPU core"
            ),
            "cpu_request": "1 physical core per cell",
            "memory_request_gib": 2.0,
            "memory_request_note": (
                "archived tier-A peak RSS p95 is "
                f"{percentile(tier_peak_memory, 0.95)} MB and max "
                f"{round(max(tier_peak_memory), 1)} MB; a 2 GiB request leaves "
                "headroom for the container and Python/scipy runtime"
            ),
            "sharding": (
                "shard by sha256(task_id) into N shards; each shard writes "
                "append-only JSONL and resumes by task_id; one Modal volume "
                "holds raw/, checkpoints/ and summary/ so a killed shard "
                "restarts without redoing completed cells"
            ),
            "wall_cap": (
                "do not use the historical 60 s cap for the reference: 725 of "
                "the 3,645 tier-A cells exceed it; use a 300 s cap (max archived "
                "cell below 300 s) and keep the cap in the record"
            ),
            "retry_policy": "one retry per cell on infrastructure error; no retry on wall-cap censoring",
            "parallelism": (
                "16-64 concurrent cell invocations; the 3,645-cell minimum set is "
                "the recommended first target, the 36,450-row full set is a "
                "separate, larger confirmation"
            ),
        },
        "cost_scenarios": scenarios,
        "budget_separation": {
            "formal_v6_matrix_cells": V6_FORMAL_MATRIX_CELLS,
            "statement": (
                "the historical MSCG-LP reference regeneration is an add-on to "
                "the v6 formal experiment and MUST be accounted, executed and "
                "reported separately: separate Modal app/environment, separate "
                "artifact prefix, separate cost line, and never pooled with the "
                "formal 8,280-cell matrix results"
            ),
            "historical_cells_vs_formal_cells": len(tier_runtimes),
            "historical_cell_count_ratio_to_formal": round(
                len(tier_runtimes) / V6_FORMAL_MATRIX_CELLS, 6
            ),
        },
        "limitations": [
            "cost figures are planning estimates derived from archived single-pass wall times with explicit overhead/efficiency assumptions; no Modal run was executed for this plan",
            "the archived CSV has no certificate overlap endpoints, so a reference regeneration must persist them if the time-feasible-domain comparison is to be extended",
            "a 300 s cap keeps all archived cells below the cap, but v6 semantics can increase per-cell enumeration and therefore runtime; re-measure on a 200-cell pilot before the full shard run",
        ],
    }


# ---------------------------------------------------------------------------
# Audit driver
# ---------------------------------------------------------------------------
def run_audit(manifest: dict) -> dict:
    ensure_dirs()
    drift_payload = build_drift_tables(manifest)
    detail_rows = drift_payload["detail_rows"]
    drift = drift_payload["drift"]
    critical = critical_pair_analysis(detail_rows)
    time_domain = time_domain_analysis(detail_rows)
    legacy_analysis = legacy_replay_analysis(detail_rows)
    scg_summary = run_scg_fingerprint_sample(manifest)
    reference_plan = mscg_lp_reference_plan(manifest)
    (SUM_DIR / "mscg_lp_reference_plan.json").write_text(
        json.dumps(reference_plan, indent=2) + "\n"
    )

    first_run = json.loads((SUM_DIR / "run_first.json").read_text()) if (
        SUM_DIR / "run_first.json"
    ).is_file() else {}
    complete_run = json.loads((SUM_DIR / "run_complete.json").read_text()) if (
        SUM_DIR / "run_complete.json"
    ).is_file() else {}
    legacy_run = json.loads((SUM_DIR / "run_legacy_complete.json").read_text()) if (
        SUM_DIR / "run_legacy_complete.json"
    ).is_file() else {}
    # Coverage is always derived from the raw JSONL (the authoritative record),
    # so an interrupted/paused phase still reports its true coverage.
    raw_first = load_raw_records("first")
    raw_complete = load_raw_records("complete")
    raw_legacy = load_raw_records("legacy_complete")

    def _phase_coverage(raw_records: dict, planned: int) -> dict:
        statuses = Counter(
            rec.get("execution_status") for rec in raw_records.values()
        )
        return {
            "planned": planned,
            "recorded": len(raw_records),
            "recorded_fraction": round(len(raw_records) / planned, 6) if planned else None,
            "completed": statuses.get("COMPLETED", 0),
            "timeout_wall_clock": statuses.get("TIMEOUT_WALL_CLOCK", 0),
            "error": statuses.get("ERROR", 0)
            + statuses.get("MODEL_IDENTITY_MISMATCH", 0),
        }

    amb_planned = sum(
        1 for task in manifest["tasks"] if task["archived"]["verdict"] == "AMB"
    )
    coverage = {
        "primary_verdict_pass": _phase_coverage(raw_first, len(manifest["tasks"])),
        "complete_pass_amb_subset": _phase_coverage(raw_complete, amb_planned),
        "legacy_replay_sample": _phase_coverage(raw_legacy, LEGACY_REPLAY_SAMPLE_SIZE),
        "definitions": {
            "primary_verdict_coverage": (
                "rows with a recorded primary result / 3,645 frozen tasks; this is "
                "the coverage of the verdict-drift denominator"
            ),
            "critical_pair_coverage": (
                "VER rows are complete by construction in the first pass; AMB rows "
                "only contribute when their complete pass finished (see "
                "critical_pairs.rows_comparable / 3,645)"
            ),
        },
    }
    frozen_sha = (
        (SUM_DIR / "frozen_manifest.sha256").read_text().split()[0]
        if (SUM_DIR / "frozen_manifest.sha256").is_file()
        else None
    )
    summary = {
        "schema_version": DRIFT_SCHEMA_VERSION,
        "snapshot_status": "PAUSED_FOR_SAFE_SNAPSHOT",
        "snapshot_note": (
            "Primary verdict pass is complete (3,645/3,645). The auxiliary "
            "complete-set pass on archived-AMB rows was suspended at a task "
            "boundary by an explicit safe-pause request; it is resumable by "
            "task_id. No verdict, counter or file in the primary pass depends "
            "on the suspended pass."
        ),
        "resume_commands": [
            ".venv/bin/python scripts/recompute_v6_historical_impact.py run --phase complete",
            ".venv/bin/python scripts/recompute_v6_historical_impact.py audit",
        ],
        "frozen_manifest": {
            "path": str(PROTOCOL_MANIFEST.relative_to(ROOT)),
            "sha256": frozen_sha,
            "tasks": len(manifest["tasks"]),
            "bounded_problems": manifest["counts"]["bounded_problems"],
            "archived_verdicts": manifest["counts"]["archived_verdicts"],
        },
        "scope_separation": manifest["scope_separation"],
        "recompute_protocol": manifest["recompute_protocol"],
        "runs": {
            "first": first_run,
            "complete": complete_run,
            "legacy_complete": legacy_run,
        },
        "coverage": coverage,
        "verdict_drift": drift,
        "critical_pairs": critical,
        "time_domain": time_domain,
        "legacy_replay": legacy_analysis,
        "scg_fingerprint_sample": scg_summary,
        "mscg_lp_reference_plan_file": str(
            (SUM_DIR / "mscg_lp_reference_plan.json").relative_to(ROOT)
        ),
        "inputs": manifest["source_inputs"],
        "outputs": {
            "raw_jsonl": {phase: str(raw_path(phase).relative_to(ROOT)) for phase in PHASE_FILES},
            "drift_detail_csv": str((SUM_DIR / "drift_detail.csv").relative_to(ROOT)),
            "summary_json": str((SUM_DIR / "summary.json").relative_to(ROOT)),
            "scg_fingerprint_sample_csv": str(
                (SUM_DIR / "scg_fingerprint_sample.csv").relative_to(ROOT)
            ),
            "frozen_manifest_sha256": str(
                (SUM_DIR / "frozen_manifest.sha256").relative_to(ROOT)
            ),
        },
        "limitations": [
            "the archived Proposed shards carry no mode column; FIRST_CERTIFICATE is the documented assumption, supported by the 0.108 median proposed/reference runtime ratio",
            "the archived Proposed shards carry no verifier counters, so the archived critical-pair count is taken from the paired archived MSCG-LP reference record (complete enumeration, different baseline implementation)",
            "archived certificate overlap endpoints were never persisted, so archived-vs-v6 overlap endpoint deltas are only available on the legacy-semantics replay sample",
            "the conflict tier is a necessary-condition exposure screen; the 32,805 no-conflict rows remain inside the conservative upper bound and are not certified unaffected",
            "the complete pass is censored at 60 s for the slowest AMB rows; censored rows keep their first-pass verdict but have no complete critical-pair count",
            "verdict drift measured here is for the historical rmt_fms baseline only and does not transfer to the 8,280-cell v6 formal matrix",
        ],
    }
    (SUM_DIR / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    log(
        "audit summary: "
        + json.dumps(
            {
                "drift": drift["drift_counts"],
                "matrix": drift["transition_matrix"],
                "critical_pairs": {
                    "rows_comparable": critical["rows_comparable"],
                    "equal": critical["rows_equal"],
                    "increased": critical["rows_increased"],
                    "decreased": critical["rows_decreased"],
                },
                "scg_fp": scg_summary["counts"],
            }
        ),
        phase="audit",
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    manifest_p = sub.add_parser("manifest", help="freeze the task list")
    manifest_p.add_argument("--force", action="store_true")
    run_p = sub.add_parser("run", help="run a recomputation phase")
    run_p.add_argument("--phase", choices=sorted(PHASE_FILES), required=True)
    run_p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    run_p.add_argument("--wall-cap", type=float, default=WALL_CAP_S)
    run_p.add_argument("--subset", choices=["amb", "all"], default="amb")
    run_p.add_argument("--limit", type=int, default=None)
    run_p.add_argument("--no-resume", action="store_true")
    sub.add_parser("audit", help="build drift tables and plans")
    all_p = sub.add_parser("all", help="manifest + first + complete + audit")
    all_p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    all_p.add_argument("--wall-cap", type=float, default=WALL_CAP_S)
    all_p.add_argument("--skip-complete", action="store_true")
    all_p.add_argument("--skip-legacy", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    ensure_dirs()
    if args.command == "manifest":
        build_frozen_manifest(force=args.force)
        return 0
    if args.command == "run":
        run_phase(
            phase=args.phase,
            workers=max(1, min(args.workers, DEFAULT_WORKERS)),
            wall_cap_s=args.wall_cap,
            subset=args.subset,
            resume=not args.no_resume,
            limit=args.limit,
        )
        return 0
    if args.command == "audit":
        manifest = load_manifest_and_verify_freeze()
        run_audit(manifest)
        return 0
    if args.command == "all":
        manifest = build_frozen_manifest(force=False)
        run_phase("first", max(1, min(args.workers, DEFAULT_WORKERS)), args.wall_cap, "amb", True, None)
        if not args.skip_complete:
            run_phase("complete", max(1, min(args.workers, DEFAULT_WORKERS)), args.wall_cap, "amb", True, None)
        if not args.skip_legacy:
            run_phase(
                "legacy_complete",
                max(1, min(args.workers, DEFAULT_WORKERS)),
                args.wall_cap,
                "amb",
                True,
                None,
            )
        run_audit(manifest)
        return 0
    raise RuntimeError(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
