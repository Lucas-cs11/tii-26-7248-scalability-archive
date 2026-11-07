#!/usr/bin/env python3
"""Build the Modal shard plan for the v6 scalability benchmark.

Reads the *frozen* task CSVs (default: ``protocol/v6_formal_tasks`` when it
exists, otherwise the frozen Pilot ``protocol/v6_pilot_tasks``), re-partitions
them so that every task of the same ``expected_model_hash`` lands in the same
shard (this is what makes the per-container SCG cache reuse effective), and
writes a shard manifest plus ``shard_XXX.csv`` files with SHA-256 digests.

The task set, row order principle and per-task parameters are never changed:
a shard is only a different grouping of the same frozen rows.  No verdict,
runtime or failure stage is read.  ``--dry-run`` computes and prints the plan
without writing anything; it never touches Modal.

Outputs (formal defaults)::

    protocol/v6_modal_shards_manifest.json
    protocol/v6_modal_shards/shard_000.csv ... shard_119.csv
    protocol/v6_modal_shards/SHA256SUMS

Pilot mode (``--pilot``, 96 = 4 shards x 24 tasks, a deterministic subset of
the frozen 288-cell Pilot) writes ``protocol/v6_modal_pilot_shards*``.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORMAL_SOURCE = ROOT / "protocol" / "v6_formal_tasks"
FORMAL_MANIFEST = ROOT / "protocol" / "v6_formal_manifest.json"
PILOT_SOURCE = ROOT / "protocol" / "v6_pilot_tasks"
PILOT_MANIFEST = ROOT / "protocol" / "v6_pilot_manifest.json"
#: Frozen Modal Pilot task list produced by the Lead: a JSON document with a
#: ``cells_detail`` list (64 pre-registered cells) and an explicit
#: ``shard_plan``.  Preferred over the older GitHub Pilot CSV directory.
PILOT_JSON = ROOT / "protocol" / "v6_modal_pilot_tasks.json"

FORMAL_OUT_DIR = ROOT / "protocol" / "v6_modal_shards"
FORMAL_OUT_MANIFEST = ROOT / "protocol" / "v6_modal_shards_manifest.json"
PILOT_OUT_DIR = ROOT / "protocol" / "v6_modal_pilot_shards"
PILOT_OUT_MANIFEST = ROOT / "protocol" / "v6_modal_pilot_shards_manifest.json"

SCHEMA = "v6-modal-shard-plan-1"
CELL_TIMEOUT_S = 60
FUNCTION_TIMEOUT_S = 5400
DEFAULT_CPU = 1.0
DEFAULT_MEMORY_MIB = 4096
FORMAL_SHARDS = 120
FORMAL_TASKS = 8280
PILOT_SHARDS = 6
PILOT_TASKS_PER_SHARD = None  # derived from the frozen Pilot shard plan / task count

STANDARD_FIELDS = (
    "task_id", "protocol_version", "experiment_id", "experiment",
    "benchmark_family", "stratum", "base_model_id", "model_id",
    "bounded_problem_id", "configuration_id", "seed", "replicate", "L", "H",
    "mode", "timeout_s", "expected_model_hash", "expected_structure_hash",
    "params",
)
REQUIRED_FIELDS = (
    "task_id", "protocol_version", "experiment", "benchmark_family", "model_id",
    "bounded_problem_id", "configuration_id", "seed", "replicate", "L", "H",
    "mode", "timeout_s", "expected_model_hash", "expected_structure_hash",
    "params",
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def canonical_json(value) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def git_head(root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - provenance must not fail the build
        return "unknown"


def git_dirty(root: Path):
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        return bool(out.strip())
    except Exception:  # noqa: BLE001
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def task_model_hash(row: dict) -> str:
    return row.get("expected_model_hash") or row.get("model_id") or row["task_id"]


def task_sort_key(row: dict):
    return (
        row.get("task_id", ""),
        int(row.get("L", 0) or 0),
        float(row.get("H", 0) or 0),
        row.get("mode", ""),
    )


def _row_order_key(row: dict):
    return (
        int(row.get("L", 0) or 0),
        float(row.get("H", 0) or 0),
        row.get("mode", ""),
        row.get("task_id", ""),
    )


# --------------------------------------------------------------------------- #
# source loading and validation
# --------------------------------------------------------------------------- #
def _stable_id(prefix: str, payload: dict) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _adapt_json_task(item: dict, index: int) -> dict:
    """Normalise one ``cells_detail`` entry into the frozen CSV row schema."""
    if not isinstance(item, dict):
        raise ValueError(f"JSON task #{index} is not an object")
    params = item.get("params") or {}
    if isinstance(params, str):
        params = json.loads(params)
    params = dict(params)
    model_hash = item.get("model_hash") or item.get("expected_model_hash")
    if not model_hash:
        raise ValueError(f"JSON task #{index} lacks model_hash")
    family = item.get("benchmark_family")
    if not family:
        raise ValueError(f"JSON task #{index} lacks benchmark_family")
    experiment = item.get("experiment")
    if not experiment:
        raise ValueError(f"JSON task #{index} lacks experiment")
    protocol = item.get("protocol") or item.get("protocol_version") or "v6-modal-pilot-1"
    task_id = (item.get("source_task_id") or item.get("task_id")
               or item.get("pilot_task_id"))
    if not task_id:
        raise ValueError(f"JSON task #{index} lacks a task id")
    L = int(item["L"])
    H = float(item["H"])
    seed = item.get("seed", params.get("seed", 0))
    model_id = item.get("model_id") or f"v6:{family}:{model_hash[:20]}"
    bounded = item.get("bounded_problem_id") or _stable_id(
        "bp6", {"protocol": protocol, "model_hash": model_hash, "L": L, "H": H}
    )
    configuration = item.get("configuration_id") or task_id
    row = {
        "task_id": str(task_id),
        "protocol_version": str(protocol),
        "experiment_id": item.get("experiment_id") or f"{protocol}-{experiment}",
        "experiment": str(experiment),
        "benchmark_family": str(family),
        "stratum": item.get("stratum"),
        "base_model_id": item.get("base_model_id") or model_id,
        "model_id": model_id,
        "bounded_problem_id": bounded,
        "configuration_id": configuration,
        "seed": str(int(seed)),
        "replicate": str(int(item.get("replicate", 0))),
        "L": str(L),
        "H": str(H),
        "mode": str(item["mode"]),
        "timeout_s": str(int(item.get("timeout_s", CELL_TIMEOUT_S))),
        "expected_model_hash": str(model_hash),
        "expected_structure_hash": str(item.get("expected_structure_hash") or ""),
        "params": json.dumps(params, sort_keys=True),
    }
    reference = item.get("reference") or {}
    if reference:
        row["reference_verdict"] = reference.get("verdict")
        row["reference_execution_status"] = reference.get("execution_status")
        row["reference_wall_s"] = reference.get("wall_s")
        row["reference_run"] = item.get("reference_run")
        row["reference_pilot_task_id"] = item.get("pilot_task_id")
    return row


def _load_json_source(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        source_meta = {}
        raw_tasks = payload
    elif isinstance(payload, dict):
        source_meta = {
            key: value for key, value in payload.items()
            if key not in {"cells_detail", "tasks", "cells"}
        }
        raw_tasks = (payload.get("cells_detail") or payload.get("tasks")
                     or payload.get("cells"))
    else:
        raise ValueError(f"unsupported JSON task source shape in {path}")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError(f"JSON task source has no task list: {path}")
    rows = [_adapt_json_task(item, index) for index, item in enumerate(raw_tasks)]
    extra = sorted({key for row in rows for key in row if key not in STANDARD_FIELDS})
    header = [field for field in STANDARD_FIELDS if any(field in row for row in rows)]
    header.extend(extra)
    return rows, header, {path.name: sha256_file(path)}, {
        "path": str(path),
        "json": source_meta,
        "shard_plan": source_meta.get("shard_plan"),
        "tasks_total_in_source": len(rows),
    }


def load_source_tasks(source_dir: Path):
    """Load a frozen task source: a directory of ``shard_*.csv`` files or a JSON list.

    Returns ``(rows, header, source_files, source_meta)``.  ``rows`` preserve
    the frozen column values verbatim (JSON tasks are normalised to the same
    CSV schema; extra provenance columns are carried through untouched).
    """
    source = Path(source_dir)
    if source.is_file() and source.suffix.lower() == ".json":
        return _load_json_source(source)
    paths = sorted(source.glob("shard_*.csv"))
    if not paths:
        raise FileNotFoundError(f"no frozen shard_*.csv files in {source}")
    rows: list[dict] = []
    header = None
    source_files = {}
    for path in paths:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            this_header = list(reader.fieldnames or [])
            if header is None:
                header = this_header
            elif this_header != header:
                raise ValueError(f"inconsistent CSV header in {path}")
            file_rows = list(reader)
            rows.extend(file_rows)
        source_files[path.name] = sha256_file(path)
    assert header is not None
    missing = [field for field in REQUIRED_FIELDS if field not in header]
    if missing:
        raise ValueError(f"frozen task CSV is missing required columns: {missing}")
    seen = set()
    duplicates = []
    for row in rows:
        task_id = row["task_id"]
        if task_id in seen:
            duplicates.append(task_id)
        seen.add(task_id)
    if duplicates:
        raise ValueError(f"duplicate task_id in frozen source: {duplicates[:10]}")
    for row in rows:
        json.loads(row["params"])
        int(row["seed"]), int(row["replicate"]), int(row["L"]), float(row["H"])
        if row["mode"] not in {"first", "complete"}:
            raise ValueError(f"{row['task_id']}: unexpected mode {row['mode']!r}")
        if not row.get("expected_model_hash"):
            raise ValueError(f"{row['task_id']}: missing expected_model_hash")
    return rows, header, source_files, {"path": str(source), "json": None,
                                        "shard_plan": None,
                                        "tasks_total_in_source": len(rows)}


def plan_from_frozen_shard_plan(shard_plan, rows):
    """Use the frozen Pilot shard plan verbatim (normalise ``pilot_000`` ids)."""
    import re
    row_by_id = {row["task_id"]: row for row in rows}
    plan = []
    seen = set()
    for item in shard_plan:
        raw_id = str(item.get("shard_id", len(plan)))
        match = re.fullmatch(r"[A-Za-z]+_?(\d+)", raw_id)
        if match:
            shard = f"{int(match.group(1)):03d}"
        elif raw_id.isdigit():
            shard = f"{int(raw_id):03d}"
        else:
            raise ValueError(f"unsupported frozen shard id {raw_id!r}")
        if shard in {entry["shard"] for entry in plan}:
            raise ValueError(f"duplicate frozen shard id {raw_id!r}")
        tasks = []
        for planned in item.get("tasks") or []:
            task_id = (planned.get("source_task_id") or planned.get("task_id")
                       or planned.get("pilot_task_id"))
            row = row_by_id.get(task_id)
            if row is None:
                raise ValueError(f"frozen shard plan references unknown task {task_id!r}")
            if task_id in seen:
                raise ValueError(f"frozen shard plan repeats task {task_id!r}")
            seen.add(task_id)
            tasks.append(row)
        if not tasks:
            raise ValueError(f"frozen shard {raw_id!r} has no tasks")
        plan.append({"shard": shard, "tasks": tasks, "source_shard_id": raw_id})
    if len(seen) != len(rows):
        raise ValueError(
            f"frozen shard plan covers {len(seen)} of {len(rows)} frozen tasks"
        )
    return plan


# --------------------------------------------------------------------------- #
# model-hash clustering
# --------------------------------------------------------------------------- #
def cluster_by_model(rows):
    """Group rows by model hash; every cluster is executed inside one shard."""
    clusters: dict[str, list[dict]] = {}
    for row in rows:
        clusters.setdefault(task_model_hash(row), []).append(row)
    for key, cluster in clusters.items():
        cluster.sort(key=_row_order_key)
    return clusters


def select_tasks(rows, capacity: int):
    """Deterministic pre-registered subset when the source is larger than the plan.

    The rule is structural only: sort by ``task_id`` (a hash-derived identity),
    then keep the first ``capacity`` rows.  No run data is read.
    """
    ordered = sorted(rows, key=lambda row: row["task_id"])
    return ordered[:capacity]


def pack_clusters(clusters: dict[str, list[dict]], shards: int, tasks_per_shard: int):
    """Deterministic first-fit-decreasing packing of model clusters.

    Multi-task clusters are packed first (biggest first).  Because the plan is
    always sized so ``shards * tasks_per_shard == number of selected tasks``,
    the remaining capacity is then filled exactly with single-task clusters
    ("singles"), so every shard has exactly ``tasks_per_shard`` tasks and every
    model hash stays inside one shard.
    """
    capacity = tasks_per_shard
    multi = [cluster for cluster in clusters.values() if len(cluster) > 1]
    singles = [row for cluster in clusters.values() if len(cluster) == 1 for row in cluster]
    too_big = [cluster for cluster in multi if len(cluster) > capacity]
    if too_big:
        raise ValueError(
            "a model-hash cluster is larger than tasks_per_shard "
            f"({len(too_big[0])} > {capacity}); increase --tasks-per-shard"
        )
    bins: list[dict] = []  # {"clusters": [cluster, ...], "size": int}
    for cluster in sorted(multi, key=lambda c: (-len(c), task_sort_key(c[0]))):
        fits = [(capacity - item["size"], index) for index, item in enumerate(bins)
                if capacity - item["size"] >= len(cluster)]
        if fits:
            _, index = min(fits)
            bins[index]["clusters"].append(cluster)
            bins[index]["size"] += len(cluster)
        else:
            bins.append({"clusters": [cluster], "size": len(cluster)})
    if len(bins) > shards:
        raise ValueError(
            f"model-hash clustering needs more than {shards} shards "
            f"({len(bins)} non-empty bins); increase --shards"
        )
    while len(bins) < shards:
        bins.append({"clusters": [], "size": 0})
    expected_remaining = shards * capacity - sum(item["size"] for item in bins)
    if expected_remaining != len(singles):
        raise ValueError(
            "internal packing mismatch: capacity for singles "
            f"{expected_remaining} != {len(singles)} single-task models"
        )
    cursor = 0
    for item in bins:  # bins are filled in order; total capacity is exact
        take = capacity - item["size"]
        for row in singles[cursor:cursor + take]:
            item["clusters"].append([row])
        item["size"] += take
        cursor += take
    assert cursor == len(singles)
    # Shard ids are assigned by the smallest task id for a stable, reproducible order.
    bins.sort(key=lambda item: min(row["task_id"] for cluster in item["clusters"] for row in cluster))
    plan = []
    for index, item in enumerate(bins):
        tasks = [row for cluster in item["clusters"] for row in cluster]
        tasks.sort(key=_row_order_key)
        plan.append({"shard": f"{index:03d}", "tasks": tasks})
    for item in plan:
        if len(item["tasks"]) != tasks_per_shard:
            raise ValueError(
                f"shard {item['shard']} has {len(item['tasks'])} tasks, "
                f"expected {tasks_per_shard}"
            )
    return plan


# --------------------------------------------------------------------------- #
# plan construction
# --------------------------------------------------------------------------- #
def plan_summary(plan, rows, header, source_dir, source_files, *, kind,
                 source_manifest, source_sha=None):
    task_ids = [row["task_id"] for item in plan for row in item["tasks"]]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("duplicate task ids across the generated shards")
    model_hashes = {task_model_hash(row) for item in plan for row in item["tasks"]}
    experiments: dict[str, int] = {}
    for item in plan:
        for row in item["tasks"]:
            experiments[row["experiment"]] = experiments.get(row["experiment"], 0) + 1
    return {
        "schema": SCHEMA,
        "kind": kind,
        "generated_at": utc_now(),
        "generator": "scripts/build_v6_modal_shards.py",
        "source_sha": source_sha,
        "source": {
            "task_dir": str(Path(source_dir).relative_to(ROOT)) if Path(source_dir).is_relative_to(ROOT) else str(source_dir),
            "manifest": (
                str(Path(source_manifest).relative_to(ROOT))
                if source_manifest and Path(source_manifest).is_relative_to(ROOT)
                else (str(source_manifest) if source_manifest else None)
            ),
            "manifest_sha256": (
                sha256_file(Path(source_manifest)) if source_manifest and Path(source_manifest).is_file() else None
            ),
            "task_files": source_files,
            "tasks_total_in_source": len(rows),
        },
        "selection_uses_run_data": False,
        "selection_rule": (
            "all frozen tasks are used (no selection)" if len(rows) == len(task_ids)
            else "frozen tasks sorted by task_id; the first tasks_per_shard*shards rows are used"
        ),
        "clustering_rule": (
            "tasks with the same expected_model_hash are colocated in one shard; "
            "multi-task clusters are packed first-fit-decreasing; single-task "
            "models fill the remaining capacity; execution order inside a shard "
            "keeps every model cluster contiguous and orders L, H, mode, task_id"
        ),
        "tasks": len(task_ids),
        "shards": len(plan),
        "tasks_per_shard": (
            len(plan[0]["tasks"])
            if len({len(item["tasks"]) for item in plan}) == 1 else None
        ),
        "shard_task_counts": {item["shard"]: len(item["tasks"]) for item in plan},
        "distinct_model_hashes": len(model_hashes),
        "distinct_bounded_problems": len({row["bounded_problem_id"] for item in plan for row in item["tasks"]}),
        "experiments": dict(sorted(experiments.items())),
        "cell_timeout_s": CELL_TIMEOUT_S,
        "function_timeout_s": FUNCTION_TIMEOUT_S,
        "cpu": DEFAULT_CPU,
        "memory_mib": DEFAULT_MEMORY_MIB,
        "columns": list(header),
        "warnings": [],
    }


def write_plan(plan, header, out_dir: Path, out_manifest: Path, manifest: dict):
    out_dir = Path(out_dir)
    out_manifest = Path(out_manifest)
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_names = {f"shard_{item['shard']}.csv" for item in plan}
    for stale in out_dir.glob("shard_*.csv"):
        if stale.name not in expected_names:
            stale.unlink()
    shard_sha256: dict[str, str] = {}
    shard_task_ids: dict[str, list[str]] = {}
    shard_model_hashes: dict[str, list[str]] = {}
    for item in plan:
        path = out_dir / f"shard_{item['shard']}.csv"
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer, fieldnames=list(header), lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(item["tasks"])
        text = buffer.getvalue()
        path.write_text(text, encoding="utf-8")
        shard_sha256[item["shard"]] = sha256_bytes(text.encode("utf-8"))
        shard_task_ids[item["shard"]] = [row["task_id"] for row in item["tasks"]]
        shard_model_hashes[item["shard"]] = sorted({task_model_hash(row) for row in item["tasks"]})
    checksums = "".join(
        f"{shard_sha256[item['shard']]}  shard_{item['shard']}.csv\n" for item in plan
    )
    (out_dir / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    manifest = dict(manifest)
    manifest.update({
        "shard_sha256": shard_sha256,
        "shard_task_ids": shard_task_ids,
        "shard_model_hashes": shard_model_hashes,
        "shard_files": sorted(shard_sha256),
        "checksums_file": "SHA256SUMS",
        "checksums_sha256": sha256_bytes(checksums.encode("utf-8")),
    })
    out_manifest.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return manifest


def build(source_dir: Path, *, kind: str, shards: int, tasks_per_shard: int | None,
          source_manifest: Path | None, source_sha: str | None = None):
    rows, header, source_files, source_meta = load_source_tasks(source_dir)
    source_manifest = source_manifest or (
        Path(source_meta["path"]) if source_meta.get("path") else None
    )
    if source_meta.get("shard_plan"):
        # A frozen Pilot shard plan is authoritative: use it verbatim instead
        # of re-packing (this is the pre-registered 48+16 cell pilot layout).
        plan = plan_from_frozen_shard_plan(source_meta["shard_plan"], rows)
        manifest = plan_summary(
            plan, rows, header, source_dir, source_files,
            kind=kind, source_manifest=source_manifest,
            source_sha=source_sha or git_head(ROOT),
        )
        manifest["clustering_rule"] = (
            "frozen Pilot shard plan used verbatim; tasks that share a model are "
            "already colocated by the frozen plan"
        )
        manifest["source_tree_dirty"] = git_dirty(ROOT)
        return plan, header, manifest, rows
    if tasks_per_shard is None:
        # The default layout for a fully frozen matrix is "all tasks, evenly
        # split": the shard count must divide the frozen task total.
        tasks_per_shard = math.ceil(len(rows) / shards) if shards else 0
        if shards * tasks_per_shard != len(rows):
            raise ValueError(
                f"--shards {shards} does not divide the frozen task total {len(rows)}; "
                "pass --tasks-per-shard explicitly to build a partial plan"
            )
    capacity = shards * tasks_per_shard
    if capacity > len(rows):
        raise ValueError(
            f"plan asks for {capacity} tasks but the frozen source only has {len(rows)}"
        )
    selected = rows if capacity == len(rows) else select_tasks(rows, capacity)
    clusters = cluster_by_model(selected)
    plan = pack_clusters(clusters, shards, tasks_per_shard)
    manifest = plan_summary(
        plan, rows, header, source_dir, source_files,
        kind=kind, source_manifest=source_manifest,
        source_sha=source_sha or git_head(ROOT),
    )
    if kind == "formal":
        warnings = []
        if capacity != FORMAL_TASKS:
            warnings.append(f"formal plan has {capacity} tasks, protocol quota is {FORMAL_TASKS}")
        if not 80 <= shards <= 160:
            warnings.append(f"formal shard count {shards} is outside the designed 80..160 range")
        manifest["warnings"] = warnings
    manifest["source_tree_dirty"] = git_dirty(ROOT)
    return plan, header, manifest, rows


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _defaults_for(kind: str, args):
    if kind == "pilot":
        default_source = PILOT_JSON if PILOT_JSON.is_file() else PILOT_SOURCE
        default_manifest = PILOT_JSON if PILOT_JSON.is_file() else PILOT_MANIFEST
        return (args.out_dir or PILOT_OUT_DIR, args.out_manifest or PILOT_OUT_MANIFEST,
                args.shards or PILOT_SHARDS, args.tasks_per_shard,
                args.source or default_source, args.source_manifest or default_manifest)
    return (args.out_dir or FORMAL_OUT_DIR, args.out_manifest or FORMAL_OUT_MANIFEST,
            args.shards or FORMAL_SHARDS,
            args.tasks_per_shard,
            args.source or FORMAL_SOURCE, args.source_manifest or FORMAL_MANIFEST)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the model-colocated Modal shard plan for v6.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source", type=Path, default=None,
                        help="frozen task directory (default: formal when present, else Pilot)")
    parser.add_argument("--source-manifest", type=Path, default=None)
    parser.add_argument("--shards", type=int, default=None)
    parser.add_argument("--tasks-per-shard", type=int, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--out-manifest", type=Path, default=None)
    parser.add_argument("--pilot", action="store_true",
                        help="build the pre-registered 96-task Modal infrastructure Pilot plan")
    parser.add_argument("--formal", action="store_true",
                        help="build the formal 8,280-task plan (requires protocol/v6_formal_tasks)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON only")
    args = parser.parse_args(argv)

    if args.pilot and args.formal:
        parser.error("choose either --pilot or --formal")
    if args.pilot:
        kind = "pilot"
    elif args.formal:
        kind = "formal"
    else:
        kind = "formal" if FORMAL_SOURCE.is_dir() else "pilot"
        if kind == "pilot" and not args.json:
            print(
                "note: protocol/v6_formal_tasks is not frozen yet; using the frozen "
                "Pilot task list instead", file=sys.stderr,
            )
    out_dir, out_manifest, shards, tasks_per_shard, source, source_manifest = _defaults_for(kind, args)
    try:
        plan, header, manifest, _rows = build(
            source, kind=kind, shards=shards, tasks_per_shard=tasks_per_shard,
            source_manifest=source_manifest,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    summary = {
        key: manifest[key] for key in (
            "schema", "kind", "source_sha", "tasks", "shards", "tasks_per_shard",
            "distinct_model_hashes", "distinct_bounded_problems", "experiments",
            "warnings",
        )
    }
    summary["source"] = manifest["source"]
    summary["shard_task_counts"] = {item["shard"]: len(item["tasks"]) for item in plan}
    summary["shards_with_model_reuse"] = sum(
        1 for item in plan if len(item["tasks"]) != len({task_model_hash(r) for r in item["tasks"]})
    )
    summary["dry_run"] = bool(args.dry_run)
    if args.dry_run:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    manifest = write_plan(plan, header, out_dir, out_manifest, manifest)
    summary.update({
        "out_dir": str(out_dir), "out_manifest": str(out_manifest),
        "checksums_sha256": manifest["checksums_sha256"],
    })
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
