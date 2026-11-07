#!/usr/bin/env python3
"""Local orchestrator for the v6 Modal execution (formal 8,280-cell matrix).

This script never starts a paid Modal workload on its own: ``plan`` and
``simulate`` are free, and ``pilot``/``run``/``resume`` require the explicit
``--confirm-budget`` flag that the Lead passes only after the budget gate is
confirmed.

Commands
--------
plan        read the frozen task list, build the model-colocated shard plan and
            print shard/task/CPU-hour/cost estimates.  Never imports Modal,
            never creates resources.
pilot       run the pre-registered Pilot cells (64 cells / 6 frozen shards from
            protocol/v6_modal_pilot_tasks.json) through the real Modal
            infrastructure; --pilot-shards N limits it to the first N shards.
run         run the formal matrix on Modal.
status      read-only remote state summary (no containers started).
download    copy raw records / ledgers / hashes / metrics / run metadata to
            ``work/v6_modal_<kind>_<run_id>/`` and write ``run_manifest.json``.
resume      re-spawn only shards that are not complete; completed tasks inside
            a shard are skipped by the per-task completion ledger.
simulate    local, free end-to-end self-test: fake Volume, 2 shards, atomic
            writes, hash/ledger dedupe, resume, timeout path and audit tamper
            detection.

Examples
--------
    python scripts/run_v6_modal.py plan --dry-run --pilot
    python scripts/run_v6_modal.py plan --dry-run --formal
    python scripts/run_v6_modal.py pilot --confirm-budget --pilot-shards 2
    python scripts/run_v6_modal.py run --confirm-budget --max-parallel 12
    python scripts/run_v6_modal.py status --run-id formal-20261107T180000
    python scripts/run_v6_modal.py simulate
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODAL_DIR = ROOT / "modal_app"
RUNNER_PATH = MODAL_DIR / "v6_modal_runner.py"
WORKER_PATH = MODAL_DIR / "v6_shard_worker.py"
BUILD_SHARDS_PATH = ROOT / "scripts" / "build_v6_modal_shards.py"
AUDIT_PATH = ROOT / "scripts" / "audit_v6_modal_results.py"

FORMAL_SOURCE = ROOT / "protocol" / "v6_formal_tasks"
FORMAL_MANIFEST = ROOT / "protocol" / "v6_formal_manifest.json"
PILOT_SOURCE = ROOT / "protocol" / "v6_pilot_tasks"
PILOT_MANIFEST = ROOT / "protocol" / "v6_pilot_manifest.json"
#: Frozen Modal Pilot task list from the Lead (64 cells + explicit shard plan).
PILOT_JSON = ROOT / "protocol" / "v6_modal_pilot_tasks.json"
BUDGET_STATE = ROOT / "work" / "v6_modal_budget" / "budget.json"

DEFAULT_APP_NAME = "tii-v6-formal"
DEFAULT_VOLUME_NAME = "tii-v6-scalability"
DEFAULT_VOLUME_PREFIX = "scalability_v6_formal"
DEFAULT_CPU = 1.0
DEFAULT_MEMORY_MIB = 4096
DEFAULT_FUNCTION_TIMEOUT_S = 5400
DEFAULT_CELL_TIMEOUT_S = 60.0
FORMAL_TASKS = 8280

#: Modal public list rates captured in docs/v6_modal_environment.md
#: (retrieved 2026-11-07; must be re-read on the Modal pricing page before the
#: formal run and updated there, not silently here).
CPU_HOUR_RATE_USD = 0.0473
GIB_HOUR_RATE_USD = 0.008
RATES_DATE = "2026-11-07"

#: Local environment defaults.  No credential is read from these paths; they
#: only locate the Modal client executable / interpreter.
_KNOWN_MODAL_VENV = Path(
    "/Users/lucas/Library/Mobile Documents/com~apple~CloudDocs/WorkingFiles/"
    "在投文章/TII_TPN/02_experiments/firing_interval_adjustment/.venv"
)
_KNOWN_MODAL_CLI = _KNOWN_MODAL_VENV / "bin" / "modal"
_KNOWN_MODAL_PYTHON = _KNOWN_MODAL_VENV / "bin" / "python"

COMMANDS_NEEDING_MODAL = frozenset({"pilot", "run", "status", "download", "resume"})


# --------------------------------------------------------------------------- #
# generic helpers
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def atomic_write_text(path: Path, text: str) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return sha256_bytes(text.encode("utf-8"))


def atomic_write_json(path: Path, value) -> str:
    return atomic_write_text(path, canonical_json(value) + "\n")


def git_head() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def git_branch() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), "branch", "--show-current"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def git_dirty():
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        return bool(out.strip())
    except Exception:  # noqa: BLE001
        return None


def _load_module(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"module not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_worker_module():
    return _load_module("v6_shard_worker", WORKER_PATH)


def load_builder_module():
    return _load_module("build_v6_modal_shards", BUILD_SHARDS_PATH)


def modal_python() -> str | None:
    explicit = os.environ.get("V6_MODAL_PYTHON")
    if explicit and Path(explicit).is_file():
        return explicit
    if _KNOWN_MODAL_PYTHON.is_file():
        return str(_KNOWN_MODAL_PYTHON)
    return None


def modal_cli(explicit: str | None = None) -> str:
    for candidate in (explicit, os.environ.get("V6_MODAL_CLI"), shutil.which("modal"),
                      str(_KNOWN_MODAL_CLI)):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return "modal"


def _require_modal():
    try:
        import modal  # noqa: F401
        return sys.modules["modal"]
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "the Modal SDK is not importable with this interpreter; run this command "
            "with the Modal client virtualenv (e.g. V6_MODAL_PYTHON=/path/to/modal/venv/bin/python) "
            f"or install requirements-modal.lock ({exc})"
        )


def _load_runner_module():
    _require_modal()
    return _load_module("v6_modal_runner", RUNNER_PATH)


def _apply_runner_env(args, *, creating_resources: bool):
    os.environ["V6_REPO_ROOT"] = str(ROOT)
    os.environ["V6_APP_NAME"] = args.app_name
    os.environ["V6_VOLUME_NAME"] = args.volume_name
    os.environ["V6_VOLUME_PREFIX"] = args.volume_prefix
    os.environ["V6_VOLUME_CREATE"] = "1" if creating_resources else "0"
    os.environ["V6_CPU"] = str(args.cpu)
    os.environ["V6_MEMORY_MIB"] = str(args.memory_mib)
    os.environ["V6_FUNCTION_TIMEOUT_S"] = str(args.function_timeout)
    os.environ["V6_CELL_TIMEOUT_S"] = str(args.cell_timeout)
    if getattr(args, "profile", None):
        os.environ["MODAL_PROFILE"] = args.profile


def sanitize_run_id(value: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    cleaned = "".join(char if char in allowed else "-" for char in value.strip())
    if not cleaned:
        raise ValueError("empty run id")
    return cleaned


def default_run_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return sanitize_run_id(f"{kind}-{git_head()[:8]}-{stamp}")


# --------------------------------------------------------------------------- #
# billing observation (read-only)
# --------------------------------------------------------------------------- #
def billing_snapshot(cli: str, *, profile: str | None = None) -> dict:
    """Read the monthly metered cost with the official CLI.

    Only the metered cost and its resource breakdown are kept; raw CLI output
    (which may contain workspace metadata) is never printed or stored.
    """
    command = [cli, "billing", "summary", "--for", "this month", "--json"]
    if profile:
        command.extend(["--profile", profile])
    snapshot = {
        "command": "modal billing summary --for 'this month' --json",
        "retrieved_at": utc_now(),
        "ok": False,
    }
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except Exception as exc:  # noqa: BLE001 - billing is an observation, never a blocker
        snapshot["error"] = type(exc).__name__
        return snapshot
    if process.returncode != 0:
        snapshot["error"] = f"exit code {process.returncode}"
        return snapshot
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        snapshot["error"] = "unparseable billing summary JSON"
        return snapshot
    snapshot.update({
        "ok": True,
        "metered_cost_usd": payload.get("metered_cost"),
        "metered_cost_breakdown_usd": payload.get("metered_cost_breakdown"),
    })
    return snapshot


def billing_delta(before: dict, after: dict) -> dict:
    result = {"available": False, "note": "monthly metered cost difference is an estimate, not a run invoice"}
    try:
        delta = float(after["metered_cost_usd"]) - float(before["metered_cost_usd"])
        result.update({"available": True, "metered_cost_delta_usd": round(delta, 6)})
    except (KeyError, TypeError, ValueError):
        result["error"] = "metered cost snapshot unavailable"
    return result


# --------------------------------------------------------------------------- #
# live budget guard (equivalent to scripts/modal_budget_guard.py check)
# --------------------------------------------------------------------------- #
BUDGET_SAFETY_FACTOR = 1.30


class BudgetMonitor:
    """Stop spawning new shards when the authorized Modal cap is at risk.

    Mirrors ``scripts/modal_budget_guard.py check``: the workspace monthly
    metered-cost delta since the run started is combined with a compute-time
    estimate, projected to the full formal matrix with a 1.3 safety factor, and
    compared with the user-authorized cap.  Only metered cost is read; tokens or
    credential material are never touched.
    """

    def __init__(self, args, *, kind: str, planned_tasks: int):
        self.args = args
        self.kind = kind
        self.planned_tasks = int(planned_tasks)
        self.threshold = float(args.budget_threshold_usd)
        self.check_seconds = float(args.billing_check_seconds)
        state = {}
        budget_path = Path(args.budget_state) if args.budget_state else BUDGET_STATE
        try:
            if budget_path.is_file():
                state = json.loads(budget_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        self.budget_path = str(budget_path)
        self.cap_usd = float(state.get("cap_usd") or args.budget_cap_usd)
        self.baseline_metered_usd = state.get("baseline_metered_cost_usd")
        self.start_metered_usd = None
        self.attempted_tasks = 0
        self.task_wall_s = 0.0
        self.spend_usd = 0.0
        self.projected_full_run_usd = None
        self.stopped = False
        self.events: list[dict] = []
        self._last_check = 0.0
        self._usd_per_hour = CPU_HOUR_RATE_USD + (args.memory_mib / 1024.0) * GIB_HOUR_RATE_USD

    def start(self, billing_before: dict | None):
        try:
            self.start_metered_usd = float((billing_before or {})["metered_cost_usd"])
        except (KeyError, TypeError, ValueError):
            self.start_metered_usd = None
        self._last_check = time.monotonic()

    def note_result(self, metrics: dict):
        self.attempted_tasks += int(metrics.get("tasks_total") or 0)
        self.task_wall_s += float(metrics.get("task_wall_s_sum") or 0.0)

    def check(self, *, force: bool = False) -> dict | None:
        now = time.monotonic()
        if not force and now - self._last_check < self.check_seconds:
            return None
        self._last_check = now
        snapshot = billing_snapshot(
            modal_cli(self.args.modal_cli), profile=self.args.profile)
        metered = None
        try:
            metered = float(snapshot["metered_cost_usd"])
        except (KeyError, TypeError, ValueError):
            metered = None
        actual_spend = None
        if metered is not None and self.start_metered_usd is not None:
            actual_spend = metered - self.start_metered_usd
        model_spend = self.task_wall_s / 3600.0 * self._usd_per_hour
        candidates = [value for value in (actual_spend, model_spend) if value is not None]
        self.spend_usd = round(max(candidates) if candidates else 0.0, 6)
        if self.attempted_tasks > 0:
            self.projected_full_run_usd = round(
                self.spend_usd / self.attempted_tasks * FORMAL_TASKS * BUDGET_SAFETY_FACTOR,
                4,
            )
        stop_reasons = []
        if self.spend_usd > self.threshold:
            stop_reasons.append(f"spent ${self.spend_usd:.4f} > ${self.threshold:.2f}")
        if (self.kind == "run" and self.projected_full_run_usd is not None
                and self.projected_full_run_usd > self.threshold):
            stop_reasons.append(
                f"projected formal cost ${self.projected_full_run_usd:.2f} "
                f"> ${self.threshold:.2f}"
            )
        event = {
            "at": utc_now(),
            "attempted_tasks": self.attempted_tasks,
            "task_wall_s": round(self.task_wall_s, 3),
            "actual_metered_delta_usd": None if actual_spend is None else round(actual_spend, 6),
            "model_estimate_usd": round(model_spend, 6),
            "estimated_spend_usd": self.spend_usd,
            "projected_full_run_usd": self.projected_full_run_usd,
            "cap_usd": self.cap_usd,
            "threshold_usd": self.threshold,
            "stop": bool(stop_reasons),
            "stop_reasons": stop_reasons,
        }
        self.events.append(event)
        if stop_reasons:
            self.stopped = True
        return event

    def summary(self) -> dict:
        return {
            "budget_state": self.budget_path,
            "cap_usd": self.cap_usd,
            "baseline_metered_cost_usd": self.baseline_metered_usd,
            "threshold_usd": self.threshold,
            "safety_factor_for_full_matrix_projection": BUDGET_SAFETY_FACTOR,
            "estimated_spend_usd": self.spend_usd,
            "projected_full_run_usd": self.projected_full_run_usd,
            "stopped": self.stopped,
            "events": self.events,
            "note": (
                "equivalent to `scripts/modal_budget_guard.py check`: monthly metered "
                "cost delta since run start and compute-time estimate, projected to the "
                "8,280-cell matrix with a 1.3 safety factor"
            ),
        }


# --------------------------------------------------------------------------- #
# plan resolution
# --------------------------------------------------------------------------- #
def _source_paths(kind: str, args):
    if kind == "pilot":
        explicit = getattr(args, "pilot_manifest", None)
        if explicit is not None:
            return (Path(explicit), Path(explicit))
        default_source = PILOT_JSON if PILOT_JSON.is_file() else PILOT_SOURCE
        default_manifest = PILOT_JSON if PILOT_JSON.is_file() else PILOT_MANIFEST
        return (args.source or default_source, args.source_manifest or default_manifest)
    return (args.source or FORMAL_SOURCE, args.source_manifest or FORMAL_MANIFEST)


def _plan_defaults(kind: str, args):
    builder = load_builder_module()
    if kind == "pilot":
        # A frozen Pilot shard plan embedded in the JSON is used verbatim; the
        # shard count defaults are only a fallback for the older CSV source.
        return (args.shards or builder.PILOT_SHARDS, args.tasks_per_shard)
    return (args.shards or builder.FORMAL_SHARDS, args.tasks_per_shard)


def build_local_plan(kind: str, args) -> dict:
    builder = load_builder_module()
    source, source_manifest = _source_paths(kind, args)
    shards, tasks_per_shard = _plan_defaults(kind, args)
    plan, header, manifest, source_rows = builder.build(
        Path(source), kind=kind, shards=shards, tasks_per_shard=tasks_per_shard,
        source_manifest=Path(source_manifest) if source_manifest else None,
    )
    manifest["source_tree_dirty"] = git_dirty()
    return {
        "plan": plan,
        "header": header,
        "manifest": manifest,
        "source_rows": source_rows,
        "source_source": Path(source),
        "source_manifest": Path(source_manifest) if source_manifest else None,
    }


def load_plan_from_manifest(manifest_path: Path, shard_dir: Path | None = None) -> dict:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if shard_dir is None:
        stem = Path(manifest_path).stem.replace("_manifest", "")
        shard_dir = Path(manifest_path).parent / stem
    shard_dir = Path(shard_dir)
    plan = []
    header = manifest.get("columns")
    for shard in manifest.get("shard_files") or sorted(manifest["shard_sha256"]):
        path = shard_dir / f"shard_{shard}.csv"
        if not path.is_file():
            raise FileNotFoundError(f"shard CSV referenced by manifest is missing: {path}")
        if sha256_file(path) != manifest["shard_sha256"][shard]:
            raise ValueError(f"{path.name}: SHA differs from the manifest")
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            header = list(reader.fieldnames or [])
            tasks = list(reader)
        plan.append({"shard": shard, "tasks": tasks})
    source_info = manifest.get("source") or {}
    source_source = None
    source_manifest = None
    task_dir = source_info.get("task_dir")
    if task_dir:
        candidate = Path(task_dir)
        candidate = candidate if candidate.is_absolute() else ROOT / candidate
        if candidate.exists():
            source_source = candidate
    manifest_path = source_info.get("manifest")
    if manifest_path:
        candidate = Path(manifest_path)
        candidate = candidate if candidate.is_absolute() else ROOT / candidate
        if candidate.is_file():
            source_manifest = candidate
    source_rows = None
    header_from_source = None
    if source_source is not None:
        builder = load_builder_module()
        source_rows, header_from_source, _, _ = builder.load_source_tasks(source_source)
    return {
        "plan": plan,
        "header": header_from_source or header,
        "manifest": manifest,
        "source_rows": source_rows,
        "source_source": source_source,
        "source_manifest": source_manifest,
        "shard_dir": shard_dir,
    }


def resolve_plan(kind: str, args) -> dict:
    if getattr(args, "shard_manifest", None):
        info = load_plan_from_manifest(args.shard_manifest, getattr(args, "shard_dir", None))
        if info["manifest"].get("kind") not in (None, kind):
            print(
                f"warning: shard manifest kind={info['manifest'].get('kind')!r} "
                f"but command kind={kind!r}", file=sys.stderr,
            )
        return info
    return build_local_plan(kind, args)


# --------------------------------------------------------------------------- #
# plan command
# --------------------------------------------------------------------------- #
def plan_payload(info: dict, kind: str, args) -> dict:
    manifest = info["manifest"]
    plan = info["plan"]
    tasks = sum(len(item["tasks"]) for item in plan)
    expected_cell = float(args.expected_cell_seconds)
    cell_cap = float(args.cell_timeout)
    cpu_hours_expected = tasks * expected_cell / 3600.0
    cpu_hours_worst = tasks * cell_cap / 3600.0
    gib = args.memory_mib / 1024.0
    usd_per_cpu_hour = CPU_HOUR_RATE_USD + gib * GIB_HOUR_RATE_USD
    parallel = max(1, args.max_parallel)
    shard_sizes = [len(item["tasks"]) for item in plan]
    warnings = list(manifest.get("warnings") or [])
    if kind == "formal" and tasks != FORMAL_TASKS:
        warnings.append(f"formal plan has {tasks} tasks, protocol quota is {FORMAL_TASKS}")
    if manifest.get("source_tree_dirty"):
        warnings.append("working tree is dirty; freeze the source before a paid run")
    if kind == "formal" and not FORMAL_SOURCE.is_dir() and not args.source:
        warnings.append("protocol/v6_formal_tasks is not frozen; formal run must not start")
    return {
        "schema": "v6-modal-plan-1",
        "dry_run": True,
        "creates_modal_resources": False,
        "modal_sdk_imported": False,
        "kind": kind,
        "source": manifest.get("source"),
        "source_sha": manifest.get("source_sha"),
        "source_tree_dirty": manifest.get("source_tree_dirty"),
        "shards": len(plan),
        "tasks": tasks,
        "tasks_per_shard": {"min": min(shard_sizes), "max": max(shard_sizes),
                            "expected": sum(shard_sizes) / len(shard_sizes)},
        "distinct_model_hashes": manifest.get("distinct_model_hashes"),
        "distinct_bounded_problems": manifest.get("distinct_bounded_problems"),
        "experiments": manifest.get("experiments"),
        "resources": {
            "cpu": args.cpu, "memory_mib": args.memory_mib,
            "function_timeout_s": args.function_timeout,
            "cell_timeout_s": args.cell_timeout,
            "max_parallel": parallel,
            "note": "cpu=1 is one physical core on Modal; no GPU is requested",
        },
        "compute": {
            "expected_seconds_per_cell": expected_cell,
            "expected_cell_seconds_source": "docs/v6_gate_decision.md conservative expectation",
            "cpu_hours_worst_case": round(cpu_hours_worst, 3),
            "cpu_hours_expected": round(cpu_hours_expected, 3),
            "wall_hours_expected_at_parallel": round(cpu_hours_expected / parallel, 3),
            "wall_hours_worst_at_parallel": round(cpu_hours_worst / parallel, 3),
        },
        "cost_estimate": {
            "cpu_hour_rate_usd": CPU_HOUR_RATE_USD,
            "memory_gib_hour_rate_usd": GIB_HOUR_RATE_USD,
            "usd_per_machine_hour": round(usd_per_cpu_hour, 5),
            "expected_usd": round(cpu_hours_expected * usd_per_cpu_hour, 3),
            "worst_case_usd": round(cpu_hours_worst * usd_per_cpu_hour, 3),
            "rates_date": RATES_DATE,
            "excluded": ["image build time", "volume storage", "network egress", "retry reserve"],
            "note": "public list rates; re-read modal billing rates before the paid run",
        },
        "storage_estimate": {
            "records": tasks,
            "bytes_per_record_assumption": args.record_bytes,
            "raw_records_mb": round(tasks * args.record_bytes / (1024 * 1024), 3),
            "note": "raw JSON records only; ledgers/hashes/checkpoints add a small constant",
        },
        "run": {
            "app_name": args.app_name,
            "volume_name": args.volume_name,
            "volume_prefix": args.volume_prefix,
            "download_dir": str(Path(args.download_dir) if args.download_dir
                                else ROOT / "work" / f"v6_modal_{kind}_<run_id>"),
        },
        "warnings": warnings,
        "next_commands": [
            f"python scripts/build_v6_modal_shards.py --{kind}   # optional: freeze the shard files",
            f"python scripts/run_v6_modal.py {kind} --confirm-budget"
            + ("" if kind == "pilot" else " --max-parallel 8"),
        ],
    }


def cmd_plan(args) -> int:
    if args.pilot and args.formal:
        raise SystemExit("choose either --pilot or --formal")
    kind = "pilot" if args.pilot else "formal"
    if not args.pilot and not args.formal:
        kind = "formal" if FORMAL_SOURCE.is_dir() else "pilot"
        print(
            f"note: defaulting to the {'formal' if kind == 'formal' else 'Pilot'!r} frozen "
            "task source; pass --pilot/--formal to be explicit", file=sys.stderr,
        )
    try:
        info = resolve_plan(kind, args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        if kind == "formal" and not FORMAL_SOURCE.is_dir():
            print(
                "hint: the formal 8,280 task list is not frozen in protocol/v6_formal_tasks; "
                "run scripts/build_v6_formal.py once the protocol is frozen, or "
                "`plan --dry-run --pilot` for the infrastructure Pilot",
                file=sys.stderr,
            )
        return 2
    payload = plan_payload(info, kind, args)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# volume helpers
# --------------------------------------------------------------------------- #
def _read_remote_bytes(vol, remote_path: str) -> bytes:
    chunks = list(vol.read_file(remote_path))
    return b"".join(chunks)


def _read_remote_text(vol, remote_path: str) -> str:
    return _read_remote_bytes(vol, remote_path).decode("utf-8")


def _read_remote_json(vol, remote_path: str):
    return json.loads(_read_remote_text(vol, remote_path))


def _remote_listdir(vol, remote_path: str, recursive: bool = False):
    try:
        return vol.listdir(remote_path, recursive=recursive)
    except Exception:  # noqa: BLE001 - a missing directory is a normal state
        return []


def _remote_run_metadata(vol, prefix: str, run_id: str) -> dict:
    return _read_remote_json(vol, f"/{prefix}/run_metadata/run_{run_id}.json")


def _remote_manifest(vol, prefix: str) -> dict:
    return _read_remote_json(vol, f"/{prefix}/protocol/modal_shards_manifest.json")


def _remote_next_attempt(vol, prefix: str, shard: str) -> int:
    maximum = -1
    for entry in _remote_listdir(vol, f"/{prefix}/run_metadata/attempts"):
        name = Path(entry.path).name
        if not name.startswith(f"shard_{shard}_attempt_"):
            continue
        try:
            maximum = max(maximum, int(name.rsplit("_", 1)[1].split(".")[0]))
        except (IndexError, ValueError):
            continue
    metrics_path = f"/{prefix}/metrics/shard_{shard}.json"
    try:
        metrics = _read_remote_json(vol, metrics_path)
        maximum = max(maximum, int(metrics.get("attempt", -1)))
    except Exception:  # noqa: BLE001
        pass
    return maximum + 1


def _remote_shards_needing_work(vol, prefix: str, manifest: dict) -> list[str]:
    shard_task_ids = manifest.get("shard_task_ids") or {}
    needed = []
    for shard in sorted(manifest.get("shard_sha256") or {}):
        expected = len(shard_task_ids.get(shard) or [])
        raw_files = [entry for entry in _remote_listdir(vol, f"/{prefix}/raw/shard_{shard}")
                     if Path(entry.path).suffix == ".json"]
        metrics = {}
        try:
            metrics = _read_remote_json(vol, f"/{prefix}/metrics/shard_{shard}.json")
        except Exception:  # noqa: BLE001
            metrics = {}
        complete = (
            metrics.get("status") == "COMPLETED"
            and not metrics.get("tasks_error")
            and not metrics.get("tasks_unfinished")
            and len(raw_files) == expected
        )
        if not complete:
            needed.append(shard)
    return needed


def _shard_state_from_metrics(metrics: dict) -> dict:
    return {
        "status": metrics.get("status", "UNKNOWN"),
        "attempt": metrics.get("attempt"),
        "tasks_total": metrics.get("tasks_total"),
        "tasks_completed": metrics.get("tasks_completed"),
        "tasks_timeout": metrics.get("tasks_timeout"),
        "tasks_error": metrics.get("tasks_error"),
        "tasks_unfinished": metrics.get("tasks_unfinished"),
        "tasks_inc": metrics.get("tasks_inc"),
        "tasks_ver": metrics.get("tasks_ver"),
        "tasks_amb": metrics.get("tasks_amb"),
        "wall_s": metrics.get("wall_s"),
        "peak_rss_mb_max": metrics.get("peak_rss_mb_max"),
        "invocation_id": metrics.get("invocation_id"),
        "image_spec_sha256": metrics.get("image_spec_sha256"),
        "source_sha": metrics.get("source_sha"),
    }


# --------------------------------------------------------------------------- #
# staging and upload
# --------------------------------------------------------------------------- #
def stage_protocol(args, kind: str, run_id: str, info: dict, source_sha: str,
                   plan_manifest: dict) -> dict:
    builder = load_builder_module()
    staging = Path(args.staging_dir) if args.staging_dir else (
        ROOT / "work" / f"v6_modal_{kind}_{run_id}" / "staging"
    )
    if staging.exists():
        shutil.rmtree(staging)
    protocol_dir = staging / "protocol"
    plan_manifest = dict(plan_manifest)
    plan_manifest = builder.write_plan(
        info["plan"], info["header"], protocol_dir / "shards",
        protocol_dir / "modal_shards_manifest.json", plan_manifest,
    )
    source_rows = info.get("source_rows")
    if source_rows is None:
        raise ValueError("cannot stage protocol: frozen source rows were not loaded")
    import io as _io
    source_buffer = _io.StringIO()
    source_writer = csv.DictWriter(
        source_buffer, fieldnames=list(info["header"]), lineterminator="\n",
        extrasaction="ignore",
    )
    source_writer.writeheader()
    source_writer.writerows(source_rows)
    source_tasks_dir = protocol_dir / "source_tasks"
    source_tasks_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(source_tasks_dir / "shard_000.csv", source_buffer.getvalue())
    source_manifest = info.get("source_manifest")
    if source_manifest and Path(source_manifest).is_file():
        shutil.copy2(source_manifest, protocol_dir / "source_manifest.json")
    run_metadata = {
        "schema": "v6-modal-run-metadata-1",
        "run_id": run_id,
        "kind": kind,
        "created_at": utc_now(),
        "host": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "git_branch": git_branch(),
        "git_head": git_head(),
        "source_sha": source_sha,
        "source_tree_dirty": git_dirty(),
        "source_manifest": plan_manifest.get("source", {}).get("manifest"),
        "source_manifest_sha256": plan_manifest.get("source", {}).get("manifest_sha256"),
        "shard_manifest_sha256": sha256_file(protocol_dir / "modal_shards_manifest.json"),
        "shard_sha256": plan_manifest["shard_sha256"],
        "shard_task_ids": plan_manifest.get("shard_task_ids"),
        "shards": len(plan_manifest["shard_sha256"]),
        "tasks": plan_manifest["tasks"],
        "tasks_per_shard": plan_manifest.get("tasks_per_shard"),
        "cell_timeout_s": args.cell_timeout,
        "function_timeout_s": args.function_timeout,
        "cpu": args.cpu,
        "memory_mib": args.memory_mib,
        "scg_cache_dir": "/tmp/scg_cache",
        "image_spec_sha256": load_worker_module().image_spec_sha256(ROOT),
        "image": {
            "spec_sha256": load_worker_module().image_spec_sha256(ROOT),
            "base": "modal.Image.debian_slim(python_version='3.13')",
            "pip": ["numpy==2.3.4", "scipy==1.16.2", "modal==1.6.3"],
            "python": "3.13",
        },
        "executor": "modal",
        "planner": "scripts/run_v6_modal.py",
        "app_name": args.app_name,
        "volume_name": args.volume_name,
        "volume_prefix": args.volume_prefix,
        "max_parallel": args.max_parallel,
        "modal_client": getattr(_require_modal(), "__version__", None),
    }
    metadata_path = staging / "run_metadata" / f"run_{run_id}.json"
    atomic_write_json(metadata_path, run_metadata)
    return {
        "staging_dir": staging,
        "run_metadata": run_metadata,
        "plan_manifest": plan_manifest,
    }


def upload_protocol(vol, staging: Path, prefix: str) -> dict:
    staging = Path(staging)
    uploaded = []
    with vol.batch_upload(force=True) as batch:
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(staging).as_posix()
            remote = f"/{prefix}/{relative}"
            batch.put_file(str(path), remote)
            uploaded.append({"local": str(path), "remote": remote,
                             "sha256": sha256_file(path), "bytes": path.stat().st_size})
    # ``Volume.commit()`` is only valid for a volume mounted inside a container
    # (Modal SDK >= 1.x).  ``batch_upload`` uploads to the volume server
    # directly, so the files are durable when the context exits; no local
    # commit is allowed here.
    return {"files": len(uploaded), "details": uploaded}


# --------------------------------------------------------------------------- #
# Modal execution
# --------------------------------------------------------------------------- #
def _prebuild_image(runner) -> str | None:
    """Build (or reuse) the image once and return its Modal image id."""
    try:
        runner.IMAGE.build(runner.app)
        return runner.IMAGE.object_id
    except Exception as exc:  # noqa: BLE001 - recorded as a warning, not fatal
        print(f"warning: image prebuild/object-id unavailable: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None


def _run_pool(runner, run_id: str, shard_ids, attempts, args,
              monitor: "BudgetMonitor | None" = None) -> tuple[dict, "BudgetMonitor | None"]:
    state = {shard: {"status": "PENDING", "attempts": []} for shard in shard_ids}
    budget_stopped = False
    queue = deque((shard, int(attempts.get(shard, 0))) for shard in shard_ids)
    pending: dict[str, dict] = {}
    max_parallel = max(1, int(args.max_parallel))
    finished = 0
    last_progress = time.monotonic()
    try:
        while queue or pending:
            while queue and not budget_stopped and len(pending) < max_parallel:
                shard, attempt = queue.popleft()
                call = runner.run_shard.spawn(shard, run_id, attempt)
                pending[shard] = {
                    "call": call, "attempt": attempt, "call_id": call.object_id,
                    "started": time.monotonic(),
                }
                state[shard]["status"] = "RUNNING"
                print(
                    f"spawn shard {shard} attempt {attempt} "
                    f"(call {call.object_id})", flush=True,
                )
                last_progress = time.monotonic()
            progressed = False
            if monitor is not None and not budget_stopped:
                event = monitor.check()
                if event and event["stop"]:
                    budget_stopped = True
                    print("BUDGET GUARD STOP: " + "; ".join(event["stop_reasons"]), flush=True)
                    while queue:
                        queued_shard, _ = queue.popleft()
                        state[queued_shard]["status"] = "BUDGET_STOPPED"
                    state["__budget_events__"] = monitor.events
            for shard, item in list(pending.items()):
                try:
                    result = item["call"].get(timeout=0)
                except TimeoutError:
                    if time.monotonic() - last_progress > 120:
                        print(
                            f"waiting: {len(pending)} running, {finished}/{len(shard_ids)} done",
                            flush=True,
                        )
                        last_progress = time.monotonic()
                    continue
                except Exception as exc:  # noqa: BLE001 - infrastructure failure
                    del pending[shard]
                    finished += 1
                    progressed = True
                    error = f"{type(exc).__name__}: {str(exc)[:500]}"
                    state[shard]["attempts"].append({
                        "attempt": item["attempt"], "call_id": item["call_id"],
                        "status": "INFRA_FAILURE", "error": error,
                        "wall_s": round(time.monotonic() - item["started"], 3),
                    })
                    if item["attempt"] < int(args.max_infra_retries):
                        state[shard]["status"] = "RETRYING"
                        queue.append((shard, item["attempt"] + 1))
                        print(f"shard {shard} infrastructure failure, retrying "
                              f"(attempt {item['attempt'] + 1}): {error}", flush=True)
                    else:
                        state[shard]["status"] = "FAILED"
                        print(f"shard {shard} failed after {item['attempt'] + 1} "
                              f"attempt(s): {error}", flush=True)
                    continue
                else:
                    del pending[shard]
                    finished += 1
                    progressed = True
                    metrics = result if isinstance(result, dict) else {}
                    if monitor is not None:
                        monitor.note_result(metrics)
                    status = metrics.get("status", "UNKNOWN")
                    state[shard]["status"] = status
                    state[shard]["attempts"].append({
                        "attempt": item["attempt"], "call_id": item["call_id"],
                        "status": status,
                        "wall_s": round(time.monotonic() - item["started"], 3),
                        "metrics": metrics,
                    })
                    print(
                        f"shard {shard} attempt {item['attempt']} -> {status} "
                        f"(executed {metrics.get('tasks_executed')}, "
                        f"skipped {metrics.get('tasks_skipped_resumed')}, "
                        f"errors {metrics.get('tasks_error')})", flush=True,
                    )
            if pending and not progressed:
                time.sleep(float(args.poll_seconds))
    finally:
        pass
    if monitor is not None:
        state["__budget_events__"] = monitor.events
    return state, monitor


def _download_volume(vol, prefix: str, run_id: str, out_dir: Path,
                     include_checkpoints: bool = False) -> dict:
    from modal.types import FileEntryType  # local import: only needed with Modal

    vol.reload()
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = ["protocol", "run_metadata", "raw", "completion_ledger", "hashes",
              "metrics", "logs"]
    if include_checkpoints:
        wanted.append("checkpoints")
    files = []
    warnings = []
    for top in wanted:
        entries = _remote_listdir(vol, f"/{prefix}/{top}", recursive=True)
        if not entries:
            if top not in {"logs", "checkpoints"}:
                warnings.append(f"no remote files under {top}")
            continue
        for entry in entries:
            if int(getattr(entry, "type", 0)) != int(FileEntryType.FILE):
                continue
            remote = entry.path
            rel = remote.lstrip("/")
            marker = f"{prefix}/"
            if rel.startswith(marker):
                rel = rel[len(marker):]
            data = _read_remote_bytes(vol, remote)
            local = out_dir / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_name(f".{local.name}.tmp.{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, local)
            files.append({"path": rel, "sha256": sha256_bytes(data), "bytes": len(data)})
    files.sort(key=lambda item: item["path"])
    checksums = "".join(f"{item['sha256']}  {item['path']}\n" for item in files)
    atomic_write_text(out_dir / "SHA256SUMS", checksums)
    return {
        "download_dir": str(out_dir),
        "files": len(files),
        "bytes": sum(item["bytes"] for item in files),
        "sha256sums": str(out_dir / "SHA256SUMS"),
        "sha256sums_sha256": sha256_bytes(checksums.encode("utf-8")),
        "warnings": warnings,
    }


def _collect_local_metrics(download_dir: Path) -> dict:
    metrics = {}
    metrics_dir = Path(download_dir) / "metrics"
    if not metrics_dir.is_dir():
        return metrics
    for path in sorted(metrics_dir.glob("shard_*.json")):
        shard = path.stem.replace("shard_", "")
        try:
            metrics[shard] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
    return metrics


def write_run_manifest(*, download_dir: Path, run_id: str, kind: str,
                       run_metadata: dict, plan_manifest: dict, state: dict,
                       billing_before: dict | None, billing_after: dict | None,
                       image_object_id: str | None, download_summary: dict | None,
                       error: str | None = None, budget_summary: dict | None = None) -> dict:
    download_dir = Path(download_dir)
    local_metrics = _collect_local_metrics(download_dir)
    shards = {}
    for shard in sorted(plan_manifest.get("shard_sha256") or {}):
        metrics = local_metrics.get(shard)
        entry = state.get(shard) if isinstance(state, dict) else None
        if metrics:
            summary = _shard_state_from_metrics(metrics)
        elif entry:
            summary = {"status": entry.get("status", "UNKNOWN")}
        else:
            summary = {"status": "NOT_RUN"}
        summary["attempts"] = [
            {key: value for key, value in attempt.items() if key != "metrics"}
            for attempt in (entry or {}).get("attempts", [])
        ]
        summary["call_ids"] = [attempt.get("call_id") for attempt in summary["attempts"]]
        shards[shard] = summary
    status_counts = Counter(summary["status"] for summary in shards.values())
    planned = set(plan_manifest.get("shard_sha256") or {})
    attempted = [shard for shard, summary in shards.items()
                 if summary["status"] not in {"NOT_RUN"}]
    totals = {
        "shards_planned": len(planned),
        "shards_attempted": len(attempted),
        "shards_completed": status_counts["COMPLETED"],
        "shards_incomplete": status_counts["INCOMPLETE"],
        "shards_failed": status_counts["FAILED"],
        "shards_budget_stopped": status_counts["BUDGET_STOPPED"],
        "shards_not_run": status_counts["NOT_RUN"],
        "tasks_completed": sum(int(summary.get("tasks_completed") or 0)
                               for summary in shards.values()),
        "tasks_timeout": sum(int(summary.get("tasks_timeout") or 0)
                             for summary in shards.values()),
        "tasks_error": sum(int(summary.get("tasks_error") or 0)
                           for summary in shards.values()),
        "tasks_inc": sum(int(summary.get("tasks_inc") or 0)
                         for summary in shards.values()),
    }
    manifest = {
        "schema": "v6-modal-run-manifest-1",
        "run_id": run_id,
        "kind": kind,
        "created_at": utc_now(),
        "finished_at": utc_now(),
        "error": error,
        "source_sha": run_metadata.get("source_sha"),
        "git": {
            "branch": run_metadata.get("git_branch"),
            "head": run_metadata.get("git_head"),
            "tree_dirty": run_metadata.get("source_tree_dirty"),
        },
        "app_name": run_metadata.get("app_name"),
        "volume": {
            "name": run_metadata.get("volume_name"),
            "prefix": run_metadata.get("volume_prefix"),
        },
        "image": dict(run_metadata.get("image") or {}),
        "image_object_id": image_object_id,
        "resources": {
            "cpu": run_metadata.get("cpu"),
            "memory_mib": run_metadata.get("memory_mib"),
            "function_timeout_s": run_metadata.get("function_timeout_s"),
            "cell_timeout_s": run_metadata.get("cell_timeout_s"),
            "max_parallel": run_metadata.get("max_parallel"),
        },
        "shard_manifest_sha256": run_metadata.get("shard_manifest_sha256"),
        "shard_sha256": plan_manifest.get("shard_sha256"),
        "tasks_planned": plan_manifest.get("tasks"),
        "shards": shards,
        "totals": totals,
        "budget_guard": budget_summary,
        "billing": {
            "before": billing_before,
            "after": billing_after,
            "delta": (billing_delta(billing_before or {}, billing_after or {})
                      if billing_before and billing_after else
                      {"available": False, "note": "billing snapshots disabled or unavailable"}),
        },
        "download": download_summary,
        "environment": {
            "python": sys.version,
            "platform": sys.platform,
            "host": run_metadata.get("host"),
            "modal_client": run_metadata.get("modal_client"),
        },
        "notes": [
            "TIMEOUT_WALL_CLOCK is right-censored at the frozen 60 s cap; it is not a verdict.",
            "Only infrastructure failures are retried; COMPLETED and TIMEOUT_WALL_CLOCK tasks are never repeated.",
            "The monthly metered-cost difference is an estimate and is not a per-run invoice.",
        ],
    }
    if download_dir.is_dir():
        manifest_path = download_dir / "run_manifest.json"
        atomic_write_json(manifest_path, manifest)
        checksums_path = download_dir / "SHA256SUMS"
        lines = []
        if checksums_path.is_file():
            lines = [line for line in checksums_path.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.endswith("run_manifest.json")]
        lines.append(f"{sha256_file(manifest_path)}  run_manifest.json")
        lines.sort()
        atomic_write_text(checksums_path, "\n".join(lines) + "\n")
    return manifest


def _execute(args, kind: str) -> int:
    modal = _require_modal()
    _apply_runner_env(args, creating_resources=True)
    runner = _load_runner_module()
    worker = load_worker_module()
    run_id = sanitize_run_id(args.run_id) if args.run_id else default_run_id(kind)
    download_dir = Path(args.download_dir) if args.download_dir else (
        ROOT / "work" / f"v6_modal_{kind}_{run_id}"
    )
    if download_dir.exists() and not args.force:
        print(
            f"refusing to overwrite existing evidence directory {download_dir}; "
            "pass --force or choose another --run-id/--download-dir", file=sys.stderr,
        )
        return 2
    source_sha = args.source_sha or git_head()
    billing_before = None if args.no_billing else billing_snapshot(
        modal_cli(args.modal_cli), profile=args.profile)
    if not args.confirm_budget:
        print(
            "refusing to start a paid Modal workload without --confirm-budget "
            "(the Lead passes it only after the budget gate is confirmed)", file=sys.stderr,
        )
        return 2

    error = None
    state: dict = {}
    download_summary = None
    image_object_id = None
    plan_manifest = {}
    run_metadata = {}
    monitor = None

    if kind in {"pilot", "run"}:
        try:
            info = resolve_plan(kind, args)
        except (FileNotFoundError, ValueError) as exc:
            print(f"plan error: {exc}", file=sys.stderr)
            return 2
        plan_shards = [item["shard"] for item in info["plan"]]
        if kind == "pilot":
            if args.pilot_shards is None:
                shard_ids = plan_shards
            else:
                count = int(args.pilot_shards)
                if not 1 <= count <= len(plan_shards):
                    print(f"--pilot-shards must be between 1 and {len(plan_shards)}",
                          file=sys.stderr)
                    return 2
                shard_ids = plan_shards[:count]
        elif args.only_shards:
            requested = [f"{int(part):03d}" for part in args.only_shards.split(",") if part.strip()]
            unknown = [shard for shard in requested if shard not in plan_shards]
            if unknown:
                print(f"--only-shards not in plan: {unknown}", file=sys.stderr)
                return 2
            shard_ids = sorted(set(requested))
        else:
            shard_ids = plan_shards
        staging_info = stage_protocol(
            args, kind, run_id, info, source_sha, info["manifest"],
        )
        run_metadata = staging_info["run_metadata"]
        plan_manifest = staging_info["plan_manifest"]
        attempts = {shard: 0 for shard in shard_ids}
        planned_tasks = sum(len(item["tasks"]) for item in info["plan"]
                            if item["shard"] in set(shard_ids))
        monitor = BudgetMonitor(args, kind=kind, planned_tasks=planned_tasks)
        monitor.start(billing_before)
        with modal.enable_output() if args.verbose else contextlib.nullcontext():
            with runner.app.run():
                image_object_id = _prebuild_image(runner)
                vol = runner.VOLUME
                upload = upload_protocol(vol, staging_info["staging_dir"], args.volume_prefix)
                print(f"uploaded {upload['files']} protocol/run-metadata files to volume",
                      flush=True)
                state, monitor = _run_pool(runner, run_id, shard_ids, attempts, args, monitor)
                vol.reload()
                if not args.no_download:
                    download_summary = _download_volume(
                        vol, args.volume_prefix, run_id, download_dir,
                        include_checkpoints=args.include_checkpoints,
                    )
    else:  # resume
        monitor = BudgetMonitor(args, kind="resume", planned_tasks=0)
        monitor.start(billing_before)
        with modal.enable_output() if args.verbose else contextlib.nullcontext():
            with runner.app.run():
                vol = runner.VOLUME
                run_metadata = _remote_run_metadata(vol, args.volume_prefix, run_id)
                plan_manifest = _remote_manifest(vol, args.volume_prefix)
                shard_ids = _remote_shards_needing_work(vol, args.volume_prefix, plan_manifest)
                if args.only_shards:
                    requested = [f"{int(part):03d}" for part in args.only_shards.split(",") if part.strip()]
                    shard_ids = sorted(set(shard_ids) & set(requested))
                attempts = {shard: _remote_next_attempt(vol, args.volume_prefix, shard)
                            for shard in shard_ids}
                print(f"resume {run_id}: {len(shard_ids)} shard(s) need work", flush=True)
                if shard_ids:
                    state, monitor = _run_pool(runner, run_id, shard_ids, attempts,
                                               args, monitor)
                vol.reload()
                if not args.no_download:
                    download_summary = _download_volume(
                        vol, args.volume_prefix, run_id, download_dir,
                        include_checkpoints=args.include_checkpoints,
                    )

    billing_after = None if args.no_billing else billing_snapshot(
        modal_cli(args.modal_cli), profile=args.profile)
    manifest = write_run_manifest(
        download_dir=download_dir, run_id=run_id, kind=kind,
        run_metadata=run_metadata, plan_manifest=plan_manifest, state=state,
        billing_before=billing_before, billing_after=billing_after,
        image_object_id=image_object_id, download_summary=download_summary,
        error=error, budget_summary=monitor.summary() if monitor is not None else None,
    )
    totals = manifest["totals"]
    attempted = totals["shards_attempted"]
    partial = totals["shards_planned"] and attempted < totals["shards_planned"]
    audit_hint = (
        f"python scripts/audit_v6_modal_results.py --download-dir {download_dir}"
        + (" --allow-partial" if partial else "")
    )
    print(json.dumps({
        "run_id": run_id,
        "kind": kind,
        "download_dir": str(download_dir),
        "totals": totals,
        "billing": manifest["billing"]["delta"],
        "budget_guard": monitor.summary() if monitor is not None else None,
        "run_manifest": str(download_dir / "run_manifest.json"),
        "audit_hint": audit_hint,
    }, indent=2, sort_keys=True))
    if totals["shards_failed"] or totals["shards_incomplete"]:
        return 1
    return 0


# --------------------------------------------------------------------------- #
# status / download (read-only / no containers)
# --------------------------------------------------------------------------- #
def _latest_run_id(vol, prefix: str) -> str | None:
    candidates = []
    for entry in _remote_listdir(vol, f"/{prefix}/run_metadata"):
        name = Path(entry.path).name
        if name.startswith("run_") and name.endswith(".json"):
            candidates.append((int(getattr(entry, "mtime", 0)), name[len("run_"):-len(".json")]))
    return sorted(candidates)[-1][1] if candidates else None


def cmd_status(args) -> int:
    _apply_runner_env(args, creating_resources=False)
    runner = _load_runner_module()
    vol = runner.VOLUME
    vol.reload()
    run_id = args.run_id or _latest_run_id(vol, args.volume_prefix)
    if not run_id:
        print(f"no run metadata found under /{args.volume_prefix}/run_metadata", file=sys.stderr)
        return 2
    metadata = _remote_run_metadata(vol, args.volume_prefix, run_id)
    manifest = _remote_manifest(vol, args.volume_prefix)
    shards = {}
    for shard in sorted(manifest.get("shard_sha256") or {}):
        metrics = {}
        try:
            metrics = _read_remote_json(vol, f"/{args.volume_prefix}/metrics/shard_{shard}.json")
        except Exception:  # noqa: BLE001
            metrics = {}
        raw_files = [entry for entry in _remote_listdir(vol, f"/{args.volume_prefix}/raw/shard_{shard}")
                     if Path(entry.path).suffix == ".json"]
        expected = len((manifest.get("shard_task_ids") or {}).get(shard) or [])
        shards[shard] = {
            "status": metrics.get("status", "NOT_RUN"),
            "attempt": metrics.get("attempt"),
            "raw_records": len(raw_files),
            "tasks_expected": expected,
            "tasks_completed": metrics.get("tasks_completed"),
            "tasks_timeout": metrics.get("tasks_timeout"),
            "tasks_error": metrics.get("tasks_error"),
            "next_attempt": _remote_next_attempt(vol, args.volume_prefix, shard),
            "invocation_id": metrics.get("invocation_id"),
        }
    status_counts = Counter(shard["status"] for shard in shards.values())
    payload = {
        "run_id": run_id,
        "kind": metadata.get("kind"),
        "source_sha": metadata.get("source_sha"),
        "image_spec_sha256": (metadata.get("image") or {}).get("spec_sha256"),
        "shards_planned": len(shards),
        "status_counts": dict(status_counts),
        "raw_records": sum(shard["raw_records"] for shard in shards.values()),
        "tasks_planned": manifest.get("tasks"),
        "started_at": metadata.get("created_at"),
        "shards": shards,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        summary = {key: payload[key] for key in payload if key != "shards"}
        summary["incomplete_shards"] = [
            shard for shard, entry in shards.items()
            if entry["status"] != "COMPLETED"
        ][:50]
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_download(args) -> int:
    _apply_runner_env(args, creating_resources=False)
    runner = _load_runner_module()
    vol = runner.VOLUME
    vol.reload()
    run_id = args.run_id or _latest_run_id(vol, args.volume_prefix)
    if not run_id:
        print(f"no run metadata found under /{args.volume_prefix}/run_metadata", file=sys.stderr)
        return 2
    metadata = _remote_run_metadata(vol, args.volume_prefix, run_id)
    manifest = _remote_manifest(vol, args.volume_prefix)
    download_dir = Path(args.download_dir) if args.download_dir else (
        ROOT / "work" / f"v6_modal_{metadata.get('kind', 'run')}_{run_id}"
    )
    if download_dir.exists() and not args.force:
        print(f"{download_dir} exists; pass --force to overwrite", file=sys.stderr)
        return 2
    summary = _download_volume(
        vol, args.volume_prefix, run_id, download_dir,
        include_checkpoints=args.include_checkpoints,
    )
    run_manifest = write_run_manifest(
        download_dir=download_dir, run_id=run_id,
        kind=metadata.get("kind", "run"), run_metadata=metadata,
        plan_manifest=manifest, state={}, billing_before=None, billing_after=None,
        image_object_id=metadata.get("image_object_id"), download_summary=summary,
    )
    print(json.dumps({
        "run_id": run_id, "download_dir": str(download_dir),
        "files": summary["files"], "bytes": summary["bytes"],
        "tasks_completed": run_manifest["totals"]["tasks_completed"],
        "audit_hint": (
            f"python scripts/audit_v6_modal_results.py --download-dir {download_dir}"
        ),
    }, indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# local simulation (never touches Modal)
# --------------------------------------------------------------------------- #
class SimulationFailure(RuntimeError):
    pass


def _sim_assert(condition, message, checks):
    checks.append({"check": message, "pass": bool(condition)})
    if not condition:
        raise SimulationFailure(message)


def _sim_write_shard_csvs(volume_root: Path, source_rows: list[dict], header: list[str],
                          task_rows: list[dict], shards: int) -> dict:
    """Write the fake frozen protocol tree and return the shard manifest."""
    import csv as _csv
    import io

    protocol = volume_root / "protocol"
    (protocol / "shards").mkdir(parents=True, exist_ok=True)
    (protocol / "source_tasks").mkdir(parents=True, exist_ok=True)
    (protocol / "run_metadata").mkdir(parents=True, exist_ok=True)
    buckets = [[] for _ in range(shards)]
    for index, row in enumerate(task_rows):
        buckets[index % shards].append(row)
    shard_sha = {}
    shard_task_ids = {}
    for shard, rows in enumerate(buckets):
        buffer = io.StringIO()
        writer = _csv.DictWriter(buffer, fieldnames=header, lineterminator="\n",
                                 extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        text = buffer.getvalue()
        (protocol / "shards" / f"shard_{shard:03d}.csv").write_text(text, encoding="utf-8")
        shard_sha[f"{shard:03d}"] = hashlib.sha256(text.encode()).hexdigest()
        shard_task_ids[f"{shard:03d}"] = [row["task_id"] for row in rows]
    buffer = io.StringIO()
    writer = _csv.DictWriter(buffer, fieldnames=header, lineterminator="\n",
                             extrasaction="ignore")
    writer.writeheader()
    writer.writerows(source_rows)
    (protocol / "source_tasks" / "shard_000.csv").write_text(buffer.getvalue(), encoding="utf-8")
    manifest = {
        "schema": "v6-modal-shard-plan-1",
        "kind": "pilot",
        "source": {"task_dir": "protocol/v6_pilot_tasks", "tasks_total_in_source": len(source_rows)},
        "tasks": len(task_rows),
        "shards": shards,
        "tasks_per_shard": len(task_rows) // shards,
        "distinct_model_hashes": len({row.get("expected_model_hash") for row in task_rows}),
        "cell_timeout_s": 60,
        "shard_sha256": shard_sha,
        "shard_task_ids": shard_task_ids,
        "columns": header,
    }
    atomic_write_json(protocol / "modal_shards_manifest.json", manifest)
    return manifest


def _sim_copy_to_download(volume_root: Path, download_dir: Path) -> None:
    if download_dir.exists():
        shutil.rmtree(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    for name in ("protocol", "run_metadata", "raw", "completion_ledger", "hashes",
                 "metrics", "logs"):
        source = volume_root / name
        if source.is_dir():
            shutil.copytree(source, download_dir / name)


def _sim_run_audit(download_dir: Path, out_dir: Path) -> tuple[int, dict, str]:
    process = subprocess.run(
        [sys.executable, str(AUDIT_PATH), "--download-dir", str(download_dir),
         "--out", str(out_dir), "--json"],
        capture_output=True, text=True, timeout=600,
    )
    payload = {}
    for line in process.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
    return process.returncode, payload, (process.stdout + process.stderr)


def _sim_tamper_case(base_download: Path, workdir: Path, name: str, mutate) -> dict:
    target = workdir / "tamper" / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(base_download, target)
    mutate(target)
    code, payload, text = _sim_run_audit(target, workdir / "audit" / f"tamper_{name}")
    return {
        "case": name,
        "audit_exit_code": code,
        "detected": code != 0,
        "errors": (payload.get("audit_errors") or [])[:3],
    }


def _acquire_sim_lock(workdir: Path):
    """Prevent two concurrent simulations from interleaving the same fake Volume."""
    lock_path = workdir.with_name(workdir.name + ".lock")
    for _ in range(2):
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(descriptor, str(os.getpid()).encode())
            return descriptor, lock_path
        except FileExistsError:
            try:
                other = int(lock_path.read_text().strip() or "0")
            except (OSError, ValueError):
                other = 0
            alive = False
            if other:
                try:
                    os.kill(other, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                print(
                    f"another simulation (pid {other}) is using {workdir}; "
                    "wait for it or pass --workdir with a unique path", file=sys.stderr,
                )
                return None, lock_path
            try:
                lock_path.unlink()
            except OSError:
                pass
    return None, lock_path


def _simulate(args) -> int:
    worker = load_worker_module()
    workdir = Path(args.workdir).resolve()
    lock_descriptor, lock_path = _acquire_sim_lock(workdir)
    if lock_descriptor is None:
        return 2
    import atexit
    def _release_lock():
        try:
            os.close(lock_descriptor)
        except OSError:
            pass
        try:
            lock_path.unlink()
        except OSError:
            pass
    atexit.register(_release_lock)
    if workdir.exists() and not args.keep:
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    volume_root = workdir / "vol" / DEFAULT_VOLUME_PREFIX
    volume_root.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or "simulation-0001"
    checks: list[dict] = []

    # The simulation runs on macOS/Linux; Linux containers get RSS from /proc.
    if not Path("/proc").is_dir():
        def _ps_rss_mb(pid):
            try:
                out = subprocess.run(
                    ["ps", "-o", "rss=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                return round(int(out) / 1024.0, 3) if out else None
            except Exception:  # noqa: BLE001
                return None
        worker._process_rss_mb = _ps_rss_mb  # simulation-only sampling fallback

    builder = load_builder_module()
    default_source = PILOT_JSON if PILOT_JSON.is_file() else PILOT_SOURCE
    source_dir = Path(args.source) if args.source else default_source
    all_rows, header, _, _source_meta = builder.load_source_tasks(source_dir)
    # "first few Pilot tasks" + one pre-registered heavy WIP=2 cell so that the
    # timeout/null-semantics path is exercised deterministically.
    ordered = sorted(all_rows, key=lambda row: row["task_id"])
    selected = ordered[: max(2, int(args.tasks) - 1)]
    selected_ids = {row["task_id"] for row in selected}
    heavy = next(
        (row for row in ordered
         if row.get("stratum") == "WIP_2" and row["task_id"] not in selected_ids),
        ordered[-1],
    )
    selected.append(heavy)
    shards = int(args.shards)
    task_rows = selected[: int(args.tasks)]
    manifest = _sim_write_shard_csvs(volume_root, all_rows, header, task_rows, shards)
    run_metadata = {
        "schema": "v6-modal-run-metadata-1",
        "run_id": run_id,
        "kind": "pilot",
        "created_at": utc_now(),
        "source_sha": args.source_sha or f"simulation-{worker.repo_tree_sha256(ROOT)[:16]}",
        "source_tree_dirty": git_dirty(),
        "image_spec_sha256": worker.image_spec_sha256(ROOT),
        "image": {"spec_sha256": worker.image_spec_sha256(ROOT)},
        "shard_sha256": manifest["shard_sha256"],
        "shard_task_ids": manifest["shard_task_ids"],
        "tasks": manifest["tasks"],
        "cell_timeout_s": args.cell_timeout,
        "cell_timeout_override_s": args.cell_timeout,
        "function_timeout_s": 5400,
        "cpu": 1.0,
        "memory_mib": 4096,
        "scg_cache_dir": str(workdir / "scg_cache"),
        "max_parallel": shards,
        "app_name": "local-simulation",
        "volume_name": "local-simulation",
        "volume_prefix": DEFAULT_VOLUME_PREFIX,
        "executor": "local-simulation",
    }
    atomic_write_json(volume_root / "run_metadata" / f"run_{run_id}.json", run_metadata)
    config = dict(run_metadata)

    def run_shard(shard: str, attempt: int) -> dict:
        return worker.execute_shard(
            shard, run_id, attempt,
            volume_root=volume_root, repo_root=ROOT, config=config,
            invocation_id=f"local-sim-{run_id}-{shard}-a{attempt}",
            log=lambda message: print(f"[sim {shard}] {message}", flush=True),
        )

    metrics = {}
    for shard in [f"{index:03d}" for index in range(shards)]:
        metrics[shard] = run_shard(shard, 0)
    try:
        _sim_assert(all(Path(volume_root / "metrics" / f"shard_{shard}.json").is_file()
                        for shard in metrics), "shard metrics written", checks)
        total_records = sum(len(list((volume_root / "raw" / f"shard_{shard}").glob("*.json")))
                            for shard in metrics)
        _sim_assert(total_records == len(task_rows),
                    f"one raw record per task ({total_records}/{len(task_rows)})", checks)
        stale_tmp = list((volume_root).rglob("*.tmp*"))
        _sim_assert(not stale_tmp, "no partially written tmp files remain", checks)
        # SHA / ledger / hash reconciliation (hashes over the exact file bytes)
        ledger_entries = 0
        for shard in metrics:
            ledger_path = volume_root / "completion_ledger" / f"shard_{shard}.jsonl"
            hash_path = volume_root / "hashes" / f"shard_{shard}.jsonl"
            raw_files = sorted((volume_root / "raw" / f"shard_{shard}").glob("*.json"))
            entries = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
            ledger_entries += len(entries)
            _sim_assert(len(raw_files) == len(manifest["shard_task_ids"][shard]),
                        f"shard {shard} has one raw record per frozen task", checks)
            _sim_assert(len(entries) == len(raw_files),
                        f"shard {shard} ledger has one entry per final task", checks)
            per_task = Counter(entry["task_id"] for entry in entries)
            duplicates = sorted(task for task, count in per_task.items() if count > 1)
            _sim_assert(not duplicates,
                        f"shard {shard} ledger has no duplicate task entries", checks)
            for entry in entries:
                raw_path = volume_root / entry["raw_path"]
                actual = sha256_file(raw_path)  # recomputed from the file bytes
                if actual != entry["sha256"]:
                    raise SimulationFailure(
                        f"ledger sha mismatch for {entry['task_id']} "
                        f"(file {actual}, ledger {entry['sha256']})"
                    )
                if int(entry.get("bytes", -1)) != raw_path.stat().st_size:
                    raise SimulationFailure(f"ledger byte count mismatch for {entry['task_id']}")
            hash_entries = [json.loads(line) for line in hash_path.read_text().splitlines() if line.strip()]
            _sim_assert(len(hash_entries) == len(entries), f"shard {shard} hash manifest matches ledger", checks)
        classification = Counter()
        for shard in metrics:
            for path in sorted((volume_root / "raw" / f"shard_{shard}").glob("*.json")):
                record = json.loads(path.read_text())
                payload = {key: value for key, value in record.items() if key != "raw_record_hash"}
                actual = hashlib.sha256(
                    json.dumps(payload, default=str, sort_keys=True).encode()
                ).hexdigest()
                if actual != record["raw_record_hash"]:
                    raise SimulationFailure(f"raw_record_hash mismatch for {path.name}")
                classification[record["execution_status"]] += 1
                if record["execution_status"] == "TIMEOUT_WALL_CLOCK":
                    if record["verdict"] is not None:
                        raise SimulationFailure("timeout record carries a verdict")
                if record["execution_status"] == "COMPLETED" and record["verdict"] not in {"VER", "AMB", "INC"}:
                    raise SimulationFailure("completed record has invalid verdict")
        _sim_assert(classification["TIMEOUT_WALL_CLOCK"] >= 1,
                    f"timeout path exercised ({classification['TIMEOUT_WALL_CLOCK']} timeout records)", checks)

        # dedupe: re-running a finished shard executes nothing
        ledger_before = (volume_root / "completion_ledger" / "shard_001.jsonl").read_text()
        metrics_again = run_shard("001", 1)
        _sim_assert(metrics_again["tasks_executed"] == 0,
                    "re-running a finished shard executes 0 tasks", checks)
        _sim_assert(metrics_again["tasks_skipped_resumed"] == len(manifest["shard_task_ids"]["001"]),
                    "re-running a finished shard skips every task", checks)
        _sim_assert((volume_root / "completion_ledger" / "shard_001.jsonl").read_text() == ledger_before,
                    "re-running a finished shard appends no ledger entry", checks)

        # resume: remove one raw record + its ledger line (uncommitted task)
        shard_zero = "000"
        victim = manifest["shard_task_ids"][shard_zero][0]
        raw_victim = volume_root / "raw" / "shard_000" / f"{victim}.json"
        raw_victim.unlink()
        ledger_path = volume_root / "completion_ledger" / "shard_000.jsonl"
        lines = [line for line in ledger_path.read_text().splitlines()
                 if json.loads(line)["task_id"] != victim]
        ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        resumed = run_shard(shard_zero, 2)
        _sim_assert(resumed["tasks_executed"] == 1,
                    "resume re-executes only the missing task", checks)
        _sim_assert(resumed["tasks_skipped_resumed"] == len(manifest["shard_task_ids"][shard_zero]) - 1,
                    "resume skips every already-final task", checks)
        _sim_assert(raw_victim.is_file(), "resume restores the missing raw record", checks)
        resumed_lines = [json.loads(line) for line in ledger_path.read_text().splitlines()
                         if line.strip()]
        _sim_assert(len(resumed_lines) == len(manifest["shard_task_ids"][shard_zero]),
                    "resumed shard ledger has no duplicate entries", checks)

        # download + passing audit
        download_dir = workdir / "download"
        _sim_copy_to_download(volume_root, download_dir)
        shard_states = {}
        for shard in metrics:
            metrics_path = download_dir / "metrics" / f"shard_{shard}.json"
            shard_states[shard] = {
                "status": json.loads(metrics_path.read_text())["status"],
                "attempts": [{"attempt": 0, "call_id": f"local-sim-{shard}"}],
            }
        write_run_manifest(
            download_dir=download_dir, run_id=run_id, kind="pilot",
            run_metadata=run_metadata, plan_manifest=manifest, state=shard_states,
            billing_before=None, billing_after=None,
            image_object_id="local-simulation", download_summary=None,
        )
        code, payload, text = _sim_run_audit(download_dir, workdir / "audit" / "ok")
        _sim_assert(code == 0, f"audit passes on the clean simulation ({code})", checks)

        # tamper detection
        def remove_record(target):
            (target / "raw" / "shard_000" / f"{victim}.json").unlink()

        def duplicate_ledger_line(target):
            path = target / "completion_ledger" / "shard_000.jsonl"
            lines = [line for line in path.read_text().splitlines() if line.strip()]
            lines.append(lines[0])
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        def corrupt_record(target):
            path = target / "raw" / "shard_001" / f"{manifest['shard_task_ids']['001'][0]}.json"
            record = json.loads(path.read_text())
            record["wall_s"] = float(record.get("wall_s") or 1.0) + 1.0
            path.write_text(json.dumps(record, default=str, sort_keys=True) + "\n", encoding="utf-8")

        def duplicate_final_entry(target):
            path = target / "completion_ledger" / "shard_001.jsonl"
            lines = [line for line in path.read_text().splitlines() if line.strip()]
            entry = json.loads(lines[0])
            entry["attempt"] = int(entry.get("attempt", 0)) + 7
            lines.append(json.dumps(entry, sort_keys=True))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        tamper_results = [
            _sim_tamper_case(download_dir, workdir, "missing_record", remove_record),
            _sim_tamper_case(download_dir, workdir, "duplicate_ledger", duplicate_ledger_line),
            _sim_tamper_case(download_dir, workdir, "corrupt_record_hash", corrupt_record),
            _sim_tamper_case(download_dir, workdir, "duplicate_final_entry", duplicate_final_entry),
        ]
        for result in tamper_results:
            _sim_assert(result["detected"],
                        f"audit detects planted fault: {result['case']} "
                        f"(exit {result['audit_exit_code']})", checks)

        report = {
            "schema": "v6-modal-simulation-report-1",
            "PASS": True,
            "run_id": run_id,
            "workdir": str(workdir),
            "cells": len(task_rows),
            "shards": shards,
            "cell_timeout_s": args.cell_timeout,
            "classification": dict(classification),
            "metrics": {shard: {key: metrics[shard].get(key) for key in (
                "status", "tasks_total", "tasks_executed", "tasks_skipped_resumed",
                "tasks_timeout", "tasks_error", "wall_s")} for shard in metrics},
            "resume": {
                "task_removed": victim,
                "executed_after_resume": resumed["tasks_executed"],
                "skipped_after_resume": resumed["tasks_skipped_resumed"],
            },
            "audit_clean": {"exit_code": code, "PASS": payload.get("PASS"),
                            "tasks_audited": payload.get("tasks_audited")},
            "tamper_cases": tamper_results,
            "checks": checks,
        }
        atomic_write_json(workdir / "simulation_report.json", report)
        print(json.dumps({key: report[key] for key in (
            "PASS", "cells", "shards", "classification", "audit_clean", "tamper_cases")},
            indent=2, sort_keys=True))
        return 0
    except SimulationFailure as exc:
        report = {"schema": "v6-modal-simulation-report-1", "PASS": False,
                  "error": str(exc), "checks": checks}
        atomic_write_json(workdir / "simulation_report.json", report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _add_plan_args(parser):
    parser.add_argument("--source", type=Path, default=None,
                        help="frozen task directory or JSON task list "
                             "(default: protocol/v6_modal_pilot_tasks.json for Pilot, "
                             "protocol/v6_formal_tasks for formal)")
    parser.add_argument("--source-manifest", type=Path, default=None)
    parser.add_argument("--pilot-manifest", type=Path, default=None,
                        help="frozen Pilot JSON task list (default protocol/v6_modal_pilot_tasks.json)")
    parser.add_argument("--shard-manifest", type=Path, default=None,
                        help="use a previously frozen modal shard manifest instead of rebuilding")
    parser.add_argument("--shard-dir", type=Path, default=None)
    parser.add_argument("--shards", type=int, default=None)
    parser.add_argument("--tasks-per-shard", type=int, default=None)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--formal", action="store_true")


def _add_resource_args(parser):
    parser.add_argument("--cpu", type=float, default=DEFAULT_CPU)
    parser.add_argument("--memory-mib", type=int, default=DEFAULT_MEMORY_MIB)
    parser.add_argument("--function-timeout", type=int, default=DEFAULT_FUNCTION_TIMEOUT_S)
    parser.add_argument("--cell-timeout", type=float, default=DEFAULT_CELL_TIMEOUT_S)
    parser.add_argument("--max-parallel", type=int, default=8)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-infra-retries", type=int, default=2)
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME)
    parser.add_argument("--volume-name", default=DEFAULT_VOLUME_NAME)
    parser.add_argument("--volume-prefix", default=DEFAULT_VOLUME_PREFIX)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--modal-cli", default=None)
    parser.add_argument("--modal-python", default=None,
                        help="interpreter with the Modal SDK (or set V6_MODAL_PYTHON)")
    parser.add_argument("--download-dir", type=Path, default=None)
    parser.add_argument("--staging-dir", type=Path, default=None)
    parser.add_argument("--include-checkpoints", action="store_true")
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--no-billing", action="store_true")
    parser.add_argument("--budget-state", type=Path, default=None,
                        help="budget state JSON (default work/v6_modal_budget/budget.json)")
    parser.add_argument("--budget-cap-usd", type=float, default=10.0)
    parser.add_argument("--budget-threshold-usd", type=float, default=9.5,
                        help="stop spawning new shards above this projected/spent cost")
    parser.add_argument("--billing-check-seconds", type=float, default=300.0)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--confirm-budget", action="store_true",
                        help="required before spawning any paid Modal container")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="print the execution plan; creates nothing")
    _add_plan_args(plan)
    _add_resource_args(plan)
    plan.add_argument("--dry-run", action="store_true", default=True,
                      help="accepted for symmetry; plan is always read-only")
    plan.add_argument("--expected-cell-seconds", type=float, default=18.8,
                      help="expected mean cell wall time used for the cost estimate")
    plan.add_argument("--record-bytes", type=int, default=6000,
                      help="estimated raw JSON bytes per record (storage estimate only)")
    plan.set_defaults(func=cmd_plan)

    pilot = subparsers.add_parser("pilot", help="run the pre-registered Pilot on Modal")
    _add_plan_args(pilot)
    _add_resource_args(pilot)
    pilot.add_argument("--pilot-shards", type=int, default=None,
                       help="limit the run to the first N frozen Pilot shards (default: all)")
    pilot.add_argument("--run-id", default=None)
    pilot.add_argument("--source-sha", default=None)
    pilot.add_argument("--force", action="store_true")
    pilot.set_defaults(func=lambda args: _execute(args, "pilot"))

    run = subparsers.add_parser("run", help="run the formal 8,280 matrix on Modal")
    _add_plan_args(run)
    _add_resource_args(run)
    run.add_argument("--pilot-shards", type=int, default=None, help=argparse.SUPPRESS)
    run.add_argument("--run-id", default=None)
    run.add_argument("--source-sha", default=None)
    run.add_argument("--only-shards", default=None)
    run.add_argument("--force", action="store_true")
    run.set_defaults(func=lambda args: _execute(args, "run"))

    resume = subparsers.add_parser("resume", help="re-spawn only incomplete shards")
    _add_plan_args(resume)
    _add_resource_args(resume)
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--source-sha", default=None)
    resume.add_argument("--only-shards", default=None)
    resume.add_argument("--force", action="store_true")
    resume.set_defaults(func=lambda args: _execute(args, "resume"))

    status = subparsers.add_parser("status", help="read remote run state (no containers)")
    _add_resource_args(status)
    status.add_argument("--run-id", default=None)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    download = subparsers.add_parser("download", help="download a run from the volume")
    _add_resource_args(download)
    download.add_argument("--run-id", default=None)
    download.add_argument("--force", action="store_true")
    download.set_defaults(func=cmd_download)

    simulate = subparsers.add_parser(
        "simulate", help="free local end-to-end self-test (fake Volume, no Modal calls)")
    simulate.add_argument("--workdir", type=Path, default="/tmp/v6_modal_simulation")
    simulate.add_argument("--source", type=Path, default=None)
    simulate.add_argument("--source-sha", default=None)
    simulate.add_argument("--run-id", default="simulation-0001")
    simulate.add_argument("--tasks", type=int, default=12)
    simulate.add_argument("--shards", type=int, default=2)
    simulate.add_argument("--cell-timeout", type=float, default=3.0)
    simulate.add_argument("--keep", action="store_true")
    simulate.set_defaults(func=_simulate)
    return parser


def _maybe_reexec(argv) -> None:
    """Re-exec with an interpreter that has the Modal SDK (paid commands only)."""
    if "--local-simulate" in argv:
        return
    command = next((part for part in argv if not part.startswith("-")), None)
    if command not in COMMANDS_NEEDING_MODAL:
        return
    try:
        import importlib.util as _iu
        if _iu.find_spec("modal") is not None:
            return
    except Exception:  # noqa: BLE001
        pass
    interpreter = os.environ.get("V6_MODAL_PYTHON") or modal_python()
    if interpreter and os.environ.get("V6_MODAL_REEXEC") != "1":
        os.environ["V6_MODAL_REEXEC"] = "1"
        print(f"note: re-executing with the Modal client interpreter {interpreter}",
              file=sys.stderr)
        os.execv(interpreter, [interpreter, str(Path(__file__).resolve()), *argv])


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--local-simulate":
        argv[0] = "simulate"
    _maybe_reexec(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
