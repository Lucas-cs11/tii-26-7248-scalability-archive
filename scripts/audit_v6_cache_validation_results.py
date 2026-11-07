#!/usr/bin/env python3
"""Independent audit and budget projection for the v6 cache-validation run.

This tool is deliberately independent of any runner summary: every number is
recomputed from the raw ``raw/shard_XXX.jsonl`` artifacts, the frozen task CSVs
and the frozen manifest.  It never mutates the raw artifacts.

Outputs written to ``--out``:

1. ``audited_records.jsonl``      - raw records annotated with frozen-task metadata
2. ``cache_validation_audit.json`` - PASS / acceptance / errors / findings / counts / gates
3. ``cache_statistics.csv``       - family / experiment / cache-status / WIP / L summaries
4. ``differential_pairs.json``    - cold-vs-cached r0/r1 field-by-field comparison
5. ``budget_projection.json``     - conservative projection for the formal 8,280 matrix

``--fixtures`` runs a self-test: it builds a synthetic 96-cell protocol + raw run
that mirrors the real frozen shape, verifies a clean run is PASS, then injects
six independent faults (missing record, duplicate record, tampered raw hash,
frozen-field mismatch, hit without a prior miss, differential counter mismatch)
and verifies each one is caught.

Usage:
    python scripts/audit_v6_cache_validation_results.py \\
        --manifest protocol/v6_cache_validation_manifest.json \\
        --tasks protocol/v6_cache_validation_tasks \\
        --artifacts work/v6_cache_validation_<runid> \\
        --run-id <runid> --source-sha <sha> --out <audit_dir>

    python scripts/audit_v6_cache_validation_results.py --fixtures
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Frozen contract constants (task brief + Lead-confirmed cache schema)
# ---------------------------------------------------------------------------
CACHE_KEY_FORMAT = "v6-scg-cache"
CACHE_KEY_FORMAT_VERSION = 2
CACHE_SEMANTICS_VERSION = "v6-strong-time-1"
CACHE_SERIALIZATION = "json-fraction-v2"
CACHE_PYTHON_MAJOR_MINOR = "3.13"

CACHE_OK_STATUSES = {"hit", "miss", "disabled"}
CACHE_DEGRADED_STATUSES = {"store_error", "load_error"}
CACHE_STATUSES = CACHE_OK_STATUSES | CACHE_DEGRADED_STATUSES
EXECUTION_STATUSES = {"COMPLETED", "TIMEOUT_WALL_CLOCK", "ERROR"}
VALID_VERDICTS = {"VER", "AMB", "INC"}

#: Stages that imply the SCG (and therefore the cache decision) was reached.
CACHE_COMPLETE_STAGES = {
    "SCG_CACHED",
    "SCG_COMPLETE",
    "SYNCHRONIZER_COMPLETE",
    "FIRST_CERTIFICATE_COMPLETE",
    "HISTORY_ANALYSIS_PARTIAL",
    "VERIFICATION_COMPLETE",
    "SCG",
    "BOUNDED_ANALYSIS",
    "COMPLETE",
}
#: Stages before the cache decision; metadata may legitimately be missing there.
PRE_CACHE_STAGES = {"MODEL_GENERATION", "MODEL_GENERATED", "MODEL_VALIDATED"}

CACHE_RECORD_FIELDS = (
    "scg_cache_status",
    "scg_cache_key",
    "scg_cache_provenance",
    "runtime_scg_build_s",
    "runtime_scg_load_s",
)

KEY_MATERIAL_REQUIRED = (
    "format",
    "format_version",
    "model_hash",
    "source_sha",
    "scg_builder_sha",
    "semantics_version",
    "python",
    "serialization",
)

#: Keys the final provenance dict must carry for hit/miss cache decisions.
PROVENANCE_REQUIRED_BASE = (
    "cache_status",
    "key",
    "key_material",
    "reason",
    "build_s",
    "load_s",
    "store_s",
    "payload_sha256",
    "file_bytes",
    "serializer",
)

DIFF_SCALAR_FIELDS = (
    "verdict",
    "inconclusive_reason",
    "enumeration_complete",
    "critical_pair_count",
    "model_hash",
    "structure_hash",
    "execution_status",
    "failure_stage",
    "params",
    "L",
    "H",
    "mode",
)

#: Counter keys allowed to differ between the cold build and the cached load.
DIFF_COUNTER_EXCLUDED_PREFIXES = ("runtime_", "wall", "time_")
DIFF_COUNTER_EXCLUDED_TOKENS = ("cache",)

#: Fixed formal-matrix quota (from the task brief / frozen formal plan).
FORMAL_QUOTA = OrderedDict(
    [
        ("structural", 600),
        ("wip", 1080),
        ("lsweep", 5400),
        ("difficult", 1200),
    ]
)
FORMAL_TOTAL_TASKS = sum(FORMAL_QUOTA.values())
CONCURRENCY = 20
JOB_TIME_LIMIT_HOURS = 6.0
CELL_TIMEOUT_SECONDS = 60

#: Which cache-validation experiment / stratum proxies each formal bucket.
#: There is no A_structural experiment in the cache-validation protocol; its
#: cold structural cells are proxied by the C_Lsweep ``L_low_first`` cold row.
BUCKET_SELECTORS = OrderedDict(
    [
        ("structural", {"experiment": "C_Lsweep", "stratum": "L_low_first"}),
        ("wip", {"experiment": "B_wip"}),
        ("lsweep", {"experiment": "C_Lsweep"}),
        ("difficult", {"experiment": "D_difficult"}),
    ]
)

LSWEEP_GROUP_SIZES = (3, 6, 9)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _num(value):
    """Return ``value`` as a finite float, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _replicate(record):
    return _as_int(record.get("replicate", record.get("replicate_id", 0))) or 0


def _is_r0(record):
    return _replicate(record) == 0


def _is_hex64(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _major_minor(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\.\d+)?\s*", value)
    if not match:
        return None
    return f"{match.group(1)}.{match.group(2)}"


def _percentile(values, probability):
    """Nearest-rank percentile (same convention as the Pilot audit)."""
    numbers = [value for value in values if _num(value) is not None]
    if not numbers:
        return None
    ordered = sorted(float(value) for value in numbers)
    index = max(0, math.ceil(probability * len(ordered)) - 1)
    return ordered[index]


def raw_hash(record):
    """Recompute ``raw_record_hash`` exactly as runner/run_shard.py does."""
    payload = dict(record)
    expected = payload.pop("raw_record_hash", None)
    actual = hashlib.sha256(
        json.dumps(payload, default=str, sort_keys=True).encode()
    ).hexdigest()
    return expected, actual


def cache_key_from_material(key_material):
    """Recompute the cache key from key material (contract canonical JSON)."""
    return hashlib.sha256(
        json.dumps(key_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _add(errors, code, detail=""):
    errors.append(f"{code}: {detail}" if detail else code)


def _codes(errors):
    codes = set()
    for message in errors:
        codes.add(message.split(":", 1)[0].strip())
    return codes


def _normalise_scalar(value):
    """Normalise a value for differential comparison."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _scalar_equal(left, right):
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, dict) and isinstance(right, dict):
        return left == right
    left_num, right_num = _num(left), _num(right)
    if left_num is not None and right_num is not None:
        return math.isclose(left_num, right_num, rel_tol=1e-12, abs_tol=1e-12)
    return left == right


def _counter_key_compared(key):
    lowered = key.lower()
    if key.startswith(DIFF_COUNTER_EXCLUDED_PREFIXES):
        return False
    if any(token in lowered for token in DIFF_COUNTER_EXCLUDED_TOKENS):
        return False
    return True


# ---------------------------------------------------------------------------
# Protocol / CSV loading
# ---------------------------------------------------------------------------
def _coerce_task(row):
    task = dict(row)
    for key in ("seed", "replicate", "L", "timeout_s"):
        task[key] = _as_int(row.get(key))
    try:
        task["H"] = float(str(row.get("H")).strip())
    except (TypeError, ValueError):
        task["H"] = None
    try:
        task["params_obj"] = json.loads(row.get("params") or "{}")
    except (TypeError, ValueError):
        task["params_obj"] = None
    return task


def load_protocol(manifest_path, tasks_dir):
    """Load manifest + frozen CSVs; return tasks keyed by task_id."""
    errors = []
    findings = []
    manifest = json.loads(Path(manifest_path).read_text())
    task_files = sorted(Path(tasks_dir).glob("shard_*.csv"))
    if not task_files:
        _add(errors, "TASK_CSV_MISSING", f"no shard_*.csv under {tasks_dir}")
    tasks = {}
    shard_of_task = {}
    task_rows = []
    for path in task_files:
        match = re.search(r"shard_(\d+)\.csv$", path.name)
        shard = int(match.group(1)) if match else None
        with path.open(newline="", encoding="utf-8") as handle:
            for line_no, row in enumerate(csv.DictReader(handle), start=2):
                task = _coerce_task(row)
                task_id = task.get("task_id")
                if not task_id:
                    _add(errors, "TASK_CSV_INVALID", f"{path.name}:{line_no} has no task_id")
                    continue
                if task_id in tasks:
                    _add(errors, "TASK_CSV_DUPLICATE", task_id)
                tasks[task_id] = task
                shard_of_task[task_id] = shard
                task_rows.append((shard, path, line_no, task))

    expected_shard_ids = {value for value in shard_of_task.values() if value is not None}

    expected_tasks = manifest.get("tasks")
    if isinstance(expected_tasks, int) and len(tasks) != expected_tasks:
        _add(errors, "TASK_COUNT_MISMATCH", f"manifest {expected_tasks}, CSV {len(tasks)}")

    families = manifest.get("families")
    family_names = manifest.get("family_names")
    expected_family_count = None
    if isinstance(families, int):
        expected_family_count = families
    elif isinstance(families, list):
        expected_family_count = len(families)
    elif isinstance(family_names, list):
        expected_family_count = len(family_names)
    observed_families = {task["benchmark_family"] for task in tasks.values()}
    if expected_family_count is not None and len(observed_families) != expected_family_count:
        _add(
            errors,
            "FAMILY_COUNT_MISMATCH",
            f"manifest {expected_family_count}, CSV {len(observed_families)}",
        )

    tasks_per_family = manifest.get("tasks_per_family")
    if isinstance(tasks_per_family, int):
        per_family = Counter(task["benchmark_family"] for task in tasks.values())
        for family, count in sorted(per_family.items()):
            if count != tasks_per_family:
                _add(
                    errors,
                    "FAMILY_TASK_COUNT_MISMATCH",
                    f"{family}: {count} tasks, expected {tasks_per_family}",
                )

    tasks_per_shard = manifest.get("tasks_per_shard")
    if isinstance(tasks_per_shard, int):
        per_shard = Counter(value for value in shard_of_task.values() if value is not None)
        for shard, count in sorted(per_shard.items()):
            if count != tasks_per_shard:
                _add(
                    errors,
                    "SHARD_TASK_COUNT_MISMATCH",
                    f"shard {shard:03d}: {count} tasks, expected {tasks_per_shard}",
                )

    configuration_counts = Counter(task["configuration_id"] for task in tasks.values())
    for configuration_id, count in sorted(configuration_counts.items()):
        if count > 1:
            _add(errors, "TASK_CSV_DUPLICATE_CONFIGURATION", configuration_id)

    return {
        "manifest": manifest,
        "tasks": tasks,
        "shard_of_task": shard_of_task,
        "expected_shard_ids": expected_shard_ids,
        "task_rows": task_rows,
        "errors": errors,
        "findings": findings,
    }


def _expected_shard_count(manifest, expected_shard_ids):
    shards = manifest.get("shards")
    if isinstance(shards, int):
        return set(range(shards))
    if isinstance(shards, list):
        indices = set()
        for item in shards:
            if isinstance(item, dict):
                value = item.get("shard_id", item.get("id"))
            else:
                value = item
            number = _as_int(value)
            if number is not None:
                indices.add(number)
        if indices:
            return indices
    return set(expected_shard_ids)


def load_raw_entries(artifacts_dir, expected_shard_ids, manifest, run_id):
    """Discover ``**/raw/shard_*.jsonl`` artifacts and parse every record."""
    errors = []
    findings = []
    artifacts_dir = Path(artifacts_dir)
    expected = _expected_shard_count(manifest, expected_shard_ids)
    if expected_shard_ids and expected != expected_shard_ids:
        _add(
            errors,
            "TASK_SHARD_COVERAGE",
            f"CSV shards {sorted(expected_shard_ids)} != manifest shards {sorted(expected)}",
        )
    raw_paths = sorted(artifacts_dir.glob("**/raw/shard_*.jsonl"))
    if not raw_paths:
        _add(errors, "RAW_SHARD_MISSING", f"no **/raw/shard_*.jsonl under {artifacts_dir}")
    found = {}
    for path in raw_paths:
        match = re.search(r"shard_(\d+)\.jsonl$", path.name)
        if not match:
            _add(errors, "RAW_SHARD_UNEXPECTED", str(path))
            continue
        shard = int(match.group(1))
        if shard in found:
            _add(
                errors,
                "RAW_SHARD_DUPLICATE",
                f"shard {shard:03d}: {found[shard]} and {path}",
            )
        else:
            found[shard] = path
        directory_name = path.parent.parent.name
        if directory_name.startswith("v6-cv-"):
            dir_match = re.fullmatch(r"v6-cv-(\d+)-(.+)", directory_name)
            if not dir_match:
                _add(errors, "ARTIFACT_DIR_MISMATCH", directory_name)
            else:
                if int(dir_match.group(1)) != shard:
                    _add(
                        errors,
                        "ARTIFACT_DIR_MISMATCH",
                        f"{directory_name} holds shard_{shard:03d}.jsonl",
                    )
                if str(dir_match.group(2)) != str(run_id):
                    _add(
                        errors,
                        "ARTIFACT_DIR_MISMATCH",
                        f"{directory_name} run id != {run_id}",
                    )

    missing = sorted(expected - set(found))
    unexpected = sorted(set(found) - expected)
    if missing:
        _add(errors, "RAW_SHARD_MISSING", f"missing shard files {missing}")
    if unexpected:
        _add(errors, "RAW_SHARD_UNEXPECTED", f"unexpected shard files {unexpected}")
    if len(found) != len(expected):
        _add(
            errors,
            "RAW_SHARD_COUNT",
            f"expected {len(expected)} raw shard files, found {len(found)}",
        )

    entries = []
    raw_bytes = 0
    for shard in sorted(found):
        path = found[shard]
        raw_bytes += path.stat().st_size
        with path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    _add(
                        errors,
                        "RAW_PARSE_ERROR",
                        f"shard {shard:03d} line {line_no}: {exc}",
                    )
                    continue
                entries.append(
                    {
                        "record": record,
                        "shard": shard,
                        "line": line_no,
                        "path": str(path),
                        "artifact_dir": path.parent.parent.name,
                    }
                )
    expected_records = manifest.get("tasks")
    if isinstance(expected_records, int) and len(entries) != expected_records:
        _add(
            errors,
            "RAW_RECORD_COUNT",
            f"expected {expected_records} raw records, found {len(entries)}",
        )
    return {
        "entries": entries,
        "raw_paths": raw_paths,
        "raw_bytes": raw_bytes,
        "errors": errors,
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Per-record audit
# ---------------------------------------------------------------------------
FIELD_SPEC = (
    ("protocol_version", "str"),
    ("experiment", "str"),
    ("experiment_id", "str"),
    ("benchmark_family", "str"),
    ("model_id", "str"),
    ("bounded_problem_id", "str"),
    ("configuration_id", "str"),
    ("seed", "int"),
    ("replicate", "int"),
    ("L", "int"),
    ("H", "float"),
    ("mode", "str"),
    ("timeout_s", "int"),
)

#: Fields that must match for *every* status (the shard runner always writes them).
IDENTITY_FIELDS = (
    "task_id",
    "configuration_id",
    "bounded_problem_id",
    "L",
    "H",
    "mode",
    "params",
    "seed",
    "replicate",
    "protocol_version",
    "timeout_s",
)


def _field_equal(kind, value, expected):
    if kind == "str":
        return value is not None and str(value) == str(expected)
    if kind == "int":
        number = _as_int(value)
        return number is not None and expected is not None and number == expected
    if kind == "float":
        number = _num(value)
        return number is not None and expected is not None and math.isclose(
            number, expected, rel_tol=1e-12, abs_tol=1e-12
        )
    return value == expected


def _audit_cache_fields(entry, task, errors, findings, ctx, source_sha, components):
    """Audit one record's cache metadata; returns info for statistics/budget."""
    record = entry["record"]
    task_id = str(record.get("task_id"))
    status = record.get("execution_status")
    stage = record.get("failure_stage")
    cache_contract_required = status == "COMPLETED" or (
        status == "TIMEOUT_WALL_CLOCK" and stage in CACHE_COMPLETE_STAGES
    )

    if not cache_contract_required:
        ctx["censored"] += 1
        if ctx["censored"] <= 200:
            ctx["censored_records"].append(
                {
                    "task_id": task_id,
                    "execution_status": status,
                    "failure_stage": stage,
                }
            )
        return {"censored": True}

    missing_fields = [name for name in CACHE_RECORD_FIELDS if name not in record]
    if missing_fields:
        _add(
            errors,
            "CACHE_FIELD_MISSING",
            f"{task_id}: missing {missing_fields}",
        )

    cache_status = record.get("scg_cache_status")
    key = record.get("scg_cache_key")
    provenance = record.get("scg_cache_provenance")
    expected_status = task.get("expected_cache_status") if task else None

    if cache_status in CACHE_DEGRADED_STATUSES:
        ctx["degraded"] += 1
        ctx["degraded_records"].append({"task_id": task_id, "status": cache_status})
        findings.append(
            {
                "code": "cache_degraded_status",
                "issue": "cache reported a degraded status instead of hit/miss",
                "task_id": task_id,
                "status": cache_status,
            }
        )
        return {"cache_status": cache_status, "degraded": True}
    if cache_status == "disabled":
        ctx["disabled"] += 1
        ctx["disabled_records"].append(task_id)
        findings.append(
            {
                "code": "cache_disabled",
                "issue": "SCG cache was disabled for this cell",
                "task_id": task_id,
            }
        )
        if key is not None:
            findings.append(
                {
                    "code": "cache_disabled_with_key",
                    "issue": "disabled cache cell still carries a cache key",
                    "task_id": task_id,
                    "scg_cache_key": key,
                }
            )
        return {"cache_status": cache_status, "disabled": True}
    if cache_status not in ("hit", "miss"):
        _add(
            errors,
            "CACHE_STATUS_INVALID",
            f"{task_id}: scg_cache_status={cache_status!r}",
        )
        return {"cache_status": cache_status}

    ctx["status_counts"][cache_status] += 1
    if expected_status and cache_status != expected_status:
        _add(
            errors,
            "CACHE_STATUS_MISMATCH",
            f"{task_id}: record {cache_status!r}, frozen CSV expects {expected_status!r}",
        )

    if not _is_hex64(key):
        _add(errors, "CACHE_KEY_INVALID", f"{task_id}: scg_cache_key={key!r}")
        key = None

    if not isinstance(provenance, dict):
        _add(errors, "CACHE_PROVENANCE_MISSING", f"{task_id}: {type(provenance).__name__}")
        return {"cache_status": cache_status, "key": key, "provenance": None}

    ctx["provenance_checked"] += 1
    required_keys = list(PROVENANCE_REQUIRED_BASE)
    if cache_status == "miss":
        required_keys.append("store_ok")
    for name in required_keys:
        if name not in provenance:
            _add(
                errors,
                "CACHE_PROVENANCE_FIELD",
                f"{task_id}: provenance lacks {name!r}",
            )
    if provenance.get("cache_status") != cache_status:
        _add(
            errors,
            "CACHE_PROVENANCE_MISMATCH",
            f"{task_id}: provenance cache_status={provenance.get('cache_status')!r} "
            f"!= record {cache_status!r}",
        )
    if key is not None and provenance.get("key") not in (None, key):
        _add(
            errors,
            "CACHE_PROVENANCE_MISMATCH",
            f"{task_id}: provenance key != scg_cache_key",
        )

    expected_serialization = components.get("serialization", CACHE_SERIALIZATION)
    if provenance.get("serializer") != expected_serialization:
        _add(
            errors,
            "CACHE_PROVENANCE_MISMATCH",
            f"{task_id}: provenance serializer={provenance.get('serializer')!r} "
            f"!= {expected_serialization!r}",
        )

    key_material = provenance.get("key_material")
    if not isinstance(key_material, dict):
        _add(errors, "CACHE_PROVENANCE_FIELD", f"{task_id}: key_material is not a dict")
        return {"cache_status": cache_status, "key": key, "provenance": provenance}

    expected_components = {
        "format": components.get("format", CACHE_KEY_FORMAT),
        "format_version": components.get("format_version", CACHE_KEY_FORMAT_VERSION),
        "semantics_version": components.get("semantics_version", CACHE_SEMANTICS_VERSION),
        "serialization": components.get("serialization", CACHE_SERIALIZATION),
    }
    for name, value in expected_components.items():
        if name not in key_material:
            _add(
                errors,
                "CACHE_PROVENANCE_FIELD",
                f"{task_id}: key_material lacks {name!r}",
            )
        elif key_material[name] != value:
            _add(
                errors,
                "CACHE_PROVENANCE_MISMATCH",
                f"{task_id}: key_material.{name}={key_material[name]!r} != {value!r}",
            )
    for name in KEY_MATERIAL_REQUIRED:
        if name not in key_material:
            _add(
                errors,
                "CACHE_PROVENANCE_FIELD",
                f"{task_id}: key_material lacks required component {name!r}",
            )
    if key_material.get("model_hash") != record.get("model_hash"):
        _add(
            errors,
            "CACHE_PROVENANCE_MISMATCH",
            f"{task_id}: key_material.model_hash != record.model_hash",
        )
    if key_material.get("source_sha") != record.get("commit_sha"):
        _add(
            errors,
            "CACHE_PROVENANCE_MISMATCH",
            f"{task_id}: key_material.source_sha != record.commit_sha",
        )
    if key_material.get("source_sha") != source_sha:
        _add(
            errors,
            "CACHE_PROVENANCE_MISMATCH",
            f"{task_id}: key_material.source_sha != --source-sha",
        )
    if not _is_hex64(key_material.get("scg_builder_sha")):
        _add(
            errors,
            "CACHE_PROVENANCE_MISMATCH",
            f"{task_id}: key_material.scg_builder_sha is not a sha256 hex",
        )
    python_material = key_material.get("python")
    if not isinstance(python_material, str) or _major_minor(python_material) is None:
        _add(
            errors,
            "CACHE_PROVENANCE_MISMATCH",
            f"{task_id}: key_material.python={python_material!r} is not major.minor",
        )
    elif _major_minor(record.get("python")) is None:
        findings.append(
            {
                "code": "cache_record_python_missing",
                "issue": "record has no parseable python version to cross-check",
                "task_id": task_id,
                "record_python": record.get("python"),
            }
        )
    elif _major_minor(python_material) != _major_minor(record.get("python")):
        _add(
            errors,
            "CACHE_PYTHON_MISMATCH",
            f"{task_id}: key material {python_material!r} vs record {record.get('python')!r}",
        )

    recomputed = None
    try:
        recomputed = cache_key_from_material(key_material)
    except (TypeError, ValueError) as exc:
        _add(errors, "CACHE_KEY_UNCOMPUTABLE", f"{task_id}: {exc}")
    else:
        if key is not None:
            if recomputed != key:
                _add(
                    errors,
                    "CACHE_KEY_MISMATCH",
                    f"{task_id}: recomputed {recomputed} != recorded {key}",
                )
            else:
                ctx["key_verified"] += 1
        else:
            _add(
                errors,
                "CACHE_KEY_UNVERIFIABLE",
                f"{task_id}: provenance key material present but record key invalid",
            )

    # --- status-specific provenance shape -----------------------------------
    if cache_status == "hit":
        if provenance.get("reason") != "verified":
            _add(
                errors,
                "CACHE_PROVENANCE_MISMATCH",
                f"{task_id}: hit reason={provenance.get('reason')!r} != 'verified'",
            )
        if provenance.get("build_s") is not None:
            _add(
                errors,
                "CACHE_PROVENANCE_MISMATCH",
                f"{task_id}: hit provenance build_s must be null",
            )
        if provenance.get("store_s") is not None:
            _add(
                errors,
                "CACHE_PROVENANCE_MISMATCH",
                f"{task_id}: hit provenance store_s must be null",
            )
        load_s = _num(provenance.get("load_s"))
        if load_s is None:
            _add(
                errors,
                "CACHE_PROVENANCE_FIELD",
                f"{task_id}: hit provenance load_s must be a number",
            )
        if not _is_hex64(provenance.get("payload_sha256")):
            _add(
                errors,
                "CACHE_PROVENANCE_FIELD",
                f"{task_id}: hit provenance payload_sha256 is not sha256 hex",
            )
        file_bytes = provenance.get("file_bytes")
        if not isinstance(file_bytes, int) or isinstance(file_bytes, bool) or file_bytes <= 0:
            _add(
                errors,
                "CACHE_PROVENANCE_FIELD",
                f"{task_id}: hit provenance file_bytes={file_bytes!r}",
            )
        record_load = _num(record.get("runtime_scg_load_s"))
        if record_load is None:
            _add(
                errors,
                "CACHE_TIME_INVALID",
                f"{task_id}: hit record runtime_scg_load_s={record.get('runtime_scg_load_s')!r}",
            )
        elif load_s is not None and not math.isclose(
            record_load, load_s, rel_tol=1e-9, abs_tol=1e-9
        ):
            findings.append(
                {
                    "code": "cache_load_s_record_mismatch",
                    "issue": "record runtime_scg_load_s differs from provenance load_s",
                    "task_id": task_id,
                    "record_load_s": record_load,
                    "provenance_load_s": load_s,
                }
            )
        record_build = _num(record.get("runtime_scg_build_s"))
        if record_build not in (None, 0.0):
            _add(
                errors,
                "CACHE_HIT_BUILD_TIME",
                f"{task_id}: hit must not record build time ({record_build})",
            )
    elif cache_status == "miss":
        build_s = _num(provenance.get("build_s"))
        load_s = _num(provenance.get("load_s"))
        store_s = _num(provenance.get("store_s"))
        if load_s is None:
            _add(
                errors,
                "CACHE_PROVENANCE_FIELD",
                f"{task_id}: miss provenance load_s must be a number (0.0)",
            )
        if store_s is None:
            _add(
                errors,
                "CACHE_PROVENANCE_FIELD",
                f"{task_id}: miss provenance store_s must be a number",
            )
        store_ok = provenance.get("store_ok")
        if store_ok is True:
            if provenance.get("reason") != "stored":
                _add(
                    errors,
                    "CACHE_PROVENANCE_MISMATCH",
                    f"{task_id}: miss store_ok reason={provenance.get('reason')!r} != 'stored'",
                )
            if build_s is None:
                _add(
                    errors,
                    "CACHE_PROVENANCE_FIELD",
                    f"{task_id}: miss store_ok provenance build_s must be a number",
                )
            if not _is_hex64(provenance.get("payload_sha256")):
                _add(
                    errors,
                    "CACHE_PROVENANCE_FIELD",
                    f"{task_id}: miss payload_sha256 is not sha256 hex",
                )
            file_bytes = provenance.get("file_bytes")
            if not isinstance(file_bytes, int) or isinstance(file_bytes, bool) or file_bytes <= 0:
                _add(
                    errors,
                    "CACHE_PROVENANCE_FIELD",
                    f"{task_id}: miss file_bytes={file_bytes!r}",
                )
        elif store_ok is False:
            reason = str(provenance.get("reason") or "")
            if not (
                reason.startswith("store_error:")
                or reason.startswith("entry_too_large:")
            ):
                _add(
                    errors,
                    "CACHE_PROVENANCE_MISMATCH",
                    f"{task_id}: failed store reason={reason!r}",
                )
            if provenance.get("payload_sha256") is not None:
                _add(
                    errors,
                    "CACHE_PROVENANCE_MISMATCH",
                    f"{task_id}: failed store payload_sha256 must be null",
                )
            if provenance.get("file_bytes") is not None:
                _add(
                    errors,
                    "CACHE_PROVENANCE_MISMATCH",
                    f"{task_id}: failed store file_bytes must be null",
                )
            ctx["store_failures"] += 1
        else:
            _add(
                errors,
                "CACHE_PROVENANCE_FIELD",
                f"{task_id}: miss provenance store_ok={store_ok!r}",
            )
        record_build = _num(record.get("runtime_scg_build_s"))
        if record_build is None:
            findings.append(
                {
                    "code": "cache_miss_build_missing",
                    "issue": "completed miss does not report runtime_scg_build_s",
                    "task_id": task_id,
                }
            )

    # --- hit evidence chain (same shard, earlier JSONL line) ----------------
    if cache_status == "hit" and key is not None:
        prior = ctx["miss_keys_by_shard"][entry["shard"]].get(key)
        if prior is None:
            _add(
                errors,
                "CACHE_HIT_WITHOUT_MISS",
                f"{task_id}: shard {entry['shard']:03d} line {entry['line']} has no earlier "
                f"miss with key {key}",
            )
        else:
            ctx["hit_with_prior_miss"] += 1
            if not prior.get("store_ok"):
                findings.append(
                    {
                        "code": "cache_hit_after_failed_store",
                        "issue": "cache hit follows a miss whose store did not succeed",
                        "task_id": task_id,
                        "prior_miss_task": prior.get("task_id"),
                    }
                )
    elif cache_status == "miss" and key is not None:
        ctx["miss_keys_by_shard"][entry["shard"]][key] = {
            "task_id": task_id,
            "store_ok": provenance.get("store_ok"),
        }

    return {
        "cache_status": cache_status,
        "key": key,
        "recomputed_key": recomputed,
        "provenance": provenance,
    }


def audit_entries(entries, protocol, run_id, source_sha):
    """Audit task<->raw mapping, frozen fields, hashes, statuses and cache data."""
    errors = []
    findings = []
    tasks = protocol["tasks"]
    shard_of_task = protocol["shard_of_task"]
    manifest = protocol["manifest"]
    components = manifest.get("cache_key_components")
    if not isinstance(components, dict):
        components = {}

    by_id = defaultdict(list)
    for entry in entries:
        by_id[str(entry["record"].get("task_id"))].append(entry)

    missing = sorted(set(tasks) - set(by_id))
    duplicate = sorted(task_id for task_id, group in by_id.items() if len(group) != 1)
    unexpected = sorted(set(by_id) - set(tasks))
    if missing:
        _add(errors, "TASK_MISSING", f"{len(missing)} tasks: {missing[:20]}")
    if duplicate:
        _add(errors, "TASK_DUPLICATE", f"{len(duplicate)} task ids: {duplicate[:20]}")
    if unexpected:
        _add(errors, "TASK_UNEXPECTED", f"{len(unexpected)} ids: {unexpected[:20]}")

    ctx = {
        "status_counts": Counter(),
        "key_verified": 0,
        "provenance_checked": 0,
        "hit_with_prior_miss": 0,
        "disabled": 0,
        "degraded": 0,
        "censored": 0,
        "store_failures": 0,
        "disabled_records": [],
        "degraded_records": [],
        "censored_records": [],
        "miss_keys_by_shard": defaultdict(dict),
    }
    error_records = []
    audited = []
    recomputed_keys = {}
    for entry in entries:
        record = entry["record"]
        task = tasks.get(str(record.get("task_id")))
        task_id = str(record.get("task_id"))
        status = record.get("execution_status")

        # Always-required identity fields (the shard wrapper writes them for
        # COMPLETED, TIMEOUT_WALL_CLOCK and ERROR alike).
        for field in IDENTITY_FIELDS:
            if field == "params":
                continue
            expected = task.get(field) if task else None
            if task is not None and not _field_equal(
                dict(FIELD_SPEC).get(field, "str"), record.get(field), expected
            ):
                _add(
                    errors,
                    "FIELD_MISMATCH",
                    f"{task_id}: {field}={record.get(field)!r} != frozen {expected!r}",
                )
        if task is not None:
            if record.get("params") != task.get("params_obj"):
                _add(errors, "PARAMS_MISMATCH", task_id)
            for field in ("experiment", "benchmark_family", "experiment_id"):
                expected = task.get(field)
                if expected is not None and str(record.get(field)) != str(expected):
                    _add(
                        errors,
                        "FIELD_MISMATCH",
                        f"{task_id}: {field}={record.get(field)!r} != frozen {expected!r}",
                    )
            expected_shard = shard_of_task.get(task_id)
            if expected_shard is not None and entry["shard"] != expected_shard:
                _add(
                    errors,
                    "FIELD_MISMATCH",
                    f"{task_id}: raw shard {entry['shard']} != frozen shard {expected_shard}",
                )
            # model/structure hashes: hard for COMPLETED, censored-aware for timeouts.
            for record_field, frozen_field in (
                ("model_hash", "expected_model_hash"),
                ("structure_hash", "expected_structure_hash"),
            ):
                actual = record.get(record_field)
                expected = task.get(frozen_field)
                if expected in (None, ""):
                    continue
                if actual is None:
                    if status == "COMPLETED":
                        _add(
                            errors,
                            "FIELD_MISMATCH",
                            f"{task_id}: {record_field} is null",
                        )
                    else:
                        findings.append(
                            {
                                "code": "frozen_field_censored",
                                "issue": f"{record_field} missing on a censored record",
                                "task_id": task_id,
                                "execution_status": status,
                                "failure_stage": record.get("failure_stage"),
                                "field": record_field,
                            }
                        )
                elif actual != expected:
                    _add(
                        errors,
                        "FIELD_MISMATCH",
                        f"{task_id}: {record_field}={actual!r} != frozen {expected!r}",
                    )
            # If the record echoes expected_* fields, they must agree with the CSV.
            for key in list(record):
                if key.startswith("expected_") and key in task:
                    if record.get(key) is not None and record.get(key) != task.get(key):
                        _add(
                            errors,
                            "FIELD_MISMATCH",
                            f"{task_id}: {key} != frozen task",
                        )

        if str(record.get("workflow_run_id")) != str(run_id):
            _add(
                errors,
                "RUN_ID_MISMATCH",
                f"{task_id}: {record.get('workflow_run_id')!r} != {run_id!r}",
            )
        if record.get("commit_sha") != source_sha:
            _add(
                errors,
                "SOURCE_SHA_MISMATCH",
                f"{task_id}: {record.get('commit_sha')!r} != {source_sha!r}",
            )
        source_code_hash = record.get("source_code_hash")
        if source_code_hash is not None and source_code_hash != record.get("commit_sha"):
            _add(
                errors,
                "SOURCE_CODE_HASH_MISMATCH",
                f"{task_id}: source_code_hash != commit_sha",
            )
        if not str(record.get("job_id", "")).isdigit():
            _add(
                errors,
                "JOB_ID_INVALID",
                f"{task_id}: job_id={record.get('job_id')!r}",
            )
        wall = _num(record.get("wall_s"))
        if wall is None or wall < 0:
            _add(
                errors,
                "WALL_S_INVALID",
                f"{task_id}: wall_s={record.get('wall_s')!r}",
            )
        peak_rss = _num(record.get("peak_rss_mb"))
        if peak_rss is None or peak_rss <= 0:
            if status in ("COMPLETED", "TIMEOUT_WALL_CLOCK"):
                _add(
                    errors,
                    "PEAK_RSS_INVALID",
                    f"{task_id}: peak_rss_mb={record.get('peak_rss_mb')!r}",
                )
            else:
                findings.append(
                    {
                        "code": "peak_rss_missing_infrastructure_error",
                        "issue": "ERROR record has no measured peak RSS",
                        "task_id": task_id,
                    }
                )

        expected_hash, actual_hash = raw_hash(record)
        if expected_hash != actual_hash:
            _add(
                errors,
                "RAW_HASH_MISMATCH",
                f"{task_id}: raw_record_hash {expected_hash} != {actual_hash}",
            )

        verdict = record.get("verdict")
        if status not in EXECUTION_STATUSES:
            _add(errors, "STATUS_INVALID", f"{task_id}: {status!r}")
        elif status == "COMPLETED":
            if verdict not in VALID_VERDICTS:
                _add(errors, "VERDICT_INVALID", f"{task_id}: {verdict!r}")
        elif status == "TIMEOUT_WALL_CLOCK":
            if verdict is not None:
                _add(errors, "TIMEOUT_HAS_VERDICT", f"{task_id}: verdict={verdict!r}")
        else:  # ERROR is reported separately, never by itself a PASS blocker.
            error_records.append(
                {
                    "task_id": task_id,
                    "failure_stage": record.get("failure_stage"),
                    "error": record.get("error"),
                    "error_type": record.get("error_type"),
                    "execution_status": status,
                }
            )

        cache_info = {}
        if task is not None:
            cache_info = _audit_cache_fields(
                entry, task, errors, findings, ctx, source_sha, components
            )
        if cache_info.get("recomputed_key") is not None:
            recomputed_keys[task_id] = cache_info["recomputed_key"]

        annotated = dict(record)
        if task is not None:
            annotated.update(
                {
                    "audit_shard": entry["shard"],
                    "audit_line": entry["line"],
                    "audit_source": entry["path"],
                    "audit_artifact_dir": entry["artifact_dir"],
                    "audit_stratum": task.get("stratum"),
                    "audit_base_model_id": task.get("base_model_id"),
                    "audit_cache_group": task.get("cache_group"),
                    "audit_expected_cache_status": task.get("expected_cache_status"),
                }
            )
        if task_id in recomputed_keys:
            annotated["audit_recomputed_cache_key"] = recomputed_keys[task_id]
        audited.append(annotated)

    if ctx["censored"]:
        findings.append(
            {
                "code": "cache_metadata_censored",
                "issue": (
                    "cells killed before the cache decision (pre-SCG failure stage) "
                    "carry no cache verdict; reported, not counted as a contract failure"
                ),
                "records": ctx["censored"],
                "examples": ctx["censored_records"][:20],
            }
        )
    if ctx["disabled"]:
        findings.append(
            {
                "code": "cache_disabled_cells",
                "issue": "SCG cache was disabled for some cells; CACHE_INTEGRITY gate is false",
                "records": ctx["disabled"],
                "examples": ctx["disabled_records"][:20],
            }
        )
    if ctx["degraded"]:
        findings.append(
            {
                "code": "cache_degraded_cells",
                "issue": "cache reported degraded store/load status",
                "records": ctx["degraded"],
                "examples": ctx["degraded_records"][:20],
            }
        )
    if ctx["store_failures"]:
        findings.append(
            {
                "code": "cache_store_failures",
                "issue": (
                    "cache store failed (cache policy: caching must never break a cell); "
                    "the cell was still analysed from the cold build"
                ),
                "records": ctx["store_failures"],
            }
        )

    return {
        "audited": audited,
        "errors": errors,
        "findings": findings,
        "ctx": ctx,
        "error_records": error_records,
        "recomputed_keys": recomputed_keys,
    }


# ---------------------------------------------------------------------------
# Expected-cache invariant
# ---------------------------------------------------------------------------
def _extract_status_counts(mapping):
    if not isinstance(mapping, dict):
        return {}
    counts = {}
    for key, value in mapping.items():
        normalised = str(key).lower().strip()
        for status in ("hit", "miss", "disabled"):
            if normalised in (status, f"{status}es", f"cache_{status}", f"n_{status}"):
                number = _as_int(value)
                if number is not None:
                    counts[status] = number
    return counts


def audit_expected_cache(manifest, tasks, entries):
    """Compare observed hit/miss counts with the frozen ``expected_cache`` block.

    A mismatch caused by cells censored before the cache decision is reported as
    a finding (the frozen expectation assumes every cell completes); an
    otherwise clean mismatch is an audit error.
    """
    errors = []
    findings = []
    observed = Counter()
    observed_per_shard = defaultdict(Counter)
    observed_per_family = defaultdict(Counter)
    for entry in entries:
        record = entry["record"]
        task = tasks.get(str(record.get("task_id")))
        if task is None:
            continue
        status = record.get("scg_cache_status")
        if status in ("hit", "miss"):
            observed[status] += 1
            observed_per_shard[entry["shard"]][status] += 1
            observed_per_family[task["benchmark_family"]][status] += 1

    expected = manifest.get("expected_cache")
    if not isinstance(expected, dict):
        findings.append(
            {
                "code": "expected_cache_block_missing",
                "issue": "manifest has no expected_cache block; count invariant not checked",
            }
        )
        return {"errors": errors, "findings": findings, "observed": observed}

    censored = entry_censored(entries)
    mismatch_details = []

    expected_total = _extract_status_counts(expected.get("total"))
    if not expected_total:
        expected_total = _extract_status_counts(expected)
    for status, count in sorted(expected_total.items()):
        if observed.get(status, 0) != count:
            mismatch_details.append(
                f"observed {observed.get(status, 0)} {status}, manifest expects {count}"
            )

    expected_per_shard = _extract_status_counts(expected.get("per_shard"))
    if expected_per_shard:
        for shard in sorted(observed_per_shard):
            for status, count in expected_per_shard.items():
                if observed_per_shard[shard].get(status, 0) != count:
                    mismatch_details.append(
                        f"shard {shard:03d}: {status} count "
                        f"{observed_per_shard[shard].get(status, 0)} != {count}"
                    )

    expected_per_family = expected.get("per_family")
    if isinstance(expected_per_family, dict):
        for family, counts in sorted(expected_per_family.items()):
            status_counts = _extract_status_counts(counts)
            for status, count in status_counts.items():
                if observed_per_family[family].get(status, 0) != count:
                    mismatch_details.append(
                        f"{family}: {status} count "
                        f"{observed_per_family[family].get(status, 0)} != {count}"
                    )

    if mismatch_details:
        if censored:
            findings.append(
                {
                    "code": "expected_cache_underfill",
                    "issue": (
                        "cache hit/miss counts differ from the frozen expectation "
                        "because some cells were censored before the cache decision"
                    ),
                    "details": mismatch_details[:20],
                    "detail_count": len(mismatch_details),
                }
            )
        else:
            for detail in mismatch_details:
                _add(errors, "CACHE_EXPECTED_COUNT_MISMATCH", detail)

    return {
        "errors": errors,
        "findings": findings,
        "observed": observed,
        "expected_total": expected_total,
        "mismatch": bool(mismatch_details),
    }



def entry_censored(entries):
    for entry in entries:
        record = entry["record"]
        status = record.get("execution_status")
        cache_status = record.get("scg_cache_status")
        if cache_status in CACHE_DEGRADED_STATUSES or cache_status == "disabled":
            return True
        if status == "COMPLETED" or cache_status in ("hit", "miss"):
            continue
        if record.get("failure_stage") in PRE_CACHE_STAGES or status == "ERROR":
            return True
    return False


# ---------------------------------------------------------------------------
# Differential pairs
# ---------------------------------------------------------------------------
def _pair_ids(pair):
    if isinstance(pair, (list, tuple)) and len(pair) == 2:
        return str(pair[0]), str(pair[1])
    if isinstance(pair, dict):
        r0 = pair.get("r0", pair.get("cold", pair.get("miss")))
        r1 = pair.get("r1", pair.get("cached", pair.get("hit")))
        if r0 is not None and r1 is not None:
            return str(r0), str(r1)
    return None, None


def audit_differential(manifest, tasks, shard_of_task, entries_by_task):
    """r0(miss) vs r1(hit) per frozen pair, ignoring runtime_* / cache counters."""
    errors = []
    findings = []
    pairs = manifest.get("differential_pairs") or []
    results = []
    complete = 0
    consistent = 0
    mismatched = 0
    censored = 0
    status_errors = 0
    for index, raw_pair in enumerate(pairs):
        r0_id, r1_id = _pair_ids(raw_pair)
        result = {
            "index": index,
            "r0_task_id": r0_id,
            "r1_task_id": r1_id,
            "status": "FAIL",
            "complete": False,
            "consistent": False,
            "mismatches": [],
        }
        if r0_id is None or r1_id is None:
            _add(errors, "DIFF_PAIR_MISSING", f"pair {index} is malformed: {raw_pair!r}")
            result["note"] = "malformed pair"
            results.append(result)
            continue
        if r0_id not in tasks or r1_id not in tasks:
            _add(
                errors,
                "DIFF_PAIR_MISSING",
                f"pair {index}: unknown task id(s) {r0_id!r}, {r1_id!r}",
            )
            result["note"] = "task id not in frozen CSV"
            results.append(result)
            status_errors += 1
            continue
        shard0 = shard_of_task.get(r0_id)
        shard1 = shard_of_task.get(r1_id)
        result["shard"] = shard0
        if shard0 != shard1:
            _add(
                errors,
                "DIFF_PAIR_SHARD",
                f"pair {index}: r0 shard {shard0}, r1 shard {shard1}",
            )
            status_errors += 1
        entry0 = entries_by_task.get(r0_id)
        entry1 = entries_by_task.get(r1_id)
        if entry0 is None or entry1 is None:
            missing = [task_id for task_id, entry in ((r0_id, entry0), (r1_id, entry1)) if entry is None]
            _add(errors, "DIFF_PAIR_MISSING", f"pair {index}: no raw record for {missing}")
            result["note"] = "raw record missing"
            results.append(result)
            status_errors += 1
            continue
        record0 = entry0["record"]
        record1 = entry1["record"]
        result["r0_cache_status"] = record0.get("scg_cache_status")
        result["r1_cache_status"] = record1.get("scg_cache_status")
        result["r0_execution_status"] = record0.get("execution_status")
        result["r1_execution_status"] = record1.get("execution_status")
        both_completed = (
            record0.get("execution_status") == "COMPLETED"
            and record1.get("execution_status") == "COMPLETED"
        )

        if not both_completed:
            # A censored pair cannot serve as a semantic comparison.  It is a
            # finding (the contract tolerates up to two such pairs via the
            # >=10/12 gate), not a hard status error.
            censored += 1
            findings.append(
                {
                    "code": "differential_pair_censored",
                    "issue": (
                        "one side of the differential pair did not complete; the pair "
                        "cannot be used as a semantic-equivalence comparison"
                    ),
                    "pair": index,
                    "r0_execution_status": record0.get("execution_status"),
                    "r1_execution_status": record1.get("execution_status"),
                    "r0_cache_status": record0.get("scg_cache_status"),
                    "r1_cache_status": record1.get("scg_cache_status"),
                    "r0_failure_stage": record0.get("failure_stage"),
                    "r1_failure_stage": record1.get("failure_stage"),
                }
            )
            result["status"] = "CENSORED"
            result["note"] = "censored by execution status"
            results.append(result)
            continue

        if record0.get("scg_cache_status") != "miss" or record1.get("scg_cache_status") != "hit":
            _add(
                errors,
                "DIFF_PAIR_STATUS",
                f"pair {index}: expected r0 miss / r1 hit, got "
                f"{record0.get('scg_cache_status')!r}/{record1.get('scg_cache_status')!r}",
            )
            status_errors += 1
            result["note"] = "wrong cache statuses"

        complete += 1
        for field in DIFF_SCALAR_FIELDS:
            left = _normalise_scalar(record0.get(field))
            right = _normalise_scalar(record1.get(field))
            if not _scalar_equal(left, right):
                result["mismatches"].append(
                    {"field": field, "r0": record0.get(field), "r1": record1.get(field)}
                )
                _add(
                    errors,
                    "DIFF_FIELD_MISMATCH",
                    f"pair {index} field {field}: r0={record0.get(field)!r} "
                    f"r1={record1.get(field)!r}",
                )
        counters0 = record0.get("counters") or {}
        counters1 = record1.get("counters") or {}
        if not isinstance(counters0, dict):
            counters0 = {}
        if not isinstance(counters1, dict):
            counters1 = {}
        for counter in sorted(set(counters0) | set(counters1)):
            if not _counter_key_compared(counter):
                continue
            left = _normalise_scalar(counters0.get(counter))
            right = _normalise_scalar(counters1.get(counter))
            if not _scalar_equal(left, right):
                result["mismatches"].append(
                    {
                        "field": f"counters.{counter}",
                        "r0": counters0.get(counter),
                        "r1": counters1.get(counter),
                    }
                )
                _add(
                    errors,
                    "DIFF_COUNTER_MISMATCH",
                    f"pair {index} counter {counter}: r0={counters0.get(counter)!r} "
                    f"r1={counters1.get(counter)!r}",
                )
        if result["mismatches"]:
            mismatched += 1
            result["status"] = "FAIL"
            result["consistent"] = False
        else:
            consistent += 1
            result["status"] = "PASS"
            result["complete"] = True
            result["consistent"] = True
        result["note"] = (
            "cold build vs verified cache load; runtime_*/cache counter keys excluded"
        )
        results.append(result)

    gate = (
        len(pairs) > 0
        and complete >= min(10, len(pairs))
        and mismatched == 0
        and status_errors == 0
    )
    summary = {
        "pairs": len(pairs),
        "complete": complete,
        "censored": censored,
        "consistent": consistent,
        "mismatched": mismatched,
        "status_errors": status_errors,
        "required_complete": min(10, len(pairs)),
        "CACHE_DIFFERENTIAL_PASS": gate,
    }
    return {
        "document": {
            "protocol_version": manifest.get("version"),
            "pairs_total": len(pairs),
            "scalar_fields_compared": list(DIFF_SCALAR_FIELDS),
            "counter_fields_compared": "all counters keys except runtime_*/cache/time keys",
            "counter_keys_excluded_prefixes": list(DIFF_COUNTER_EXCLUDED_PREFIXES),
            "counter_keys_excluded_tokens": list(DIFF_COUNTER_EXCLUDED_TOKENS),
            "policy": (
                "r0 must be a cold miss and r1 a verified cache hit in the same shard; "
                "all non-cache fields/counters must be identical; a censored pair is "
                "reported but not counted as a semantic mismatch"
            ),
            "summary": summary,
            "pairs": results,
        },
        "errors": errors,
        "findings": findings,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Statistics (right-censored; replicate==1 excluded)
# ---------------------------------------------------------------------------
STAT_COLUMNS = [
    "group",
    "value",
    "replicate_policy",
    "wall_semantics",
    "tasks",
    "completed",
    "timeout",
    "error",
    "inc",
    "ver",
    "amb",
    "timeout_fraction",
    "wall_samples",
    "wall_p50_s",
    "wall_p90_s",
    "wall_p95_s",
    "rss_samples",
    "peak_rss_p95_mb",
    "miss_tasks",
    "miss_build_samples",
    "miss_build_p50_s",
    "miss_build_p90_s",
    "miss_build_p95_s",
    "hit_tasks",
    "hit_load_samples",
    "hit_load_p50_s",
    "hit_load_p90_s",
    "hit_load_p95_s",
    "build_minus_load_p50_s",
    "build_load_savings_fraction_of_wall_p50",
    "timeout_scg_build_samples",
    "timeout_scg_build_p50_s",
    "timeout_scg_build_p90_s",
    "timeout_scg_build_p95_s",
]


def summary_row(group, value, records):
    statuses = Counter(record.get("execution_status") for record in records)
    verdicts = Counter(str(record.get("verdict")) for record in records)
    completed = [record for record in records if record.get("execution_status") == "COMPLETED"]
    walls = [value for record in completed for value in [_num(record.get("wall_s"))] if value is not None]
    rss = [
        value
        for record in records
        for value in [_num(record.get("peak_rss_mb"))]
        if value is not None and value > 0
    ]
    miss = [record for record in records if record.get("scg_cache_status") == "miss"]
    hit = [record for record in records if record.get("scg_cache_status") == "hit"]
    builds = [
        value
        for record in miss
        for value in [_num(record.get("runtime_scg_build_s"))]
        if value is not None
    ]
    loads = [
        value
        for record in hit
        for value in [_num(record.get("runtime_scg_load_s"))]
        if value is not None
    ]
    timeout_builds = [
        value
        for record in records
        if record.get("execution_status") == "TIMEOUT_WALL_CLOCK"
        for value in [_num(record.get("runtime_scg_build_s"))]
        if value is not None
    ]
    wall_p50 = _percentile(walls, 0.50)
    build_p50 = _percentile(builds, 0.50)
    load_p50 = _percentile(loads, 0.50)
    savings = (
        build_p50 - load_p50
        if build_p50 is not None and load_p50 is not None
        else None
    )
    savings_fraction = (
        savings / wall_p50
        if savings is not None and wall_p50 not in (None, 0)
        else None
    )
    tasks = len(records)
    return {
        "group": group,
        "value": str(value),
        "replicate_policy": "replicate==0 only",
        "wall_semantics": "right_censored_completed_only",
        "tasks": tasks,
        "completed": statuses.get("COMPLETED", 0),
        "timeout": statuses.get("TIMEOUT_WALL_CLOCK", 0),
        "error": statuses.get("ERROR", 0),
        "inc": verdicts.get("INC", 0),
        "ver": verdicts.get("VER", 0),
        "amb": verdicts.get("AMB", 0),
        "timeout_fraction": (statuses.get("TIMEOUT_WALL_CLOCK", 0) / tasks) if tasks else None,
        "wall_samples": len(walls),
        "wall_p50_s": wall_p50,
        "wall_p90_s": _percentile(walls, 0.90),
        "wall_p95_s": _percentile(walls, 0.95),
        "rss_samples": len(rss),
        "peak_rss_p95_mb": _percentile(rss, 0.95),
        "miss_tasks": len(miss),
        "miss_build_samples": len(builds),
        "miss_build_p50_s": build_p50,
        "miss_build_p90_s": _percentile(builds, 0.90),
        "miss_build_p95_s": _percentile(builds, 0.95),
        "hit_tasks": len(hit),
        "hit_load_samples": len(loads),
        "hit_load_p50_s": load_p50,
        "hit_load_p90_s": _percentile(loads, 0.90),
        "hit_load_p95_s": _percentile(loads, 0.95),
        "build_minus_load_p50_s": savings,
        "build_load_savings_fraction_of_wall_p50": savings_fraction,
        "timeout_scg_build_samples": len(timeout_builds),
        "timeout_scg_build_p50_s": _percentile(timeout_builds, 0.50),
        "timeout_scg_build_p90_s": _percentile(timeout_builds, 0.90),
        "timeout_scg_build_p95_s": _percentile(timeout_builds, 0.95),
    }


def _get_wip(record):
    value = record.get("WIP")
    if value is None:
        attributes = record.get("model_attributes")
        if isinstance(attributes, dict):
            value = attributes.get("WIP")
    return value


def build_statistics(audited_r0):
    rows = []
    groupers = [
        ("benchmark_family", lambda record: record.get("benchmark_family")),
        ("experiment", lambda record: record.get("experiment")),
        ("cache_status", lambda record: record.get("scg_cache_status")),
        ("WIP", _get_wip),
        ("L", lambda record: record.get("L")),
    ]
    for name, getter in groupers:
        values = {}
        for record in audited_r0:
            key = getter(record)
            values.setdefault(str(key), []).append(record)
        for value in sorted(values):
            rows.append(summary_row(name, value, values[value]))
    rows.append(summary_row("overall", "all", audited_r0))
    return rows


# ---------------------------------------------------------------------------
# Budget projection for the formal 8,280 matrix
# ---------------------------------------------------------------------------
def _metrics(records):
    statuses = Counter(record.get("execution_status") for record in records)
    completed = [record for record in records if record.get("execution_status") == "COMPLETED"]
    walls = [value for record in completed for value in [_num(record.get("wall_s"))] if value is not None]
    rss = [
        value
        for record in records
        for value in [_num(record.get("peak_rss_mb"))]
        if value is not None and value > 0
    ]
    builds = [
        value
        for record in records
        if record.get("scg_cache_status") == "miss"
        for value in [_num(record.get("runtime_scg_build_s"))]
        if value is not None
    ]
    loads = [
        value
        for record in records
        if record.get("scg_cache_status") == "hit"
        for value in [_num(record.get("runtime_scg_load_s"))]
        if value is not None
    ]
    overhead = []
    for record in completed:
        if record.get("scg_cache_status") != "miss":
            continue
        wall = _num(record.get("wall_s"))
        build = _num(record.get("runtime_scg_build_s"))
        if wall is not None and build is not None:
            overhead.append(wall - build)
    timeout_builds = [
        value
        for record in records
        if record.get("execution_status") == "TIMEOUT_WALL_CLOCK"
        for value in [_num(record.get("runtime_scg_build_s"))]
        if value is not None
    ]
    tasks = len(records)
    return {
        "tasks": tasks,
        "completed": statuses.get("COMPLETED", 0),
        "timeout": statuses.get("TIMEOUT_WALL_CLOCK", 0),
        "error": statuses.get("ERROR", 0),
        "timeout_fraction": (statuses.get("TIMEOUT_WALL_CLOCK", 0) / tasks) if tasks else None,
        "wall_p50": _percentile(walls, 0.50),
        "wall_p90": _percentile(walls, 0.90),
        "wall_samples": len(walls),
        "scg_build_p50": _percentile(builds, 0.50),
        "scg_build_p90": _percentile(builds, 0.90),
        "scg_build_samples": len(builds),
        "scg_load_p50": _percentile(loads, 0.50),
        "scg_load_p90": _percentile(loads, 0.90),
        "scg_load_samples": len(loads),
        "rss_p95": _percentile(rss, 0.95),
        "rss_p50": _percentile(rss, 0.50),
        "rss_samples": len(rss),
        "overhead_p50": _percentile(overhead, 0.50),
        "overhead_samples": len(overhead),
        "timeout_scg_build_samples": len(timeout_builds),
        "timeout_scg_build_p50": _percentile(timeout_builds, 0.50),
    }


def _select_bucket(entries, bucket):
    selector = BUCKET_SELECTORS[bucket]
    selected = []
    for entry in entries:
        task = entry.get("task")
        if not task:
            continue
        if any(task.get(key) != value for key, value in selector.items()):
            continue
        selected.append(entry)
    return selected


def _pick(value, fallback):
    return value if value is not None else fallback


def _mean(values):
    numbers = [value for value in values if _num(value) is not None]
    if not numbers:
        return None
    return sum(float(value) for value in numbers) / len(numbers)


def _scenario(cell_times, cache_entries, storage, peak_rss, basis, missing):
    """Assemble one scenario summary from per-bucket cell-time estimates."""
    complete = all(value is not None for value in cell_times.values())
    total_seconds = None
    if complete:
        total_seconds = sum(cell_times[bucket] * FORMAL_QUOTA[bucket] for bucket in FORMAL_QUOTA)
    total_compute_minutes = total_seconds / 60 if total_seconds is not None else None
    total_wall_minutes = (
        total_compute_minutes / CONCURRENCY if total_compute_minutes is not None else None
    )
    wall_hours = total_wall_minutes / 60 if total_wall_minutes is not None else None
    storage_bytes = None
    storage_block = {
        "raw_record_bytes": storage.get("raw_record_bytes"),
        "cache_entries": cache_entries,
        "cache_entries_bytes": None,
        "total_bytes": None,
        "total_gb": None,
        "complete": False,
        "missing": [],
    }
    if storage.get("raw_record_bytes") is not None and storage.get("mean_cache_entry_bytes") is not None:
        cache_bytes = cache_entries * storage["mean_cache_entry_bytes"]
        storage_block["cache_entries_bytes"] = cache_bytes
        storage_block["total_bytes"] = storage["raw_record_bytes"] + cache_bytes
        storage_block["total_gb"] = storage_block["total_bytes"] / (1024 ** 3)
        storage_block["complete"] = True
    else:
        if storage.get("raw_record_bytes") is None:
            storage_block["missing"].append("mean raw record size not measured")
        if storage.get("mean_cache_entry_bytes") is None:
            storage_block["missing"].append("cache entry size not measured (provenance.file_bytes)")
    return {
        "total_tasks": FORMAL_TOTAL_TASKS,
        "total_compute_minutes": total_compute_minutes,
        "total_wall_minutes": total_wall_minutes,
        "wall_hours_at_concurrency_20": wall_hours,
        "concurrency": CONCURRENCY,
        "job_time_limit_hours": JOB_TIME_LIMIT_HOURS,
        "jobs_needed_at_6h": (
            math.ceil(total_compute_minutes / (JOB_TIME_LIMIT_HOURS * 60))
            if total_compute_minutes is not None
            else None
        ),
        "waves_of_20_jobs_at_6h": (
            math.ceil(total_compute_minutes / (CONCURRENCY * JOB_TIME_LIMIT_HOURS * 60))
            if total_compute_minutes is not None
            else None
        ),
        "fits_one_6h_wave_at_concurrency_20": (
            wall_hours <= JOB_TIME_LIMIT_HOURS if wall_hours is not None else None
        ),
        "peak_rss_p95_mb": peak_rss,
        "storage": storage_block,
        "complete": complete,
        "basis": basis,
        "missing": missing,
    }


def compute_budget(entries, manifest, storage_inputs, audit_gates, provenance_context=None):
    """Conservative budget for the formal 8,280-cell matrix from measured data."""
    provenance_context = provenance_context or {}
    entries = [entry for entry in entries if entry.get("task")]
    r0_entries = [entry for entry in entries if _is_r0(entry["record"])]
    excluded_replicas = len(entries) - len(r0_entries)
    measured_entries = r0_entries or entries
    measured_basis = (
        "replicate==0 records only; replicate==1 differential replicas are excluded "
        "from every timing/budget statistic"
    )
    if not r0_entries:
        measured_basis += " (WARNING: no replicate==0 records found; fell back to all records)"

    overall = _metrics([entry["record"] for entry in measured_entries])

    by_family = {}
    families = sorted(
        {entry["task"].get("benchmark_family") for entry in measured_entries if entry["task"].get("benchmark_family")}
    )
    family_samples = {}
    for family in families:
        rows = [
            entry["record"]
            for entry in measured_entries
            if entry["task"].get("benchmark_family") == family
        ]
        by_family[family] = _metrics(rows)
        family_samples[family] = {
            "r0_tasks": len(rows),
            "miss": sum(1 for record in rows if record.get("scg_cache_status") == "miss"),
            "hit": sum(1 for record in rows if record.get("scg_cache_status") == "hit"),
            "completed": sum(1 for record in rows if record.get("execution_status") == "COMPLETED"),
            "timeout": sum(
                1 for record in rows if record.get("execution_status") == "TIMEOUT_WALL_CLOCK"
            ),
        }

    by_bucket = {}
    bucket_metrics = {}
    for bucket in FORMAL_QUOTA:
        selected = _select_bucket(measured_entries, bucket)
        basis = f"measured from {BUCKET_SELECTORS[bucket]}"
        if not selected:
            selected = measured_entries
            basis = "fallback: overall measured sample (no selector rows)"
        metrics = _metrics([entry["record"] for entry in selected])
        bucket_metrics[bucket] = metrics
        by_bucket[bucket] = dict(metrics)
        by_bucket[bucket]["basis"] = basis

    # Per-bucket cell-time estimators with overall fallback.
    bucket_values = {}
    for bucket in FORMAL_QUOTA:
        metrics = bucket_metrics[bucket]
        bucket_values[bucket] = {
            "wall": _pick(metrics["wall_p50"], overall["wall_p50"]),
            "build": _pick(metrics["scg_build_p50"], overall["scg_build_p50"]),
            "load": _pick(metrics["scg_load_p50"], overall["scg_load_p50"]),
            "overhead": _pick(metrics["overhead_p50"], overall["overhead_p50"]),
            "timeout_fraction": _pick(metrics["timeout_fraction"], overall["timeout_fraction"]),
            "rss_p95": _pick(metrics["rss_p95"], overall["rss_p95"]),
        }
    peak_rss = max(
        (
            value["rss_p95"]
            for value in bucket_values.values()
            if value["rss_p95"] is not None
        ),
        default=None,
    )

    # Storage inputs (measured sizes only, never invented).
    mean_raw_record_bytes = None
    raw_records = storage_inputs.get("raw_records") or 0
    if raw_records and storage_inputs.get("raw_bytes"):
        mean_raw_record_bytes = storage_inputs["raw_bytes"] / raw_records
    cache_sizes = []
    for entry in entries:
        provenance = entry["record"].get("scg_cache_provenance")
        if not isinstance(provenance, dict):
            continue
        if provenance.get("store_ok") is not True:
            continue
        file_bytes = provenance.get("file_bytes")
        if isinstance(file_bytes, int) and not isinstance(file_bytes, bool) and file_bytes > 0:
            cache_sizes.append(file_bytes)
    mean_cache_entry_bytes = _mean(cache_sizes)
    storage = {
        "mean_raw_record_bytes": mean_raw_record_bytes,
        "raw_record_bytes": (
            mean_raw_record_bytes * FORMAL_TOTAL_TASKS
            if mean_raw_record_bytes is not None
            else None
        ),
        "mean_cache_entry_bytes": mean_cache_entry_bytes,
        "mean_cache_entry_samples": len(cache_sizes),
    }

    cold_buckets = ("structural", "wip", "difficult")
    cold_tasks = sum(FORMAL_QUOTA[bucket] for bucket in cold_buckets)

    # ---- no_cache ----------------------------------------------------------
    no_cache_times = {}
    no_cache_missing = []
    for bucket in FORMAL_QUOTA:
        value = bucket_values[bucket]["wall"]
        no_cache_times[bucket] = value
        if value is None:
            no_cache_missing.append(f"{bucket}: measured wall_p50 unavailable")
    no_cache = _scenario(
        no_cache_times,
        0,
        storage,
        peak_rss,
        "per-bucket measured wall_p50 from COMPLETED r0 records (right-censored)",
        no_cache_missing,
    )

    # ---- measured_cache (L-sweep grouped by same model) --------------------
    def measured_times(group_size_k):
        times = {}
        missing = []
        for bucket in cold_buckets:
            values = bucket_values[bucket]
            if values["overhead"] is None or values["build"] is None:
                missing.append(f"{bucket}: overhead/build p50 unavailable")
                times[bucket] = None
            else:
                times[bucket] = values["overhead"] + values["build"]
        lsweep = bucket_values["lsweep"]
        if lsweep["overhead"] is None or lsweep["build"] is None or lsweep["load"] is None:
            missing.append("lsweep: overhead/build/load p50 unavailable")
            times["lsweep"] = None
        else:
            hit_rate = 1.0 - 1.0 / group_size_k
            times["lsweep"] = lsweep["overhead"] + (
                (1.0 - hit_rate) * lsweep["build"] + hit_rate * lsweep["load"]
            )
        return times, missing

    by_k = {}
    for group_size_k in LSWEEP_GROUP_SIZES:
        times, missing = measured_times(group_size_k)
        cache_entries = cold_tasks + math.ceil(FORMAL_QUOTA["lsweep"] / group_size_k)
        scenario = _scenario(
            times,
            cache_entries,
            storage,
            peak_rss,
            (
                f"per-bucket measured non-SCG overhead p50 + measured build p50 "
                f"(cold buckets, hit rate 0) or build/load weighted by L-sweep group "
                f"size k={group_size_k} (hit rate 1-1/k)"
            ),
            missing,
        )
        scenario["lsweep_group_size_k"] = group_size_k
        scenario["lsweep_hit_rate"] = 1.0 - 1.0 / group_size_k
        scenario["lsweep_misses"] = math.ceil(FORMAL_QUOTA["lsweep"] / group_size_k)
        scenario["lsweep_hits"] = FORMAL_QUOTA["lsweep"] - scenario["lsweep_misses"]
        by_k[str(group_size_k)] = scenario
    measured_cache = copy.deepcopy(by_k["3"])
    measured_cache["group_size_sensitivity"] = by_k
    measured_cache["note"] = (
        "flat figures use k=3, the most conservative of {3,6,9} for compute; "
        "cache entry counts differ per k in group_size_sensitivity"
    )

    # ---- worst_case --------------------------------------------------------
    worst_times = {bucket: float(CELL_TIMEOUT_SECONDS) for bucket in FORMAL_QUOTA}
    worst_case = _scenario(
        worst_times,
        FORMAL_TOTAL_TASKS,
        storage,
        peak_rss,
        "every one of the 8,280 cells reaches the 60 s wall-clock kill",
        [],
    )

    group_assumptions = {
        "rule": (
            "L-sweep tasks are planned in groups that share one model_hash and are "
            "colocated in one job; each group performs one cold SCG build and k-1 "
            "verified cache loads, so the hit rate is 1 - 1/k"
        ),
        "group_sizes_k": list(LSWEEP_GROUP_SIZES),
        "hit_rate_by_k": {str(k): 1.0 - 1.0 / k for k in LSWEEP_GROUP_SIZES},
        "cold_buckets": list(cold_buckets),
        "cold_hit_rate": 0.0,
        "cold_bucket_reason": (
            "structural / WIP / difficult cells are assumed to be cold builds because "
            "they do not form same-model L/H/mode reuse groups in the formal plan"
        ),
        "lsweep_tasks": FORMAL_QUOTA["lsweep"],
        "cold_tasks": cold_tasks,
        "total_tasks": FORMAL_TOTAL_TASKS,
    }

    assumptions = [
        "Formal matrix quota is fixed: structural 600, WIP 1,080, L-sweep 5,400, difficult 1,200.",
        "no_cache uses the measured per-bucket wall_p50 from COMPLETED r0 cells; timeout cells are right-censored and are never substituted with 60 s.",
        "measured_cache splits each cell into measured non-SCG overhead p50 plus a measured SCG build p50 (miss) or measured SCG load p50 (hit).",
        "L-sweep cells are assumed to be grouped by identical model_hash with group size k in {3,6,9}; hit rate 1-1/k.",
        "structural / WIP / difficult cells are treated as cold builds (hit rate 0) because the cache-validation protocol does not establish reuse there.",
        "worst_case assumes every cell reaches the 60 s wall-clock limit.",
        "GitHub constraints: 20 concurrent standard jobs, 6 h per job, ubuntu-22.04.",
        "Storage estimates multiply measured mean raw-record bytes and measured mean cache-entry bytes by the formal counts; no unmeasured size is invented.",
        "All budget figures are point estimates from the cache-validation run; the final budget gate decision is reserved for the Lead.",
    ]

    budget = {
        "protocol_version": manifest.get("version"),
        "run_id": provenance_context.get("run_id"),
        "source_sha": provenance_context.get("source_sha"),
        "matrix": {
            "total_tasks": FORMAL_TOTAL_TASKS,
            "quota": dict(FORMAL_QUOTA),
        },
        "units": {
            "times": "seconds in measured blocks; minutes/hours in scenarios",
            "memory": "MiB (peak_rss)",
            "storage": "bytes / GiB",
        },
        "measured": {
            "basis": measured_basis,
            "replicate_1_excluded": excluded_replicas,
            "overall": overall,
            "by_family": by_family,
            "by_bucket": by_bucket,
        },
        "formal_plan_assumptions": {
            "total_tasks": FORMAL_TOTAL_TASKS,
            "quota": dict(FORMAL_QUOTA),
            "lsweep_grouping": group_assumptions,
            "concurrency": CONCURRENCY,
            "job_time_limit_hours": JOB_TIME_LIMIT_HOURS,
            "cell_timeout_seconds": CELL_TIMEOUT_SECONDS,
            "runner": "GitHub-hosted ubuntu-22.04 standard runner",
        },
        "scenarios": {
            "no_cache": no_cache,
            "measured_cache": measured_cache,
            "worst_case": worst_case,
        },
        "assumptions": assumptions,
        "data_provenance": {
            "protocol_version": manifest.get("version"),
            "run_id": provenance_context.get("run_id"),
            "source_sha": provenance_context.get("source_sha"),
            "artifacts_root": provenance_context.get("artifacts"),
            "manifest": provenance_context.get("manifest"),
            "raw_records": len(entries),
            "measured_r0_records": len(r0_entries or entries),
            "families": family_samples,
            "measured_rules": {
                "sample": measured_basis,
                "wall": "COMPLETED wall_s only; right-censored (timeouts excluded, never set to 60 s)",
                "scg_build": "miss cells with numeric runtime_scg_build_s",
                "scg_load": "hit cells with numeric runtime_scg_load_s",
                "rss": "measured peak_rss_mb > 0",
                "cache_entry_size": "provenance.file_bytes on successful stores",
                "raw_record_size": "actual raw JSONL bytes / raw record count",
            },
            "missing": [],
        },
        "missing_data": [],
        "gates": {
            "CACHE_INTEGRITY_PASS": audit_gates.get("CACHE_INTEGRITY_PASS"),
            "CACHE_DIFFERENTIAL_PASS": audit_gates.get("CACHE_DIFFERENTIAL_PASS"),
            "BUDGET_COMPLETE": all(
                scenario["complete"]
                for scenario in (no_cache, measured_cache, worst_case)
            ),
            "budget": {
                "no_cache": {
                    "total_compute_minutes": no_cache["total_compute_minutes"],
                    "total_wall_minutes": no_cache["total_wall_minutes"],
                    "wall_hours_at_concurrency_20": no_cache["wall_hours_at_concurrency_20"],
                },
                "measured_cache": {
                    "total_compute_minutes": measured_cache["total_compute_minutes"],
                    "total_wall_minutes": measured_cache["total_wall_minutes"],
                    "wall_hours_at_concurrency_20": measured_cache[
                        "wall_hours_at_concurrency_20"
                    ],
                },
                "worst_case": {
                    "total_compute_minutes": worst_case["total_compute_minutes"],
                    "total_wall_minutes": worst_case["total_wall_minutes"],
                    "wall_hours_at_concurrency_20": worst_case[
                        "wall_hours_at_concurrency_20"
                    ],
                },
            },
            "decision": "reserved_for_lead",
            "note": "Audit reports the objective numbers; it does not decide the budget gate.",
        },
    }

    # Missing-data disclosure.
    missing = budget["missing_data"]
    if mean_raw_record_bytes is None:
        missing.append({"field": "mean_raw_record_bytes", "reason": "no raw record bytes measured"})
    if mean_cache_entry_bytes is None:
        missing.append(
            {
                "field": "mean_cache_entry_bytes",
                "reason": "no successful-store provenance.file_bytes measured",
            }
        )
    if overall["scg_build_p50"] is None:
        missing.append(
            {
                "field": "measured.scg_build_p50",
                "reason": "no miss record with numeric runtime_scg_build_s (cache field absent or all hits)",
            }
        )
    if overall["scg_load_p50"] is None:
        missing.append(
            {
                "field": "measured.scg_load_p50",
                "reason": "no hit record with numeric runtime_scg_load_s (cache field absent or all misses)",
            }
        )
    for bucket in FORMAL_QUOTA:
        if bucket_values[bucket]["build"] is None:
            missing.append(
                {
                    "field": f"measured.by_bucket.{bucket}.scg_build_p50",
                    "reason": "no measured cold build for this bucket or fallback",
                }
            )
    budget["data_provenance"]["missing"] = list(missing)
    return budget


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------
def _gate_codes(codes, *groups):
    group = set()
    for entries in groups:
        group.update(entries)
    return not (codes & group)


MAPPING_CODES = {
    "TASK_CSV_MISSING",
    "TASK_CSV_INVALID",
    "TASK_CSV_DUPLICATE",
    "TASK_CSV_DUPLICATE_CONFIGURATION",
    "TASK_COUNT_MISMATCH",
    "TASK_MISSING",
    "TASK_DUPLICATE",
    "TASK_UNEXPECTED",
    "FAMILY_COUNT_MISMATCH",
    "FAMILY_TASK_COUNT_MISMATCH",
    "SHARD_TASK_COUNT_MISMATCH",
    "TASK_SHARD_COVERAGE",
    "RAW_SHARD_MISSING",
    "RAW_SHARD_DUPLICATE",
    "RAW_SHARD_UNEXPECTED",
    "RAW_SHARD_COUNT",
    "RAW_RECORD_COUNT",
    "RAW_PARSE_ERROR",
}
INTEGRITY_CODES = {
    "FIELD_MISMATCH",
    "PARAMS_MISMATCH",
    "RUN_ID_MISMATCH",
    "SOURCE_SHA_MISMATCH",
    "SOURCE_CODE_HASH_MISMATCH",
    "JOB_ID_INVALID",
    "PEAK_RSS_INVALID",
    "WALL_S_INVALID",
    "RAW_HASH_MISMATCH",
    "ARTIFACT_DIR_MISMATCH",
}
STATUS_CODES = {"STATUS_INVALID", "VERDICT_INVALID", "TIMEOUT_HAS_VERDICT"}
CACHE_CODES = {
    "CACHE_FIELD_MISSING",
    "CACHE_STATUS_INVALID",
    "CACHE_STATUS_MISMATCH",
    "CACHE_KEY_INVALID",
    "CACHE_KEY_MISMATCH",
    "CACHE_KEY_UNCOMPUTABLE",
    "CACHE_KEY_UNVERIFIABLE",
    "CACHE_PROVENANCE_MISSING",
    "CACHE_PROVENANCE_FIELD",
    "CACHE_PROVENANCE_MISMATCH",
    "CACHE_PYTHON_MISMATCH",
    "CACHE_HIT_BUILD_TIME",
    "CACHE_HIT_WITHOUT_MISS",
    "CACHE_EXPECTED_COUNT_MISMATCH",
    "CACHE_TIME_INVALID",
}
DIFF_CODES = {
    "DIFF_PAIR_COUNT",
    "DIFF_PAIR_MISSING",
    "DIFF_PAIR_SHARD",
    "DIFF_PAIR_STATUS",
    "DIFF_FIELD_MISMATCH",
    "DIFF_COUNTER_MISMATCH",
}
VALIDATION_CODES = {"PROTOCOL_VALIDATION_FAILED", "PROTOCOL_VALIDATION_DUPLICATE"}


def assemble_result(
    protocol,
    raw,
    audit,
    expected_audit,
    differential,
    statistics_rows,
    budget,
    run_id,
    source_sha,
    validation_artifact,
):
    errors = []
    errors.extend(protocol["errors"])
    errors.extend(raw["errors"])
    errors.extend(audit["errors"])
    errors.extend(expected_audit["errors"])
    errors.extend(differential["errors"])
    findings = []
    findings.extend(protocol["findings"])
    findings.extend(raw["findings"])
    findings.extend(audit["findings"])
    findings.extend(expected_audit["findings"])
    findings.extend(differential["findings"])
    if validation_artifact.get("duplicate"):
        _add(errors, "PROTOCOL_VALIDATION_DUPLICATE", "multiple validation artifacts")
    elif validation_artifact.get("found") and not validation_artifact.get("PASS"):
        _add(errors, "PROTOCOL_VALIDATION_FAILED", "protocol validation artifact is not PASS")
    elif not validation_artifact.get("found"):
        findings.append(
            {
                "code": "protocol_validation_artifact_missing",
                "issue": (
                    "no v6_cache_validation_protocol_validation.json was downloaded; "
                    "GitHub protocol-validation PASS could not be re-checked"
                ),
            }
        )
    codes = _codes(errors)
    ctx = audit["ctx"]
    statuses = Counter()
    verdicts = Counter()
    for entry in raw["entries"]:
        statuses[entry["record"].get("execution_status")] += 1
        verdicts[str(entry["record"].get("verdict"))] += 1
    expected_status = Counter(
        task.get("expected_cache_status") for task in protocol["tasks"].values()
    )
    observed_status = Counter()
    for entry in raw["entries"]:
        value = entry["record"].get("scg_cache_status")
        observed_status[str(value)] += 1
    cache_integrity = (
        _gate_codes(codes, CACHE_CODES)
        and ctx["disabled"] == 0
        and ctx["degraded"] == 0
    )
    gates = {
        "TASK_RAW_MAPPING_PASS": _gate_codes(codes, MAPPING_CODES),
        "RECORD_INTEGRITY_PASS": _gate_codes(codes, INTEGRITY_CODES),
        "EXECUTION_STATUS_PASS": _gate_codes(codes, STATUS_CODES),
        "CACHE_INTEGRITY_PASS": cache_integrity,
        "CACHE_DIFFERENTIAL_PASS": differential["summary"]["CACHE_DIFFERENTIAL_PASS"],
        "EXPECTED_CACHE_COUNTS_PASS": "CACHE_EXPECTED_COUNT_MISMATCH" not in codes,
        "PROTOCOL_VALIDATION_PASS": (
            bool(validation_artifact.get("PASS"))
            if validation_artifact.get("found")
            else None
        ),
        "INFRASTRUCTURE_ERRORS": {
            "count": len(audit["error_records"]),
            "reported": True,
            "does_not_block_PASS": True,
            "records": audit["error_records"][:50],
        },
        "CACHE_DISABLED_CELLS": ctx["disabled"],
        "CACHE_DEGRADED_CELLS": ctx["degraded"],
        "CACHE_CENSORED_CELLS": ctx["censored"],
    }
    result = {
        "PASS": not errors,
        "acceptance": (
            "FAIL"
            if errors
            else ("PASS" if not findings else "PASS_WITH_FINDINGS")
        ),
        "protocol_version": protocol["manifest"].get("version"),
        "run_id": str(run_id),
        "source_sha": source_sha,
        "errors": sorted(set(errors)),
        "findings": findings,
        "counts": {
            "raw_records": len(raw["entries"]),
            "raw_shards": len(raw["raw_paths"]),
            "expected_tasks": len(protocol["tasks"]),
            "unique_task_ids": len({entry["record"].get("task_id") for entry in raw["entries"]}),
            "replicate_0": sum(1 for entry in raw["entries"] if _is_r0(entry["record"])),
            "replicate_1": sum(1 for entry in raw["entries"] if not _is_r0(entry["record"])),
            "cache_applicable": ctx["status_counts"].total() + ctx["disabled"] + ctx["degraded"],
            "cache_censored": ctx["censored"],
            "disabled": ctx["disabled"],
            "degraded": ctx["degraded"],
            "cache_key_verified": ctx["key_verified"],
            "provenance_checked": ctx["provenance_checked"],
            "hit_with_prior_miss": ctx["hit_with_prior_miss"],
            "store_failures": ctx["store_failures"],
        },
        "execution_status": dict(statuses),
        "verdict": dict(verdicts),
        "cache_status": dict(observed_status),
        "expected_cache_status": dict(expected_status),
        "cache": {
            "observed": dict(expected_audit.get("observed", {})),
            "expected_total": expected_audit.get("expected_total", {}),
            "status_counts": dict(ctx["status_counts"]),
            "key_verified": ctx["key_verified"],
            "provenance_checked": ctx["provenance_checked"],
            "hit_with_prior_miss": ctx["hit_with_prior_miss"],
            "disable_cells": ctx["disabled"],
            "degraded_cells": ctx["degraded"],
            "censored_cells": ctx["censored"],
            "store_failures": ctx["store_failures"],
        },
        "differential": differential["summary"],
        "gates": gates,
        "statistics_rows": len(statistics_rows),
        "artifact_bytes": sum(
            path.stat().st_size for path in Path(raw["artifacts_dir"]).rglob("*") if path.is_file()
        )
        if raw.get("artifacts_dir")
        else None,
        "budget": {
            "no_cache": {
                "total_compute_minutes": budget["scenarios"]["no_cache"]["total_compute_minutes"],
                "total_wall_minutes": budget["scenarios"]["no_cache"]["total_wall_minutes"],
                "wall_hours_at_concurrency_20": budget["scenarios"]["no_cache"][
                    "wall_hours_at_concurrency_20"
                ],
            },
            "measured_cache": {
                "total_compute_minutes": budget["scenarios"]["measured_cache"][
                    "total_compute_minutes"
                ],
                "total_wall_minutes": budget["scenarios"]["measured_cache"]["total_wall_minutes"],
                "wall_hours_at_concurrency_20": budget["scenarios"]["measured_cache"][
                    "wall_hours_at_concurrency_20"
                ],
            },
            "worst_case": {
                "total_compute_minutes": budget["scenarios"]["worst_case"]["total_compute_minutes"],
                "total_wall_minutes": budget["scenarios"]["worst_case"]["total_wall_minutes"],
                "wall_hours_at_concurrency_20": budget["scenarios"]["worst_case"][
                    "wall_hours_at_concurrency_20"
                ],
            },
        },
        "notes": [
            "All values are recomputed from raw JSONL, frozen CSVs and the manifest; "
            "no runner-reported summary is used.",
            "Wall-clock percentiles are right-censored: only COMPLETED cells contribute; "
            "timeouts are never substituted with the 60 s limit.",
            "replicate==1 differential replicas are excluded from all timing statistics.",
            "Infrastructure ERROR cells are listed separately and do not by themselves "
            "fail PASS.",
        ],
    }
    return result


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def _find_validation_artifact(artifacts_dir):
    matches = sorted(Path(artifacts_dir).glob("**/v6_cache_validation_protocol_validation.json"))
    if not matches:
        return {"found": False, "PASS": None, "path": None, "duplicate": False}
    path = matches[0]
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"found": True, "PASS": False, "path": str(path), "duplicate": len(matches) > 1}
    return {
        "found": True,
        "PASS": bool(payload.get("PASS")),
        "path": str(path),
        "duplicate": len(matches) > 1,
    }


def run_pipeline(manifest_path, tasks_dir, artifacts_dir, run_id, source_sha):
    """Run the complete audit + budget pipeline and return a payload dict."""
    protocol = load_protocol(manifest_path, tasks_dir)
    raw = load_raw_entries(
        artifacts_dir, protocol["expected_shard_ids"], protocol["manifest"], run_id
    )
    raw["artifacts_dir"] = str(artifacts_dir)
    raw_entries = []
    for entry in raw["entries"]:
        annotated = dict(entry)
        annotated["task"] = protocol["tasks"].get(str(entry["record"].get("task_id")))
        raw_entries.append(annotated)
    audit = audit_entries(raw_entries, protocol, run_id, source_sha)
    entries_by_task = {}
    for entry in raw_entries:
        task_id = str(entry["record"].get("task_id"))
        if task_id not in entries_by_task:
            entries_by_task[task_id] = entry
    differential = audit_differential(
        protocol["manifest"],
        protocol["tasks"],
        protocol["shard_of_task"],
        entries_by_task,
    )
    expected_audit = audit_expected_cache(
        protocol["manifest"], protocol["tasks"], raw_entries
    )
    audited = audit["audited"]
    audited_r0 = [record for record in audited if _is_r0(record)]
    statistics_rows = build_statistics(audited_r0)
    validation_artifact = _find_validation_artifact(artifacts_dir)

    # Gates are needed by the budget block; compute them once in a helper.
    provisional_errors = (
        protocol["errors"]
        + raw["errors"]
        + audit["errors"]
        + expected_audit["errors"]
        + differential["errors"]
    )
    provisional_codes = _codes(provisional_errors)
    ctx = audit["ctx"]
    audit_gates = {
        "CACHE_INTEGRITY_PASS": (
            _gate_codes(provisional_codes, CACHE_CODES)
            and ctx["disabled"] == 0
            and ctx["degraded"] == 0
        ),
        "CACHE_DIFFERENTIAL_PASS": differential["summary"]["CACHE_DIFFERENTIAL_PASS"],
    }
    budget = compute_budget(
        raw_entries,
        protocol["manifest"],
        {
            "raw_bytes": raw["raw_bytes"],
            "raw_records": len(raw["entries"]),
        },
        audit_gates,
        {
            "run_id": str(run_id),
            "source_sha": source_sha,
            "artifacts": str(artifacts_dir),
            "manifest": str(manifest_path),
        },
    )
    result = assemble_result(
        protocol,
        raw,
        audit,
        expected_audit,
        differential,
        statistics_rows,
        budget,
        run_id,
        source_sha,
        validation_artifact,
    )
    return {
        "result": result,
        "audited": audited,
        "statistics_rows": statistics_rows,
        "differential": differential["document"],
        "budget": budget,
        "raw": raw,
        "protocol": protocol,
    }


def write_outputs(payload, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "audited_records.jsonl").open("w", encoding="utf-8") as handle:
        for record in payload["audited"]:
            handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
    (out_dir / "cache_validation_audit.json").write_text(
        json.dumps(payload["result"], indent=2, sort_keys=True, default=str) + "\n"
    )
    rows = payload["statistics_rows"]
    with (out_dir / "cache_statistics.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=STAT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    (out_dir / "differential_pairs.json").write_text(
        json.dumps(payload["differential"], indent=2, sort_keys=True, default=str) + "\n"
    )
    (out_dir / "budget_projection.json").write_text(
        json.dumps(payload["budget"], indent=2, sort_keys=True, default=str) + "\n"
    )
    return [
        out_dir / "audited_records.jsonl",
        out_dir / "cache_validation_audit.json",
        out_dir / "cache_statistics.csv",
        out_dir / "differential_pairs.json",
        out_dir / "budget_projection.json",
    ]


# ---------------------------------------------------------------------------
# Synthetic fixtures / self-test
# ---------------------------------------------------------------------------
FIXTURE_FAMILIES = [
    "F01_sequential_sparse",
    "F02_sequential_dense",
    "F03_deep_inspection",
    "F04_alternative_routes",
    "F05_routes_inspections",
    "F06_parallel_parts",
    "F07_shared_contention",
    "F08_mixed_visibility",
    "F09_deep_fault_stages",
    "F10_bounded_rework",
    "F11_resource_cycle",
    "F12_sparse_cycle",
]
FIXTURE_RUN_ID = "99000000001"
FIXTURE_SOURCE_SHA = "b" * 40
FIXTURE_PYTHON = "3.13.5"
FIXTURE_BUILDER_SHA = hashlib.sha256(b"fixture scg builder").hexdigest()


def _fixture_hash(*parts):
    return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()


def _fixture_key_material(model_hash):
    return {
        "format": CACHE_KEY_FORMAT,
        "format_version": CACHE_KEY_FORMAT_VERSION,
        "model_hash": model_hash,
        "source_sha": FIXTURE_SOURCE_SHA,
        "scg_builder_sha": FIXTURE_BUILDER_SHA,
        "semantics_version": CACHE_SEMANTICS_VERSION,
        "python": "3.13",
        "serialization": CACHE_SERIALIZATION,
    }


def build_fixture(root):
    """Create a synthetic protocol + artifact tree that mirrors the real shape."""
    root = Path(root)
    tasks_dir = root / "protocol" / "v6_cache_validation_tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    artifacts = root / "work" / f"v6_cache_validation_{FIXTURE_RUN_ID}"
    artifacts.mkdir(parents=True, exist_ok=True)

    csv_fields = [
        "task_id",
        "protocol_version",
        "experiment_id",
        "experiment",
        "benchmark_family",
        "stratum",
        "base_model_id",
        "model_id",
        "bounded_problem_id",
        "configuration_id",
        "seed",
        "replicate",
        "L",
        "H",
        "mode",
        "timeout_s",
        "expected_model_hash",
        "expected_structure_hash",
        "cache_group",
        "expected_cache_status",
        "params",
    ]
    differential_pairs = []
    for shard, family in enumerate(FIXTURE_FAMILIES):
        models = {}
        for kind in ("lsweep", "wip1", "wip2", "wip3", "difficult"):
            model_hash = _fixture_hash("model", family, kind)
            models[kind] = {
                "model_hash": model_hash,
                "model_id": f"v6:{family}:{model_hash[:20]}",
                "structure_hash": _fixture_hash("structure", model_hash),
                "bounded_problem_id": f"bp6-{model_hash[:20]}",
                "cache_group": model_hash,
            }
        seed = 100000 + shard * 137
        rows = []
        specs = [
            ("lsweep", "L_low_first", 3, 54.0, "first", 0, "miss", "C_Lsweep"),
            ("lsweep", "L_mid_first", 6, 54.0, "first", 0, "hit", "C_Lsweep"),
            ("lsweep", "L_high_complete", 14, 54.0, "complete", 0, "hit", "C_Lsweep"),
            ("lsweep", "L_low_first_replica", 3, 54.0, "first", 1, "hit", "C_Lsweep"),
            ("wip1", "WIP_1", 38, 162.0, "first", 0, "miss", "B_wip"),
            ("wip2", "WIP_2", 38, 162.0, "first", 0, "miss", "B_wip"),
            ("wip3", "WIP_3", 38, 162.0, "first", 0, "miss", "B_wip"),
            ("difficult", "difficult_first", 50, 216.0, "first", 0, "miss", "D_difficult"),
        ]
        for index, (kind, stratum, L, H, mode, replicate, expected_status, experiment) in enumerate(specs):
            model = models[kind]
            configuration_id = f"cfg6-{_fixture_hash('cfg', shard, index)[:20]}"
            rows.append(
                {
                    "task_id": configuration_id,
                    "protocol_version": "v6-cache-validation-1",
                    "experiment_id": f"v6-cache-validation-1-{experiment}",
                    "experiment": experiment,
                    "benchmark_family": family,
                    "stratum": stratum,
                    "base_model_id": model["model_id"],
                    "model_id": model["model_id"],
                    "bounded_problem_id": model["bounded_problem_id"],
                    "configuration_id": configuration_id,
                    "seed": seed,
                    "replicate": replicate,
                    "L": L,
                    "H": H,
                    "mode": mode,
                    "timeout_s": 60,
                    "expected_model_hash": model["model_hash"],
                    "expected_structure_hash": model["structure_hash"],
                    "cache_group": model["cache_group"],
                    "expected_cache_status": expected_status,
                    "params": json.dumps(
                        {"seed": seed, "v6_family": family}, sort_keys=True
                    ),
                    "kind": kind,
                }
            )
        differential_pairs.append([rows[0]["task_id"], rows[3]["task_id"]])
        with (tasks_dir / f"shard_{shard:03d}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=csv_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        # ---- raw artifact ------------------------------------------------
        artifact_dir = artifacts / f"v6-cv-{shard}-{FIXTURE_RUN_ID}"
        raw_dir = artifact_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "checkpoints").mkdir(exist_ok=True)
        (artifact_dir / "attempts").mkdir(exist_ok=True)
        records = []
        for index, row in enumerate(rows):
            model = models[row["kind"]]
            cache_status = row["expected_cache_status"]
            key_material = _fixture_key_material(model["model_hash"])
            cache_key = cache_key_from_material(key_material)
            if cache_status == "miss":
                runtime_build = 2.0 + shard * 0.01
                runtime_load = 0.0
                wall = 4.0 + shard * 0.01 + index * 0.01
                provenance = {
                    "cache_status": "miss",
                    "key": cache_key,
                    "key_material": key_material,
                    "reason": "stored",
                    "store_ok": True,
                    "build_s": runtime_build,
                    "load_s": 0.0,
                    "store_s": 0.12,
                    "payload_sha256": _fixture_hash("payload", cache_key),
                    "file_bytes": 8192 + index * 512,
                    "serializer": CACHE_SERIALIZATION,
                }
                counters_runtime_scg = runtime_build
            else:
                runtime_build = 0.0
                runtime_load = 0.05
                wall = 2.4 + shard * 0.01 + index * 0.01
                provenance = {
                    "cache_status": "hit",
                    "key": cache_key,
                    "key_material": key_material,
                    "reason": "verified",
                    "build_s": None,
                    "load_s": runtime_load,
                    "store_s": None,
                    "payload_sha256": _fixture_hash("payload", cache_key),
                    "file_bytes": 8192 + index * 512,
                    "serializer": CACHE_SERIALIZATION,
                }
                counters_runtime_scg = 0.0
            # Same model => identical SCG => identical non-runtime counters.
            counters = {
                "scg_classes": 120 + shard,
                "scg_edges": 180 + shard,
                "sync_states": 25 + shard,
                "candidate_words": 0,
                "temporal_states": 9 + shard,
                "trace_records": 11 + shard,
                "terminal_pairs": 0,
                "lp_calls": 0,
                "runtime_scg": counters_runtime_scg,
                "runtime_sync": 0.5,
                "runtime_expansion": 0.3,
                "runtime_lp": 0.01,
            }
            if cache_status == "hit":
                counters["scg_cache_load_s"] = runtime_load
            record = {
                "task_id": row["task_id"],
                "tag": row["task_id"],
                "protocol_version": row["protocol_version"],
                "experiment": row["experiment"],
                "experiment_id": row["experiment_id"],
                "benchmark_family": family,
                "model_id": row["model_id"],
                "model": row["model_id"],
                "bounded_problem_id": row["bounded_problem_id"],
                "configuration_id": row["configuration_id"],
                "configuration_hash": _fixture_hash("config", row["task_id"]),
                "seed": row["seed"],
                "replicate": row["replicate"],
                "replicate_id": row["replicate"],
                "L": row["L"],
                "H": row["H"],
                "mode": row["mode"],
                "timeout_s": row["timeout_s"],
                "time_limit_sec": row["timeout_s"],
                "params": json.loads(row["params"]),
                "model_hash": model["model_hash"],
                "structure_hash": model["structure_hash"],
                "verdict": "VER",
                "execution_status": "COMPLETED",
                "analysis_status": "CONCLUSIVE",
                "inconclusive_reason": None,
                "failure_stage": None,
                "enumeration_complete": True,
                "critical_pair_count": 0,
                "counters": counters,
                "runtime_s": wall - 0.05,
                "runtime_sec": wall - 0.05,
                "wall_s": wall,
                "peak_rss_mb": 60.0 + shard + index,
                "python": FIXTURE_PYTHON,
                "commit_sha": FIXTURE_SOURCE_SHA,
                "source_code_hash": FIXTURE_SOURCE_SHA,
                "workflow_run_id": FIXTURE_RUN_ID,
                "job_id": str(9000000000 + shard),
                "logical_job_key": "run",
                "shard_id": shard,
                "shard_execution_id": f"{FIXTURE_RUN_ID}:{shard:03d}",
                "raw_artifact": f"v6-cv-{shard}-{FIXTURE_RUN_ID}/raw/shard_{shard:03d}.jsonl",
                "model_attributes": {
                    "WIP": 1 + (index % 3),
                    "benchmark_family": family,
                    "number_of_parts": 3,
                },
                "WIP": 1 + (index % 3),
                "scg_cache_status": cache_status,
                "scg_cache_key": cache_key,
                "scg_cache_provenance": provenance,
                "runtime_scg_build_s": runtime_build,
                "runtime_scg_load_s": 0.0 if cache_status == "miss" else runtime_load,
                "validation_status": "PASS",
                "partial_workload": False,
            }
            record["raw_record_hash"] = hashlib.sha256(
                json.dumps(record, default=str, sort_keys=True).encode()
            ).hexdigest()
            records.append(record)
        with (raw_dir / f"shard_{shard:03d}.jsonl").open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
    # Protocol-validation artifact (uploaded by the validate job).
    validation_dir = artifacts / f"v6-cv-validation-{FIXTURE_RUN_ID}"
    validation_dir.mkdir(parents=True, exist_ok=True)
    (validation_dir / "v6_cache_validation_protocol_validation.json").write_text(
        json.dumps({"PASS": True, "protocol_version": "v6-cache-validation-1"}, indent=2) + "\n"
    )
    manifest = {
        "version": "v6-cache-validation-1",
        "families": len(FIXTURE_FAMILIES),
        "family_names": FIXTURE_FAMILIES,
        "shards": len(FIXTURE_FAMILIES),
        "tasks": len(FIXTURE_FAMILIES) * 8,
        "tasks_per_family": 8,
        "tasks_per_shard": 8,
        "cell_timeout_seconds": 60,
        "differential_pairs": differential_pairs,
        "cache_key_components": {
            "format": CACHE_KEY_FORMAT,
            "format_version": CACHE_KEY_FORMAT_VERSION,
            "semantics_version": CACHE_SEMANTICS_VERSION,
            "python": CACHE_PYTHON_MAJOR_MINOR,
            "serialization": CACHE_SERIALIZATION,
        },
        "cache_policy": {"entry": "one canonical JSON file per cache key"},
        "expected_cache": {
            "total": {"miss": 60, "hit": 36},
            "per_shard": {"miss": 5, "hit": 3},
            "per_family": {family: {"miss": 5, "hit": 3} for family in FIXTURE_FAMILIES},
        },
    }
    manifest_path = root / "protocol" / "v6_cache_validation_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {
        "root": root,
        "manifest": manifest_path,
        "tasks": tasks_dir,
        "artifacts": artifacts,
        "run_id": FIXTURE_RUN_ID,
        "source_sha": FIXTURE_SOURCE_SHA,
    }


def _fixture_shard_file(paths, shard):
    matches = sorted(Path(paths["artifacts"]).glob(f"**/raw/shard_{shard:03d}.jsonl"))
    if len(matches) != 1:
        raise RuntimeError(f"fixture shard {shard} has {len(matches)} raw files")
    return matches[0]


def _rewrite_fixture_shard(paths, shard, mutate):
    path = _fixture_shard_file(paths, shard)
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    mutate(records)
    path.write_text(
        "".join(json.dumps(record, default=str, sort_keys=True) + "\n" for record in records)
    )


def _rehash(record):
    record.pop("raw_record_hash", None)
    record["raw_record_hash"] = hashlib.sha256(
        json.dumps(record, default=str, sort_keys=True).encode()
    ).hexdigest()


def _inject_missing(paths):
    _rewrite_fixture_shard(paths, 3, lambda records: records.pop(4))


def _inject_duplicate(paths):
    _rewrite_fixture_shard(paths, 4, lambda records: records.append(copy.deepcopy(records[0])))


def _inject_hash_tamper(paths):
    def mutate(records):
        records[1]["wall_s"] = float(records[1]["wall_s"]) + 1.0

    _rewrite_fixture_shard(paths, 5, mutate)


def _inject_field_mismatch(paths):
    def mutate(records):
        records[0]["mode"] = "complete"
        _rehash(records[0])

    _rewrite_fixture_shard(paths, 6, mutate)


def _inject_hit_without_miss(paths):
    def mutate(records):
        record = records[1]
        material = dict(record["scg_cache_provenance"]["key_material"])
        material["nonce"] = "orphan-hit-without-miss"
        record["scg_cache_provenance"]["key_material"] = material
        key = cache_key_from_material(material)
        record["scg_cache_key"] = key
        record["scg_cache_provenance"]["key"] = key
        _rehash(record)

    _rewrite_fixture_shard(paths, 7, mutate)


def _inject_differential_counter_mismatch(paths):
    def mutate(records):
        records[0]["counters"]["scg_classes"] = records[0]["counters"]["scg_classes"] + 1
        _rehash(records[0])

    _rewrite_fixture_shard(paths, 8, mutate)


FIXTURE_CASES = [
    ("clean", None, []),
    ("missing_record", _inject_missing, ["TASK_MISSING"]),
    ("duplicate_record", _inject_duplicate, ["TASK_DUPLICATE"]),
    ("raw_hash_tampered", _inject_hash_tamper, ["RAW_HASH_MISMATCH"]),
    ("frozen_field_mismatch", _inject_field_mismatch, ["FIELD_MISMATCH"]),
    ("hit_without_prior_miss", _inject_hit_without_miss, ["CACHE_HIT_WITHOUT_MISS"]),
    (
        "differential_counter_mismatch",
        _inject_differential_counter_mismatch,
        ["DIFF_COUNTER_MISMATCH"],
    ),
]


def run_self_test(out_root=None):
    """Run the clean fixture + six injected-fault fixtures; return exit code."""
    base = Path(out_root) if out_root else Path(tempfile.mkdtemp(prefix="v6cv_selftest_"))
    base.mkdir(parents=True, exist_ok=True)
    cases = []
    ok = True
    for name, mutate, expected_codes in FIXTURE_CASES:
        root = base / name
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        paths = build_fixture(root)
        if mutate is not None:
            mutate(paths)
        payload = run_pipeline(
            paths["manifest"],
            paths["tasks"],
            paths["artifacts"],
            paths["run_id"],
            paths["source_sha"],
        )
        result = payload["result"]
        errors = result["errors"]
        codes = sorted(_codes(errors))
        matched = [code for code in expected_codes if code in codes]
        case = {
            "case": name,
            "mutation": mutate.__name__ if mutate else None,
            "expected_error_codes": expected_codes,
            "caught": all(code in codes for code in expected_codes),
            "PASS": result["PASS"],
            "acceptance": result["acceptance"],
            "error_count": len(errors),
            "exit_code_would_be": 0 if result["PASS"] else 1,
            "observed_error_codes": codes,
            "error_examples": [message for message in errors if any(
                message.startswith(code + ":") or message == code for code in expected_codes
            )][:4],
        }
        if name == "clean":
            out_dir = root / "audit_out"
            written = write_outputs(payload, out_dir)
            case["output_files"] = [str(path) for path in written]
            case["outputs_exist"] = all(path.is_file() for path in written)
            case["output_files_parsed"] = {}
            for path in written:
                if path.suffix == ".json":
                    json.loads(path.read_text())
                    case["output_files_parsed"][path.name] = True
                elif path.suffix == ".csv":
                    with path.open() as handle:
                        case["output_files_parsed"][path.name] = len(list(csv.DictReader(handle))) > 0
                else:
                    case["output_files_parsed"][path.name] = (
                        len(path.read_text().splitlines()) == len(payload["audited"])
                    )
            gates = result["gates"]
            case["clean_gates"] = {
                "TASK_RAW_MAPPING_PASS": gates["TASK_RAW_MAPPING_PASS"],
                "RECORD_INTEGRITY_PASS": gates["RECORD_INTEGRITY_PASS"],
                "EXECUTION_STATUS_PASS": gates["EXECUTION_STATUS_PASS"],
                "CACHE_INTEGRITY_PASS": gates["CACHE_INTEGRITY_PASS"],
                "CACHE_DIFFERENTIAL_PASS": gates["CACHE_DIFFERENTIAL_PASS"],
                "EXPECTED_CACHE_COUNTS_PASS": gates["EXPECTED_CACHE_COUNTS_PASS"],
                "PROTOCOL_VALIDATION_PASS": gates["PROTOCOL_VALIDATION_PASS"],
            }
            case["clean_all_gates_true"] = all(
                value is True
                for key, value in case["clean_gates"].items()
                if key != "PROTOCOL_VALIDATION_PASS"
            ) and case["clean_gates"]["PROTOCOL_VALIDATION_PASS"] is True
            case["budget_scenarios"] = {
                key: {
                    "total_compute_minutes": payload["budget"]["scenarios"][key][
                        "total_compute_minutes"
                    ],
                    "wall_hours_at_concurrency_20": payload["budget"]["scenarios"][key][
                        "wall_hours_at_concurrency_20"
                    ],
                }
                for key in ("no_cache", "measured_cache", "worst_case")
            }
            case["budget_complete"] = payload["budget"]["gates"]["BUDGET_COMPLETE"]
            case["statistics_rows"] = len(payload["statistics_rows"])
            clean_ok = (
                result["PASS"]
                and case["outputs_exist"]
                and case["clean_all_gates_true"]
                and case["budget_complete"]
            )
            case["clean_case_ok"] = clean_ok
            ok = ok and clean_ok
        else:
            clean_ok = case["caught"] and not result["PASS"]
            case["fault_case_ok"] = clean_ok
            ok = ok and clean_ok
        cases.append(case)
    summary = {
        "PASS": ok,
        "fixtures_root": str(base),
        "clean_fixture_PASS": any(
            case["case"] == "clean" and case["PASS"] for case in cases
        ),
        "injected_faults_caught": {
            case["case"]: case["caught"] for case in cases if case["case"] != "clean"
        },
        "cases": cases,
    }
    (base / "self_test_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n"
    )
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Independent audit + budget projection for v6 cache-validation."
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--source-sha")
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="run the synthetic self-test (clean PASS + six injected faults)",
    )
    parser.add_argument(
        "--fixtures-out",
        type=Path,
        help="optional directory for self-test fixtures (default: a temp dir)",
    )
    args = parser.parse_args(argv)

    if args.fixtures:
        return run_self_test(args.fixtures_out)

    missing = [
        name
        for name in ("manifest", "tasks", "artifacts", "run_id", "source_sha", "out")
        if getattr(args, name) is None
    ]
    if missing:
        parser.error(f"missing required arguments: {', '.join('--' + name for name in missing)}")

    payload = run_pipeline(
        args.manifest, args.tasks, args.artifacts, args.run_id, args.source_sha
    )
    write_outputs(payload, args.out)
    result = payload["result"]
    summary = {
        "PASS": result["PASS"],
        "acceptance": result["acceptance"],
        "out": str(args.out),
        "errors": len(result["errors"]),
        "findings": len(result["findings"]),
        "counts": result["counts"],
        "gates": {
            key: value
            for key, value in result["gates"].items()
            if key != "INFRASTRUCTURE_ERRORS"
        },
        "differential": result["differential"],
        "budget": result["budget"],
        "files": [
            str(args.out / "audited_records.jsonl"),
            str(args.out / "cache_validation_audit.json"),
            str(args.out / "cache_statistics.csv"),
            str(args.out / "differential_pairs.json"),
            str(args.out / "budget_projection.json"),
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    if result["errors"]:
        print("\nFirst errors:", file=sys.stderr)
        for message in result["errors"][:15]:
            print("  " + message, file=sys.stderr)
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
