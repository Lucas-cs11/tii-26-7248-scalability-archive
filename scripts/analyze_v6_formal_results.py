#!/usr/bin/env python3
"""Normalize, cross-check and statistically summarize v6 result artifacts.

The same script consumes the frozen 288-cell Pilot (default) and, once the
Modal 8,280-cell Formal run exists, the Formal raw artifacts:

    # Pilot self-check (default arguments)
    python scripts/analyze_v6_formal_results.py

    # Formal run (after the raw download)
    python scripts/analyze_v6_formal_results.py \
        --tasks-dir protocol/v6_formal_tasks \
        --manifest protocol/v6_formal_manifest.json \
        --raw-dir work/v6_formal_run_<run-id> \
        --out work/v6_analysis_formal

Statistical discipline implemented here (also documented in
``docs/v6_figure6_spec.md``):

* Right censoring: P50/P90/P95 of time are computed *only* over COMPLETED
  cells.  A TIMEOUT cell is never assigned the 60 s cap as if it were a
  completion time.  Its wall clock is reported separately as ``wall_s`` with
  ``wall_censored_at_s`` and the stopping ``failure_stage``; the cell only
  contributes a lower bound (>= cap) to any cost statement.
* INC ("completed but inconclusive") is its own denominator.  It is neither a
  zero-work outcome nor a VER/AMB, and it is counted separately everywhere.
* Every group reports n_total / n_completed / n_timeout / n_inc / n_error.
* Stage timings are additive only along the pipeline (SCG -> synchronizer ->
  history expansion -> LP); nested sub-timers (e.g. LP build/solve inside LP)
  are never summed on top of their parent.
* Cell compute time is an implementation/hardware-dependent measurement of
  this runner, not an intrinsic model property; accumulated sums are reported
  as cost accounting only and never as an independent model score.  No
  speed-up is computed across environments or across protocol versions.
* Every figure/table can be recomputed from the emitted CSV/JSON files.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_SCHEMA = "v6-analysis-statistics-1"
ANALYSIS_VERSION = "v6-analysis-1"

# ---------------------------------------------------------------------------
# Pipeline stage ordering (used for censoring-aware gating of counters).
# ---------------------------------------------------------------------------
STAGE_ORDER = {
    "MODEL_GENERATION": 0,
    "MODEL_GENERATED": 1,
    "MODEL_VALIDATED": 2,
    "SCG_COMPLETE": 3,
    "SYNCHRONIZER_COMPLETE": 4,
    "HISTORY_ANALYSIS_PARTIAL": 5,
    "FIRST_CERTIFICATE_COMPLETE": 6,
    "COMPLETE": 7,
    "BOUNDED_ANALYSIS": 7,
}
PRESYNC_STAGES = {"MODEL_GENERATION", "MODEL_GENERATED", "MODEL_VALIDATED",
                  "SCG_COMPLETE"}
SYNC_ONLY_STAGES = {"SYNCHRONIZER_COMPLETE"}
SCG_INCONCLUSIVE_REASONS = {
    "SCG_STATE_LIMIT", "COMPLETION_UNREACHABLE", "ZERO_TIME_UNOBSERVABLE_CYCLE",
}

SYNC_COUNTERS = (
    "sync_states", "sync_edges", "candidate_words", "candidate_words_processed",
    "sync_transition_calls", "sync_successors_calls", "sync_words_explored",
    "sync_closure_N_calls", "sync_closure_F_calls", "sync_closure_any_calls",
    "runtime_sync", "runtime_unobservable_closure",
)
EXPANSION_COUNTERS = (
    "temporal_states", "trace_records", "terminal_pairs",
    "candidate_words_processed", "terminal_normal_records",
    "terminal_faulty_records", "exp_nodes_created", "exp_edges",
    "exp_record_checks", "exp_worklist_pops", "runtime_expansion",
    "runtime_certificate",
)
LP_COUNTERS = (
    "lp_calls", "lp_builds", "lp_solves", "lp_feasible", "lp_infeasible",
    "lp_unknown", "confirmed_critical_pairs", "enclosure_rejected_pairs",
    "runtime_lp", "runtime_lp_build", "runtime_lp_solve",
)
STAGE_COUNTER_KEYS = tuple(dict.fromkeys(
    ("scg_classes", "scg_edges") + SYNC_COUNTERS + EXPANSION_COUNTERS + LP_COUNTERS
))

# 202 COMPLETED / 86 TIMEOUT / 80 VER / 87 AMB / 35 INC for the 288-cell Pilot
# are *derived* here, not asserted.  The expected values live in the Pilot
# audit (work/v6_optimized_pilot_audit/pilot_audit.json) and are compared by
# ``--reference-audit`` for the self-check only.
NORMALIZED_FIELDS = [
    "task_id", "experiment", "benchmark_family", "stratum", "series",
    "design_level", "base_model_id", "model_id", "seed", "replicate",
    "protocol_version", "WIP", "L", "H", "mode", "timeout_s",
    "execution_status", "verdict", "analysis_status", "inconclusive_reason",
    "failure_stage", "timeout_stage", "reached_stage", "reached_stage_rank",
    "is_completed", "is_timeout", "is_error", "entered_depth_L_stage",
    "n_places", "n_transitions", "n_arcs", "size_places_transitions",
    "number_of_parts", "operations_per_part", "resource_count",
    "inspection_locations", "route_choice_locations", "rework_locations",
    "observable_ratio", "cycle_presence", "verified_reachable_concurrency",
    "scg_classes", "scg_edges", "scg_limit", "scg_limit_hit",
    "sync_states", "sync_edges", "candidate_words", "candidate_words_processed",
    "temporal_states", "trace_records", "terminal_pairs", "lp_calls",
    "lp_feasible", "lp_infeasible", "lp_unknown", "confirmed_critical_pairs",
    "runtime_scg_s", "runtime_scg_load_s", "runtime_sync_s",
    "runtime_unobservable_closure_s", "runtime_expansion_s",
    "runtime_certificate_s", "runtime_lp_s", "runtime_lp_build_s",
    "runtime_lp_solve_s", "runtime_total_s", "runtime_compute_s",
    "wall_s", "wall_censored_at_s", "cap_utilization",
    "peak_rss_mb", "scg_cache_status", "cache_hit", "partial_workload",
    "l_rank", "n_l_levels",
    "l_sensitive_candidate", "l_sensitive_state", "l_sensitive_verdict",
]

CENSOR_NOTE = (
    "P50/P90/P95 use COMPLETED cells only; TIMEOUT cells are right-censored at "
    "the {cap:g} s cell cap and are reported as timeout counts/stages, never as "
    "completion times. INC is a separate completed-but-inconclusive denominator."
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def percentile(values, probability):
    """Nearest-rank percentile (same convention as the Pilot audit)."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    index = max(0, math.ceil(probability * len(values)) - 1)
    return values[index]


def observed(values):
    return [v for v in values if v is not None]


def median(values):
    values = observed(values)
    return statistics.median(values) if values else None


def fraction(numerator, denominator):
    return numerator / denominator if denominator else None


def fmt(value, digits=6):
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, digits)
    return value


def write_csv(path, rows):
    rows = list(rows)
    if not rows:
        path.write_text("")
        return
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields,
                                extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: fmt(row.get(key)) for key in fields})


def jsonable(value):
    """Recursively round floats so statistics.json stays diff-friendly."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, float):
        return fmt(value)
    return value


# ---------------------------------------------------------------------------
# Task / record loading and normalization
# ---------------------------------------------------------------------------
def load_tasks(tasks_dir):
    rows = []
    for path in sorted(tasks_dir.glob("shard_*.csv")):
        with path.open() as handle:
            rows.extend(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"no shard_*.csv tasks found under {tasks_dir}")
    return {row["task_id"]: row for row in rows}


def load_records(raw_dir):
    paths = sorted(raw_dir.glob("*/raw/shard_*.jsonl"))
    if not paths:
        paths = sorted(raw_dir.glob("**/shard_*.jsonl"))
    if not paths:
        paths = sorted(raw_dir.glob("**/*.jsonl"))
    records = []
    for path in paths:
        with path.open() as handle:
            for line in handle:
                if line.strip():
                    records.append(json.loads(line))
    if not records:
        raise SystemExit(f"no JSONL raw records found under {raw_dir}")
    return paths, records


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def derive_design(task):
    """Derive stratum/series/level for Pilot (explicit) and Formal (derived)."""
    experiment = task.get("experiment")
    base = task.get("base_model_id") or ""
    params = {}
    try:
        params = json.loads(task.get("params") or "{}")
    except json.JSONDecodeError:
        params = {}
    wip = _int(params.get("wip_bound"))
    stratum = (task.get("stratum") or "").strip()
    series, level = "", ""
    if experiment == "A_structural":
        match = re.match(r"^(A\d_[a-z]+):l(\d+):s\d+", base)
        if match:
            series, level = match.group(1), f"l{match.group(2)}"
        elif stratum:
            series = "depth" if stratum.startswith("depth") else "observation"
    elif experiment == "B_wip":
        series = "WIP"
        match = re.match(r"^B:([^:]+):s\d+", base)
        if match:
            series = match.group(1)
    elif experiment == "C_Lsweep":
        series = "L_sweep"
        match = re.match(r"^C:([^:]+):s\d+", base)
        if match:
            series = match.group(1)
    elif experiment == "D_difficult":
        series = "difficulty"
        match = re.match(r"^D:([^:]+):l(\d+):s\d+", base)
        if match:
            series = match.group(1)
            level = f"l{match.group(2)}"
    if not stratum:
        if experiment == "B_wip":
            stratum = f"WIP_{wip}" if wip is not None else "WIP_unknown"
        elif experiment == "C_Lsweep":
            stratum = f"L_{task.get('mode', 'unknown')}"
        elif experiment == "A_structural":
            stratum = f"{series}:{level}" if level else series
        elif experiment == "D_difficult":
            stratum = f"level_{level}" if level else "difficult"
        else:
            stratum = experiment or "unknown"
    return stratum, series, level, wip


def normalize_counters(record):
    """Stage-gate counters so unexecuted stages are null, never zero.

    The current runner already checkpoints nulls (commit 835e4bd), but the
    Pilot history contains older artifacts that wrote zero.  Re-applying the
    rule here keeps the normalization independent of which artifact version is
    downloaded and records how many raw zeroes had to be repaired.
    """
    counters = dict(record.get("counters") or {})
    repairs = 0
    status = record.get("execution_status")
    stage = record.get("failure_stage")
    reason = record.get("inconclusive_reason")
    verdict = record.get("verdict")
    if status in (None, "ERROR"):
        return {}, repairs
    drop = set()
    if status == "TIMEOUT_WALL_CLOCK":
        if stage in {"MODEL_GENERATION", "MODEL_GENERATED", "MODEL_VALIDATED"}:
            return {}, repairs
        if stage in PRESYNC_STAGES:
            drop.update(SYNC_COUNTERS + EXPANSION_COUNTERS + LP_COUNTERS)
        elif stage in SYNC_ONLY_STAGES:
            drop.update(EXPANSION_COUNTERS + LP_COUNTERS)
        # HISTORY_ANALYSIS_PARTIAL keeps the checkpointed expansion/LP counters.
    elif verdict == "INC" and reason in SCG_INCONCLUSIVE_REASONS:
        drop.update(SYNC_COUNTERS + EXPANSION_COUNTERS + LP_COUNTERS)
    elif counters.get("candidate_words") == 0:
        # Synchronizer ran and found no candidate word: expansion/LP never ran.
        drop.update(EXPANSION_COUNTERS + LP_COUNTERS)
    for key in drop:
        if key in counters and counters[key] is not None:
            repairs += 1
            counters[key] = None
    return counters, repairs


def reached_stage(record):
    status = record.get("execution_status")
    stage = record.get("failure_stage")
    if status == "COMPLETED":
        if record.get("verdict") == "INC":
            return stage or "SCG"
        return "COMPLETED"
    return stage or "UNKNOWN"


def normalize_record(record, task, manifest):
    stratum, series, level, params_wip = derive_design(task)
    counters, repairs = normalize_counters(record)
    status = record.get("execution_status")
    verdict = record.get("verdict")
    is_timeout = status == "TIMEOUT_WALL_CLOCK"
    is_completed = status == "COMPLETED"
    is_error = not is_timeout and not is_completed
    failure_stage = record.get("failure_stage")
    timeout_s = _float(task.get("timeout_s")) or _float(record.get("timeout_s")) \
        or _float(manifest.get("cell_timeout_seconds"))

    model_attributes = record.get("model_attributes") or {}
    params = {}
    try:
        params = json.loads(task.get("params") or "{}")
    except json.JSONDecodeError:
        params = {}
    wip = _int(record.get("WIP"))
    if wip is None:
        wip = params_wip if params_wip is not None else _int(
            model_attributes.get("WIP"))
    cache_status = record.get("scg_cache_status")
    cache_hit = cache_status == "hit"
    runtime_scg_build = _float(record.get("runtime_scg_build_s"))
    runtime_scg_load = _float(record.get("runtime_scg_load_s"))
    if cache_hit:
        runtime_scg = runtime_scg_load
    elif cache_status in ("miss", "disabled", None):
        runtime_scg = runtime_scg_build
    else:
        runtime_scg = runtime_scg_build

    runtime_verify = _float(record.get("runtime_total_s"))
    runtime_compute = None
    if is_completed:
        parts = [p for p in (runtime_scg, runtime_verify) if p is not None]
        if parts:
            runtime_compute = sum(parts)

    n_places = _int(record.get("n_places"))
    if n_places is None:
        n_places = _int(record.get("number_of_places"))
    n_transitions = _int(record.get("n_transitions"))
    if n_transitions is None:
        n_transitions = _int(record.get("number_of_transitions"))
    n_arcs = _int(record.get("n_arcs"))
    if n_arcs is None:
        n_arcs = _int(record.get("number_of_arcs"))
    size = (n_places + n_transitions) if n_places is not None \
        and n_transitions is not None else None

    stage = reached_stage(record)
    rank = STAGE_ORDER.get(stage)
    entered_l = (rank is not None and rank >= STAGE_ORDER["SYNCHRONIZER_COMPLETE"]) \
        or (stage == "COMPLETED") or (stage == "BOUNDED_ANALYSIS")
    if stage == "SCG" and verdict == "INC":
        entered_l = False
    wall = _float(record.get("wall_s"))
    cap = timeout_s
    row = {
        "task_id": record.get("task_id"),
        "experiment": task.get("experiment") or record.get("experiment"),
        "benchmark_family": task.get("benchmark_family")
        or record.get("benchmark_family"),
        "stratum": stratum,
        "series": series,
        "design_level": level,
        "base_model_id": task.get("base_model_id"),
        "model_id": task.get("model_id") or record.get("model_id"),
        "seed": _int(task.get("seed")),
        "replicate": _int(task.get("replicate")),
        "protocol_version": task.get("protocol_version"),
        "WIP": wip, "L": _int(task.get("L")), "H": _float(task.get("H")),
        "mode": task.get("mode"),
        "timeout_s": cap,
        "execution_status": status,
        "verdict": verdict,
        "analysis_status": record.get("analysis_status"),
        "inconclusive_reason": record.get("inconclusive_reason"),
        "failure_stage": None if is_completed and verdict != "INC" else failure_stage,
        "timeout_stage": failure_stage if is_timeout else None,
        "reached_stage": stage,
        "reached_stage_rank": rank,
        "is_completed": int(is_completed),
        "is_timeout": int(is_timeout),
        "is_error": int(is_error),
        "entered_depth_L_stage": int(bool(entered_l)),
        "n_places": n_places,
        "n_transitions": n_transitions,
        "n_arcs": n_arcs,
        "size_places_transitions": size,
        "number_of_parts": _int(record.get("number_of_parts")),
        "operations_per_part": _int(record.get("operations_per_part")),
        "resource_count": _int(record.get("resource_count")),
        "inspection_locations": model_attributes.get("inspection_locations"),
        "route_choice_locations": model_attributes.get("route_choice_locations"),
        "rework_locations": model_attributes.get("rework_locations"),
        "observable_ratio": _float(record.get("observable_ratio")),
        "cycle_presence": model_attributes.get("cycle_presence"),
        "verified_reachable_concurrency":
            record.get("verified_reachable_concurrency"),
        "scg_classes": counters.get("scg_classes"),
        "scg_edges": counters.get("scg_edges"),
        "scg_limit": _int(record.get("scg_limit")),
        "sync_states": counters.get("sync_states"),
        "sync_edges": counters.get("sync_edges"),
        "candidate_words": counters.get("candidate_words"),
        "candidate_words_processed": counters.get("candidate_words_processed"),
        "temporal_states": counters.get("temporal_states"),
        "trace_records": counters.get("trace_records"),
        "terminal_pairs": counters.get("terminal_pairs"),
        "lp_calls": counters.get("lp_calls"),
        "lp_feasible": counters.get("lp_feasible"),
        "lp_infeasible": counters.get("lp_infeasible"),
        "lp_unknown": counters.get("lp_unknown"),
        "confirmed_critical_pairs": counters.get("confirmed_critical_pairs"),
        "runtime_scg_s": runtime_scg,
        "runtime_scg_load_s": runtime_scg_load,
        "runtime_sync_s": _float(record.get("runtime_synchronizer_s")),
        "runtime_unobservable_closure_s":
            _float(record.get("runtime_unobservable_closure_s")),
        "runtime_expansion_s": _float(record.get("runtime_history_expansion_s")),
        "runtime_certificate_s": _float(record.get("runtime_certificate_s")),
        "runtime_lp_s": (None if counters.get("runtime_lp") is None
                         else _float(counters.get("runtime_lp"))),
        "runtime_lp_build_s": _float(record.get("runtime_lp_build_s")),
        "runtime_lp_solve_s": _float(record.get("runtime_lp_solve_s")),
        "runtime_total_s": runtime_verify,
        "runtime_compute_s": runtime_compute,
        "wall_s": wall,
        "wall_censored_at_s": cap if is_timeout else None,
        "cap_utilization": (wall / cap) if wall is not None and cap else None,
        "peak_rss_mb": _float(record.get("peak_rss_mb")),
        "scg_cache_status": cache_status,
        "cache_hit": int(cache_hit),
        "partial_workload": int(bool(record.get("partial_workload"))),
        "l_rank": None,
        "n_l_levels": None,
        "l_sensitive_candidate": None,
        "l_sensitive_state": None,
        "l_sensitive_verdict": None,
        "_counter_repairs": repairs,
    }
    if row["scg_classes"] is not None and row["scg_limit"]:
        row["scg_limit_hit"] = int(row["scg_classes"] >= row["scg_limit"])
    else:
        row["scg_limit_hit"] = None
    return row


# ---------------------------------------------------------------------------
# Group summaries (denominators + censoring-aware quantiles)
# ---------------------------------------------------------------------------
def summarize(rows, keys, cap, note_scope="group"):
    rows = list(rows)
    statuses = Counter(row["execution_status"] for row in rows)
    verdicts = Counter(str(row["verdict"]) for row in rows if row["is_completed"])
    completed = [row for row in rows if row["is_completed"]]
    timeout = statuses.get("TIMEOUT_WALL_CLOCK", 0)
    error = len(rows) - statuses.get("COMPLETED", 0) - timeout
    walls = observed(row["wall_s"] for row in completed)
    computes = observed(row["runtime_compute_s"] for row in completed)
    rss_all = observed(row["peak_rss_mb"] for row in rows)
    rss_completed = observed(row["peak_rss_mb"] for row in completed)
    utils = observed(row["cap_utilization"] for row in completed)
    stages = Counter(row["failure_stage"] for row in rows
                     if row["failure_stage"] and not row["is_completed"])
    row = dict(keys)
    row.update({
        "n_total": len(rows),
        "n_completed": statuses.get("COMPLETED", 0),
        "n_timeout": timeout,
        "n_error": error,
        "n_inc": verdicts.get("INC", 0),
        "n_ver": verdicts.get("VER", 0),
        "n_amb": verdicts.get("AMB", 0),
        "n_conclusive": verdicts.get("VER", 0) + verdicts.get("AMB", 0),
        "completion_fraction": fraction(statuses.get("COMPLETED", 0), len(rows)),
        "timeout_fraction": fraction(timeout, len(rows)),
        "error_fraction": fraction(error, len(rows)),
        "inc_fraction_all": fraction(verdicts.get("INC", 0), len(rows)),
        "inc_fraction_completed": fraction(verdicts.get("INC", 0),
                                           statuses.get("COMPLETED", 0)),
        "conclusive_fraction_all": fraction(
            verdicts.get("VER", 0) + verdicts.get("AMB", 0), len(rows)),
        "conclusive_fraction_completed": fraction(
            verdicts.get("VER", 0) + verdicts.get("AMB", 0),
            statuses.get("COMPLETED", 0)),
        "wall_p50_completed_s": percentile(walls, 0.50),
        "wall_p90_completed_s": percentile(walls, 0.90),
        "wall_p95_completed_s": percentile(walls, 0.95),
        "runtime_p50_completed_s": percentile(computes, 0.50),
        "runtime_p90_completed_s": percentile(computes, 0.90),
        "runtime_p95_completed_s": percentile(computes, 0.95),
        "cap_utilization_p50_completed": percentile(utils, 0.50),
        "cap_utilization_p90_completed": percentile(utils, 0.90),
        "peak_rss_p50_all_mb": percentile(rss_all, 0.50),
        "peak_rss_p95_all_mb": percentile(rss_all, 0.95),
        "peak_rss_p50_completed_mb": percentile(rss_completed, 0.50),
        "peak_rss_p95_completed_mb": percentile(rss_completed, 0.95),
        "n_observed_wall": len(walls),
        "n_observed_runtime": len(computes),
        "n_observed_peak_rss": len(rss_all),
        "timeout_failure_stages": json.dumps(dict(sorted(stages.items())),
                                             sort_keys=True),
        "timeout_censored_at_s": cap,
        "censoring_note": (
            f"{timeout}/{len(rows)} cells right-censored at {cap:g} s; "
            f"P50/P90/P95 over {len(walls)} COMPLETED cell(s); "
            f"INC counted separately (n={verdicts.get('INC', 0)})"
        ),
        "quantile_method": "nearest-rank over COMPLETED cells only",
    })
    # Metric medians with explicit observed denominators.
    for field in ("scg_classes", "scg_edges", "sync_states", "sync_edges",
                  "candidate_words", "temporal_states", "trace_records",
                  "terminal_pairs", "lp_calls", "confirmed_critical_pairs",
                  "size_places_transitions", "peak_rss_mb"):
        values = observed(row_[field] for row_ in rows)
        row[f"{field}_n"] = len(values)
        row[f"{field}_median"] = median(values)
        row[f"{field}_p90"] = percentile(values, 0.90)
    for field in ("runtime_scg_s", "runtime_sync_s", "runtime_expansion_s",
                  "runtime_lp_s", "runtime_total_s", "runtime_compute_s"):
        values = observed(row_[field] for row_ in rows)
        row[f"{field}_n"] = len(values)
        row[f"{field}_median"] = median(values)
    row["entered_depth_L_stage_fraction"] = fraction(
        sum(row_["entered_depth_L_stage"] for row_ in rows), len(rows))
    row["cache_hit_count"] = sum(row_["cache_hit"] for row_ in rows)
    return row


def size_bin(size):
    if size is None:
        return None
    edges = [(0, 64), (64, 96), (96, 128), (128, 160), (160, 200),
             (200, 260), (260, 340), (340, 450), (450, 10 ** 9)]
    for low, high in edges:
        if low <= size < high:
            return f"[{low},{high if high < 10 ** 9 else 'inf'})"
    return None


# ---------------------------------------------------------------------------
# Experiment-specific groups
# ---------------------------------------------------------------------------
def structural_group(rows, cap):
    """A: model size vs SCG/sync/runtime/conclusive/INC/TIMEOUT."""
    out = []
    by_stratum = defaultdict(list)
    for row in rows:
        by_stratum[row["stratum"]].append(row)
    for stratum in sorted(by_stratum):
        group = by_stratum[stratum]
        series = next((r["series"] for r in group if r["series"]), "")
        summary = summarize(group, {"group_kind": "stratum", "group": stratum,
                                    "series": series}, cap)
        out.append(summary)
    by_bin = defaultdict(list)
    for row in rows:
        label = size_bin(row["size_places_transitions"])
        if label:
            by_bin[label].append(row)
    for label in sorted(by_bin, key=lambda x: int(x.split(",")[0].strip("["))):
        group = by_bin[label]
        summary = summarize(group, {"group_kind": "size_bin", "group": label,
                                    "series": "all"}, cap)
        out.append(summary)
    return out, by_stratum


def spearman_summary(rows):
    """Completed-only rank correlations; censoring truncates the upper tail."""
    completed = [row for row in rows if row["is_completed"]]
    result = {"censoring_caveat": (
        "Computed over COMPLETED cells only; the right-censored (large/slow) "
        "tail is removed, so correlations are lower bounds of the true "
        "association and must not be read as predictive fits.")}
    try:
        from scipy.stats import spearmanr  # noqa: PLC0415
    except Exception:  # pragma: no cover - scipy is in the pinned venv
        result["available"] = False
        return result
    result["available"] = True
    pairs = {
        "size_vs_scg_classes": ("size_places_transitions", "scg_classes"),
        "size_vs_sync_states": ("size_places_transitions", "sync_states"),
        "size_vs_runtime_compute": ("size_places_transitions", "runtime_compute_s"),
        "scg_classes_vs_sync_states": ("scg_classes", "sync_states"),
        "scg_classes_vs_runtime_compute": ("scg_classes", "runtime_compute_s"),
    }
    for name, (x_field, y_field) in pairs.items():
        data = [(row[x_field], row[y_field]) for row in completed
                if row[x_field] is not None and row[y_field] is not None]
        if len(data) < 3:
            result[name] = {"n": len(data), "rho": None, "p": None}
            continue
        xs, ys = zip(*data)
        rho, p_value = spearmanr(xs, ys)
        result[name] = {"n": len(data), "rho": fmt(float(rho)),
                        "p": fmt(float(p_value))}
    return result


def wip_groups(rows, cap):
    """B: paired WIP 1/2/3 growth on the same base model."""
    by_wip = defaultdict(list)
    for row in rows:
        by_wip[row["WIP"]].append(row)
    by_wip_rows = [summarize(by_wip[wip],
                             {"group_kind": "WIP", "group": f"WIP_{wip}",
                              "series": "all"}, cap)
                   for wip in sorted(k for k in by_wip if k is not None)]
    pairs = defaultdict(dict)
    for row in rows:
        if row["WIP"] in (1, 2, 3):
            pairs[row["base_model_id"]][row["WIP"]] = row

    pair_rows = []
    ratio_values = {"wall": defaultdict(list), "compute": defaultdict(list)}
    bound_values = {"wall": defaultdict(list), "compute": defaultdict(list)}
    for base in sorted(pairs):
        levels = pairs[base]
        reference = levels.get(1)
        row = {
            "base_model_id": base,
            "benchmark_family": (reference or next(iter(levels.values())))["benchmark_family"],
            "seed": (reference or next(iter(levels.values())))["seed"],
            "L": (reference or next(iter(levels.values())))["L"],
            "H": (reference or next(iter(levels.values())))["H"],
            "mode": (reference or next(iter(levels.values())))["mode"],
            "n_wip_levels": len(levels),
        }
        for wip in (1, 2, 3):
            cell = levels.get(wip)
            if cell is None:
                row[f"wip{wip}_status"] = "MISSING"
                continue
            row[f"wip{wip}_status"] = cell["execution_status"]
            row[f"wip{wip}_verdict"] = cell["verdict"]
            row[f"wip{wip}_wall_s"] = cell["wall_s"]
            row[f"wip{wip}_compute_s"] = cell["runtime_compute_s"]
            row[f"wip{wip}_scg_classes"] = cell["scg_classes"]
            row[f"wip{wip}_sync_states"] = cell["sync_states"]
        if reference is not None:
            for wip in (2, 3):
                cell = levels.get(wip)
                if cell is None:
                    continue
                for metric, label, ratio_col, flag_col in (
                    ("wall_s", "wall", f"ratio_wall_{wip}_1",
                     f"ratio_wall_{wip}_1_censored"),
                    ("runtime_compute_s", "compute",
                     f"ratio_compute_{wip}_1",
                     f"ratio_compute_{wip}_1_censored"),
                ):
                    base_value = reference[metric]
                    cell_value = cell[metric]
                    if base_value not in (None, 0) and cell_value is not None:
                        ratio = cell_value / base_value
                        row[ratio_col] = ratio
                        row[flag_col] = 0
                        ratio_values[label][wip].append(ratio)
                    elif base_value not in (None, 0) and cell["is_timeout"]:
                        # Censored numerator: the ratio is only a lower bound.
                        bound = cap / base_value
                        row[ratio_col] = None
                        row[f"{ratio_col}_lower_bound"] = bound
                        row[flag_col] = 1
                        bound_values[label][wip].append(bound)
                    else:
                        row[ratio_col] = None
                        row[flag_col] = 1
        pair_rows.append(row)

    ratios = {}
    for wip in (2, 3):
        for label in ("wall", "compute"):
            values = ratio_values[label].get(wip, [])
            bounds = bound_values[label].get(wip, [])
            ratios[f"wip{wip}_vs_wip1_{label}_ratio"] = {
                "n_uncensored_pairs": len(values),
                "median_ratio": median(values),
                "p90_ratio": percentile(values, 0.90),
                "n_censored_pairs": len(bounds),
                "median_censored_lower_bound": median(bounds),
                "censoring_note": (
                    "Ratio is computed only when BOTH the WIP=1 reference and "
                    f"the WIP={wip} cell COMPLETED; a WIP={wip} TIMEOUT yields "
                    "only a lower bound (cap / reference) and is never treated "
                    "as a measured ratio."),
            }
    return by_wip_rows, pair_rows, ratios


def l_sweep_groups(rows, cap):
    """C: fixed model/H, increasing L; clean L contrast within each mode."""
    levels = defaultdict(list)
    for row in rows:
        if row["L"] is None:
            continue
        levels[(row["base_model_id"], row["mode"])].append(row)
    level_rows = []
    pair_rows = []
    rank_of = {}
    for (base, mode), group in sorted(levels.items()):
        group.sort(key=lambda r: r["L"])
        for position, row in enumerate(group):
            rank_of[id(row)] = position
        reference = group[0]
        for row in group:
            level_rows.append({
                "task_id": row["task_id"],
                "base_model_id": base,
                "benchmark_family": row["benchmark_family"],
                "mode": mode,
                "L": row["L"],
                "H": row["H"],
                "l_rank": group.index(row),
                "n_levels": len(group),
                "execution_status": row["execution_status"],
                "verdict": row["verdict"],
                "failure_stage": row["failure_stage"],
                "entered_depth_L_stage": row["entered_depth_L_stage"],
                "scg_classes": row["scg_classes"],
                "sync_states": row["sync_states"],
                "candidate_words": row["candidate_words"],
                "terminal_pairs": row["terminal_pairs"],
                "lp_calls": row["lp_calls"],
                "runtime_compute_s": row["runtime_compute_s"],
                "wall_s": row["wall_s"],
            })
        sync_pairs = [(a["sync_states"], b["sync_states"])
                      for a, b in zip(group, group[1:])
                      if a["sync_states"] is not None
                      and b["sync_states"] is not None]
        candidate_positive = [row for row in group
                              if row["candidate_words"] not in (None, 0)]
        verdicts = [row["verdict"] for row in group if row["verdict"]]
        entered = [row for row in group if row["entered_depth_L_stage"]]
        pair_rows.append({
            "base_model_id": base,
            "benchmark_family": reference["benchmark_family"],
            "seed": reference["seed"],
            "mode": mode,
            "H": reference["H"],
            "n_levels": len(group),
            "L_min": group[0]["L"],
            "L_max": group[-1]["L"],
            "L_values": json.dumps([row["L"] for row in group]),
            "n_completed": sum(row["is_completed"] for row in group),
            "n_timeout": sum(row["is_timeout"] for row in group),
            "n_inc": sum(1 for row in group if row["verdict"] == "INC"),
            "n_entered_depth_L_stage": len(entered),
            "sync_states_min": min((row["sync_states"] for row in group
                                    if row["sync_states"] is not None),
                                   default=None),
            "sync_states_max": max((row["sync_states"] for row in group
                                    if row["sync_states"] is not None),
                                   default=None),
            "l_sensitive_state": int(any(b > a for a, b in sync_pairs)),
            "l_sensitive_candidate": int(bool(candidate_positive)),
            "l_candidate_onset_L": (min(row["L"] for row in candidate_positive)
                                    if candidate_positive else None),
            "l_sensitive_verdict": int(len(set(verdicts)) > 1),
            "l_sensitive": int(any(b > a for a, b in sync_pairs)
                               or bool(candidate_positive)
                               or len(set(verdicts)) > 1),
            "mode_confound_note": (
                "synchronizer state/candidate counts are independent of mode; "
                "verdict changes are additionally affected by mode when the "
                "sweep mixes modes" if len({row["mode"] for row in group}) > 1
                else "single mode; clean L contrast"),
        })
    # Aggregate by rank across all (model, mode) sweeps using the full rows
    # (so the censoring-aware summarize() has every normalized column).
    by_rank = defaultdict(list)
    for row in rows:
        rank = rank_of.get(id(row))
        if rank is not None:
            by_rank[rank].append(row)
    rank_rows = []
    for rank in sorted(by_rank):
        group = by_rank[rank]
        summary = summarize(group, {
            "group_kind": "l_rank", "group": rank,
            "series": "all",
        }, cap)
        summary["L_median"] = median(row["L"] for row in group)
        summary["L_values"] = json.dumps(sorted({row["L"] for row in group})) \
            if len({row["L"] for row in group}) <= 12 else "varies"
        summary["candidate_words_positive"] = sum(
            1 for r in group if r["candidate_words"] not in (None, 0))
        summary["sync_states_observed"] = sum(
            1 for r in group if r["sync_states"] is not None)
        rank_rows.append(summary)
    # Aggregate by absolute L (meaningful for consecutive-L formal sweeps).
    by_l = defaultdict(list)
    for row in rows:
        if id(row) in rank_of:
            by_l[row["L"]].append(row)
    l_rows = []
    for value in sorted(by_l):
        group = by_l[value]
        summary = summarize(group, {"group_kind": "absolute_L",
                                    "group": value, "series": "all"}, cap)
        summary["candidate_words_positive"] = sum(
            1 for r in group if r["candidate_words"] not in (None, 0))
        l_rows.append(summary)
    # Model-level view across all modes (needed when a Pilot sweep mixes modes).
    by_model = defaultdict(list)
    for row in rows:
        if id(row) in rank_of:
            by_model[row["base_model_id"]].append(row)
    model_rows = []
    for base in sorted(by_model):
        group = sorted(by_model[base], key=lambda r: (r["L"], r["mode"]))
        candidate_positive = [row for row in group
                              if row["candidate_words"] not in (None, 0)]
        verdicts = [row["verdict"] for row in group if row["verdict"]]
        sync_pairs = [(a["sync_states"], b["sync_states"])
                      for a, b in zip(group, group[1:])
                      if a["sync_states"] is not None
                      and b["sync_states"] is not None]
        modes = sorted({row["mode"] for row in group})
        model_rows.append({
            "base_model_id": base,
            "benchmark_family": group[0]["benchmark_family"],
            "seed": group[0]["seed"],
            "H": group[0]["H"],
            "modes": json.dumps(modes),
            "mode_mixed": int(len(modes) > 1),
            "n_levels": len(group),
            "L_min": group[0]["L"],
            "L_max": group[-1]["L"],
            "L_values": json.dumps([row["L"] for row in group]),
            "n_completed": sum(row["is_completed"] for row in group),
            "n_timeout": sum(row["is_timeout"] for row in group),
            "n_inc": sum(1 for row in group if row["verdict"] == "INC"),
            "n_entered_depth_L_stage": sum(
                row["entered_depth_L_stage"] for row in group),
            "candidate_positive_levels": json.dumps(
                [row["L"] for row in candidate_positive]),
            "l_candidate_onset_L": (min(row["L"] for row in candidate_positive)
                                    if candidate_positive else None),
            "verdicts_by_L": json.dumps(
                [{"L": row["L"], "mode": row["mode"], "verdict": row["verdict"],
                  "status": row["execution_status"]} for row in group]),
            "l_sensitive_state": int(any(b > a for a, b in sync_pairs)),
            "l_sensitive_candidate": int(bool(candidate_positive)),
            "l_sensitive_verdict": int(len(set(verdicts)) > 1),
            "l_sensitive": int(any(b > a for a, b in sync_pairs)
                               or bool(candidate_positive)
                               or len(set(verdicts)) > 1),
            "verdict_change_confounded_by_mode": int(
                len(modes) > 1 and len(set(verdicts)) > 1),
        })
    aggregates = {
        "n_mode_sweeps": len(pair_rows),
        "n_base_models": len(model_rows),
        "n_mode_sweeps_l_state_sensitive": sum(
            r["l_sensitive_state"] for r in pair_rows),
        "n_mode_sweeps_l_candidate_onset": sum(
            r["l_sensitive_candidate"] for r in pair_rows),
        "n_mode_sweeps_l_sensitive": sum(r["l_sensitive"] for r in pair_rows),
        "n_models_l_state_sensitive": sum(
            r["l_sensitive_state"] for r in model_rows),
        "n_models_l_candidate_onset": sum(
            r["l_sensitive_candidate"] for r in model_rows),
        "n_models_l_verdict_change": sum(
            r["l_sensitive_verdict"] for r in model_rows),
        "n_models_l_sensitive": sum(r["l_sensitive"] for r in model_rows),
        "n_models_mode_mixed": sum(r["mode_mixed"] for r in model_rows),
        "n_models_verdict_change_confounded_by_mode": sum(
            r["verdict_change_confounded_by_mode"] for r in model_rows),
        "n_cells_entered_depth_L_stage": sum(
            row["entered_depth_L_stage"] for row in rows),
        "n_cells_candidate_positive": sum(
            1 for row in rows if row["candidate_words"] not in (None, 0)),
        "definition": (
            "A sweep is L-sensitive when the depth-L synchronizer actually "
            "depends on L: synchronizer states grow with L, candidate words "
            "appear only at larger L, or the verdict changes with L. "
            "sync_states/candidate_words are mode-independent; verdict changes "
            "are not when a sweep mixes modes, so the confounded count is "
            "reported explicitly. A verdict change can only be observed "
            "between levels that both produced a verdict: a high-L TIMEOUT is "
            "censored and is not a verdict change, so "
            "n_models_l_verdict_change <= n_models_l_candidate_onset."),
    }
    return rank_rows, l_rows, pair_rows, level_rows, model_rows, aggregates


def difficult_groups(rows, cap):
    """D: state-space cap, runtime, timeout, conclusive rate, failure stages."""
    overall = summarize(rows, {"group_kind": "overall", "group": "D_difficult",
                               "series": "all"}, cap)
    overall["scg_limit_hit_count"] = sum(row["scg_limit_hit"] or 0
                                         for row in rows)
    overall["scg_limit_hit_fraction"] = fraction(
        overall["scg_limit_hit_count"], len(rows))
    groups = [overall]
    for kind, field in (("family", "benchmark_family"), ("stratum", "stratum"),
                        ("level", "design_level")):
        by_value = defaultdict(list)
        for row in rows:
            value = row[field] if row[field] else "unknown"
            by_value[value].append(row)
        for value in sorted(by_value):
            summary = summarize(by_value[value],
                                {"group_kind": kind, "group": value,
                                 "series": "all"}, cap)
            summary["scg_limit_hit_count"] = sum(
                row["scg_limit_hit"] or 0 for row in by_value[value])
            summary["scg_limit_hit_fraction"] = fraction(
                summary["scg_limit_hit_count"], len(by_value[value]))
            groups.append(summary)
    return groups


def failure_stage_rows(rows):
    out = []
    scopes = [("all", rows)]
    for experiment in sorted({row["experiment"] for row in rows
                              if row["experiment"]}):
        scopes.append((f"experiment:{experiment}",
                       [row for row in rows if row["experiment"] == experiment]))
    for scope, group in scopes:
        stages = Counter()
        for row in group:
            if row["verdict"] == "INC":
                stages[f"INC_after_{row['failure_stage'] or 'SCG'}"] += 1
            elif row["is_timeout"]:
                stages[f"TIMEOUT_at_{row['failure_stage'] or 'UNKNOWN'}"] += 1
            elif row["is_error"]:
                stages["ERROR"] += 1
            else:
                stages["COMPLETED_conclusive"] += 1
        total = len(group)
        for stage in sorted(stages):
            out.append({"scope": scope, "stage": stage, "count": stages[stage],
                        "fraction": fraction(stages[stage], total),
                        "n_total": total})
    return out


# ---------------------------------------------------------------------------
# Integrity cross-check against the frozen task manifest
# ---------------------------------------------------------------------------
def check_integrity(tasks, records, manifest, raw_paths):
    errors, warnings = [], []
    errors_list = []
    by_id = defaultdict(list)
    for record in records:
        by_id[record.get("task_id")].append(record)
    missing = sorted(set(tasks) - set(by_id))
    duplicate = sorted(task_id for task_id, group in by_id.items()
                       if len(group) != 1)
    unexpected = sorted(set(by_id) - set(tasks))
    if missing:
        errors_list.append(f"missing task IDs ({len(missing)}): {missing[:20]}")
    if duplicate:
        errors_list.append(f"non-unique final records ({len(duplicate)}): "
                           f"{duplicate[:20]}")
    if unexpected:
        errors_list.append(f"unexpected task IDs ({len(unexpected)}): "
                           f"{unexpected[:20]}")
    expected_tasks = manifest.get("tasks")
    if expected_tasks is not None and len(tasks) != expected_tasks:
        errors_list.append(f"manifest declares {expected_tasks} tasks but "
                           f"tasks dir has {len(tasks)}")
    if len(records) != len(by_id):
        errors_list.append(f"record count {len(records)} != unique task IDs "
                           f"{len(by_id)}")
    shards = manifest.get("shards")
    if shards is not None and len(raw_paths) != shards:
        warnings.append(f"manifest declares {shards} shards but {len(raw_paths)} "
                        "raw shard files were found")
    frozen_fields = (
        "protocol_version", "experiment", "benchmark_family", "model_id",
        "bounded_problem_id", "configuration_id", "seed", "replicate",
        "L", "H", "mode", "timeout_s",
    )
    mismatches = []
    status_violations = []
    counter_repairs = 0
    run_ids, commit_shas = Counter(), Counter()
    for record in records:
        task = tasks.get(record.get("task_id"))
        if task is None:
            continue
        for field in frozen_fields:
            expected = task.get(field)
            actual = record.get(field)
            if field in ("L", "seed", "replicate", "timeout_s"):
                expected = _int(expected)
                actual = _int(actual)
            elif field == "H":
                expected = _float(expected)
                actual = _float(actual)
            elif field == "model_id":
                actual = record.get("model_id", record.get("model"))
            if actual != expected:
                mismatches.append({
                    "task_id": record.get("task_id"), "field": field,
                    "expected": expected, "actual": actual})
        expected_params = task.get("params")
        if expected_params:
            try:
                expected = json.loads(expected_params)
                if record.get("params") != expected:
                    mismatches.append({
                        "task_id": record.get("task_id"), "field": "params",
                        "expected": expected, "actual": record.get("params")})
            except json.JSONDecodeError:
                pass
        status = record.get("execution_status")
        if status not in {"COMPLETED", "TIMEOUT_WALL_CLOCK"}:
            status_violations.append({
                "task_id": record.get("task_id"), "execution_status": status,
                "error": record.get("error")})
        if status == "TIMEOUT_WALL_CLOCK" and record.get("verdict") is not None:
            status_violations.append({
                "task_id": record.get("task_id"),
                "issue": "timeout carries a verdict"})
        if status == "COMPLETED" and record.get("verdict") not in {
                "VER", "AMB", "INC"}:
            status_violations.append({
                "task_id": record.get("task_id"),
                "issue": "completed without a valid verdict",
                "verdict": record.get("verdict")})
        _, repairs = normalize_counters(record)
        counter_repairs += repairs
        if record.get("workflow_run_id") is not None:
            run_ids[str(record["workflow_run_id"])] += 1
        if record.get("commit_sha"):
            commit_shas[str(record["commit_sha"])] += 1
    errors.extend(errors_list)
    if mismatches:
        errors.append(f"{len(mismatches)} frozen-field mismatches; first: "
                      f"{mismatches[:5]}")
    if status_violations:
        errors.append(f"{len(status_violations)} status/verdict violations; "
                      f"first: {status_violations[:5]}")
    if counter_repairs:
        warnings.append(
            f"{counter_repairs} counter values from unexecuted stages were "
            "zero in the raw artifacts and were re-normalized to null")
    integrity = {
        "pass": not errors,
        "errors": errors,
        "warnings": warnings,
        "missing_tasks": len(missing),
        "duplicate_records": len(duplicate),
        "unexpected_tasks": len(unexpected),
        "frozen_field_mismatches": len(mismatches),
        "mismatch_examples": mismatches[:10],
        "status_violations": len(status_violations),
        "status_violation_examples": status_violations[:10],
        "counter_gating_repairs": counter_repairs,
        "unique_task_records": len(by_id),
        "raw_shard_files": len(raw_paths),
        "workflow_run_ids": dict(run_ids),
        "commit_shas": dict(commit_shas),
    }
    return integrity


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks-dir", type=Path,
                        default=ROOT / "protocol/v6_pilot_tasks")
    parser.add_argument("--manifest", type=Path,
                        default=ROOT / "protocol/v6_pilot_manifest.json")
    parser.add_argument("--raw-dir", type=Path,
                        default=ROOT / "work/v6_optimized_pilot_run_42348132809")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "work/v6_analysis_pilot_selfcheck")
    parser.add_argument("--dataset-label", default=None,
                        help="human label; defaults to PILOT-SELFCHECK for Pilot "
                             "manifests and FORMAL otherwise")
    parser.add_argument("--reference-audit", type=Path, default=None,
                        help="optional pilot_audit.json to cross-check the "
                             "independent audit counts (no effect on outputs)")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="write partial statistics even when tasks/records "
                             "do not match the frozen manifest")
    args = parser.parse_args()

    tasks_dir = args.tasks_dir if args.tasks_dir.is_absolute() \
        else (ROOT / args.tasks_dir)
    manifest_path = args.manifest if args.manifest.is_absolute() \
        else (ROOT / args.manifest)
    raw_dir = args.raw_dir if args.raw_dir.is_absolute() else (ROOT / args.raw_dir)
    out_dir = args.out if args.out.is_absolute() else (ROOT / args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(manifest_path.read_text())
    tasks = load_tasks(tasks_dir)
    raw_paths, records = load_records(raw_dir)
    integrity = check_integrity(tasks, records, manifest, raw_paths)
    if not integrity["pass"] and not args.allow_incomplete:
        (out_dir / "integrity.json").write_text(
            json.dumps(jsonable(integrity), indent=2, sort_keys=True) + "\n")
        raise SystemExit("integrity check failed; refusing to emit statistics. "
                         "See integrity.json (use --allow-incomplete to override).")

    cap = _float(manifest.get("cell_timeout_seconds")) or 60.0
    protocol = str(manifest.get("version") or "")
    is_formal = protocol.startswith("v6-formal") or "formal" in protocol
    dataset_label = args.dataset_label or ("FORMAL" if is_formal
                                           else "PILOT-SELFCHECK")

    rows = [normalize_record(record, tasks[record["task_id"]], manifest)
            for record in records]
    rows.sort(key=lambda row: row["task_id"])

    # Cross-experiment L/level metadata carried on the normalized rows.
    experiments = defaultdict(list)
    for row in rows:
        experiments[row["experiment"]].append(row)

    # ---------------- A/B/C/D groups ----------------
    a_rows, a_by_stratum = structural_group(
        experiments.get("A_structural", []), cap)
    a_spearman = spearman_summary(experiments.get("A_structural", []))
    b_by_wip, b_pairs, b_ratios = wip_groups(
        experiments.get("B_wip", []), cap)
    (c_rank_rows, c_l_rows, c_pairs, c_levels, c_models,
     c_aggregates) = l_sweep_groups(experiments.get("C_Lsweep", []), cap)
    d_rows = difficult_groups(experiments.get("D_difficult", []), cap)

    # Carry C sensitivity flags back onto the normalized table (clean,
    # mode-independent flags only: sync-state growth and candidate onset).
    level_lookup = {row["task_id"]: row for row in c_levels}
    for row in experiments.get("C_Lsweep", []):
        level = level_lookup.get(row["task_id"])
        if level:
            row["l_rank"] = level["l_rank"]
            row["n_l_levels"] = level["n_levels"]
    model_lookup = {row["base_model_id"]: row for row in c_models}
    for row in experiments.get("C_Lsweep", []):
        model = model_lookup.get(row["base_model_id"])
        if model:
            row["l_sensitive_state"] = model["l_sensitive_state"]
            row["l_sensitive_candidate"] = model["l_sensitive_candidate"]
            row["l_sensitive_verdict"] = model["l_sensitive_verdict"]

    # ---------------- denominators ----------------
    denominator_rows = [summarize(rows, {"group_kind": "overall",
                                         "group": "all", "series": "all"}, cap)]
    for experiment in sorted(experiments):
        denominator_rows.append(summarize(
            experiments[experiment],
            {"group_kind": "experiment", "group": experiment, "series": "all"},
            cap))
    for stratum, group in sorted(a_by_stratum.items()):
        denominator_rows.append(summarize(
            group, {"group_kind": "A_stratum", "group": stratum,
                    "series": "all"}, cap))
    for wip in sorted({row["WIP"] for row in experiments.get("B_wip", [])
                       if row["WIP"] is not None}):
        group = [row for row in experiments["B_wip"] if row["WIP"] == wip]
        denominator_rows.append(summarize(
            group, {"group_kind": "B_WIP", "group": f"WIP_{wip}",
                    "series": "all"}, cap))
    for stratum in sorted({row["stratum"]
                           for row in experiments.get("D_difficult", [])}):
        group = [row for row in experiments["D_difficult"]
                 if row["stratum"] == stratum]
        denominator_rows.append(summarize(
            group, {"group_kind": "D_stratum", "group": stratum,
                    "series": "all"}, cap))

    # ---------------- outputs ----------------
    write_csv(out_dir / "normalized_tasks.csv", rows)
    write_csv(out_dir / "denominators.csv", denominator_rows)
    write_csv(out_dir / "A_structural.csv", a_rows)
    write_csv(out_dir / "B_wip.csv", b_by_wip)
    write_csv(out_dir / "B_wip_pairs.csv", b_pairs)
    write_csv(out_dir / "C_lsweep.csv", c_rank_rows + c_l_rows)
    write_csv(out_dir / "C_lsweep_pairs.csv", c_pairs)
    write_csv(out_dir / "C_lsweep_models.csv", c_models)
    write_csv(out_dir / "C_lsweep_levels.csv", c_levels)
    write_csv(out_dir / "D_difficult.csv", d_rows)
    write_csv(out_dir / "failure_stages.csv", failure_stage_rows(rows))

    overall = denominator_rows[0]
    cache_counts = Counter(row["scg_cache_status"] for row in rows)
    cache_rows = [row for row in rows if row["cache_hit"]]
    statistics_payload = {
        "schema": ANALYSIS_SCHEMA,
        "analysis_version": ANALYSIS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": {
            "label": dataset_label,
            "is_formal": is_formal,
            "protocol_version": protocol,
            "manifest_version": manifest.get("version"),
            "manifest_frozen_before_execution":
                manifest.get("frozen_before_execution"),
            "tasks_declared_by_manifest": manifest.get("tasks"),
            "tasks_in_frozen_table": len(tasks),
            "records_analyzed": len(records),
            "raw_shard_files": len(raw_paths),
            "tasks_dir": str(tasks_dir),
            "manifest_path": str(manifest_path),
            "raw_dir": str(raw_dir),
            "cell_timeout_s": cap,
            "workflow_run_ids": integrity["workflow_run_ids"],
            "commit_shas": integrity["commit_shas"],
            "data_disclaimer": (
                "PILOT-SELFCHECK outputs exercise the pipeline on real Pilot "
                "artifacts and are NOT the formal 8,280-cell result."
                if not is_formal else
                "FORMAL outputs computed from the frozen 8,280-cell run."),
        },
        "integrity": integrity,
        "quantile_method": {
            "percentiles": "nearest-rank (ceil(p*n)-1) over COMPLETED cells only",
            "censoring": CENSOR_NOTE.format(cap=cap),
            "cap": cap,
        },
        "denominators": {
            "overall": overall,
            "by_experiment": {
                experiment: summarize(
                    experiments[experiment],
                    {"group_kind": "experiment", "group": experiment,
                     "series": "all"}, cap)
                for experiment in sorted(experiments)},
        },
        "groups": {
            "A_structural": {
                "definition": (
                    "120/600-cell pre-registered structural grid: model size vs "
                    "SCG classes, synchronizer states, runtime, conclusive rate, "
                    "INC and TIMEOUT."),
                "rows": a_rows,
                "spearman_completed_only": a_spearman,
                "censoring_note": CENSOR_NOTE.format(cap=cap),
            },
            "B_wip": {
                "definition": (
                    "same base model at WIP=1/2/3; paired growth ratios with "
                    "explicit right-censoring (a WIP=2/3 TIMEOUT only bounds the "
                    "ratio from below)."),
                "by_wip": b_by_wip,
                "paired_growth": b_ratios,
                "n_base_models": len({row["base_model_id"]
                                      for row in experiments.get("B_wip", [])}),
                "censoring_note": CENSOR_NOTE.format(cap=cap),
            },
            "C_Lsweep": {
                "definition": (
                    "same model/H, increasing depth limit L: synchronizer states, "
                    "candidate words, terminal pairs, LP calls, runtime, and "
                    "whether L-sensitive work was actually entered."),
                "by_l_rank": c_rank_rows,
                "by_absolute_L": c_l_rows,
                "by_model": c_models,
                "l_sensitivity": c_aggregates,
                "censoring_note": CENSOR_NOTE.format(cap=cap),
            },
            "D_difficult": {
                "definition": (
                    "pre-registered difficult cells: state-space cap ratio, "
                    "runtime, timeout share, conclusive rate, failure stages."),
                "rows": d_rows,
                "censoring_note": CENSOR_NOTE.format(cap=cap),
            },
        },
        "failure_stage_distribution": failure_stage_rows(rows),
        "cache_accounting": {
            "status_counts": dict(cache_counts),
            "cache_hit_cells": len(cache_rows),
            "note": (
                "On a cache hit the SCG build cost was paid by an earlier cell; "
                "the hit cell is attributed the cache load time instead of a "
                "fabricated 0 s build. Cache-hit cells are flagged in "
                "normalized_tasks.csv (scg_cache_status, cache_hit)."),
        },
        "cost_accounting": {
            "completed_wall_sum_s": sum(
                row["wall_s"] for row in rows if row["is_completed"]),
            "censored_wall_lower_bound_sum_s": sum(
                cap for row in rows if row["is_timeout"]),
            "n_cells_at_cap": overall["n_timeout"],
            "note": (
                "Runner-time accounting for budget planning only. It is not an "
                "independent model-performance metric; do not sum across "
                "environments or protocol versions."),
        },
        "caveats": [
            "Every time quantile is computed over COMPLETED cells only; TIMEOUT "
            "cells are right-censored at the cell cap and never imputed as "
            "completions.",
            "INC means completed-but-inconclusive (e.g. SCG state limit) and is "
            "reported as its own denominator; it is not zero work and not a "
            "VER/AMB.",
            "Cell compute time depends on the runner environment and the "
            "optimized implementation; only within-run paired contrasts are "
            "meaningful.",
            "The Pilot C sweep mixes mode=first at low/mid L with "
            "mode=complete at high L; synchronizer state and candidate-word "
            "counts are mode-independent, verdict changes are not. The formal "
            "C1/C2 sweeps fix the mode per sweep, so the clean L contrast uses "
            "the same-mode levels only.",
            "Candidate-word and state counts are null (not zero) when the "
            "stage never executed; the observed-n columns give the denominator "
            "for each median.",
        ],
    }
    (out_dir / "statistics.json").write_text(
        json.dumps(jsonable(statistics_payload), indent=2, sort_keys=True) + "\n")

    # ---------------- optional cross-check against the independent audit ----
    audit_check = None
    if args.reference_audit:
        audit = json.loads(args.reference_audit.read_text())
        audit_check = {
            "reference": str(args.reference_audit),
            "audit_tasks": audit.get("tasks"),
            "audit_execution_status": audit.get("execution_status"),
            "audit_verdict": audit.get("verdict"),
            "analysis_tasks": len(rows),
            "analysis_execution_status": dict(
                Counter(row["execution_status"] for row in rows)),
            "analysis_verdict": dict(
                Counter(str(row["verdict"]) for row in rows
                        if row["is_completed"])),
        }
        audit_check["execution_status_match"] = (
            {k: int(v) for k, v in (audit.get("execution_status") or {}).items()}
            == audit_check["analysis_execution_status"])
        audit_verdict = {k: int(v) for k, v in (audit.get("verdict") or {}).items()
                         if k != "None"}
        audit_check["verdict_match"] = audit_verdict == audit_check["analysis_verdict"]
        audit_check["tasks_match"] = audit.get("tasks") == len(rows)
        statistics_payload["audit_cross_check"] = audit_check
        (out_dir / "statistics.json").write_text(
            json.dumps(jsonable(statistics_payload), indent=2,
                       sort_keys=True) + "\n")

    # ---------------- human-readable summary ----------------
    lines = [
        f"# v6 analysis summary ({dataset_label})",
        "",
        f"- Protocol: `{protocol}`  Tasks: {len(tasks)}  Records: {len(records)}",
        f"- Cap: {cap:g} s per cell  Integrity: "
        f"{'PASS' if integrity['pass'] else 'FAIL'}",
        f"- Datasets: {', '.join(sorted(integrity['workflow_run_ids']))}",
        "",
        "## Denominators (all cells)",
        "",
        "| experiment | total | completed | timeout | INC | VER | AMB | ERROR |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in denominator_rows:
        if row["group_kind"] not in ("overall", "experiment"):
            continue
        lines.append(
            f"| {row['group']} | {row['n_total']} | {row['n_completed']} | "
            f"{row['n_timeout']} | {row['n_inc']} | {row['n_ver']} | "
            f"{row['n_amb']} | {row['n_error']} |")
    lines += [
        "",
        "## Censoring and INC handling",
        "",
        f"- {overall['n_timeout']}/{overall['n_total']} cells are right-censored "
        f"at {cap:g} s; all time quantiles use the "
        f"{overall['n_completed']} COMPLETED cells only.",
        "- INC is reported as its own denominator and is never a zero-work cell.",
        "",
        "## Key findings",
        "",
    ]
    a_summary = next((row for row in denominator_rows
                      if row["group_kind"] == "experiment"
                      and row["group"] == "A_structural"), None)
    if a_summary:
        lines.append(
            f"- A structural: conclusive rate over COMPLETED cells "
            f"{fmt(a_summary['conclusive_fraction_completed'], 3)}; "
            f"timeout share {fmt(a_summary['timeout_fraction'], 3)}.")
    if b_by_wip:
        for row in b_by_wip:
            lines.append(
                f"- B {row['group']}: {row['n_timeout']}/{row['n_total']} "
                f"timeout, INC={row['n_inc']}, wall P50 "
                f"{fmt(row['wall_p50_completed_s'], 3)} s (completed only).")
        lines.append(
            f"- B paired growth (uncensored pairs): "
            f"WIP2/WIP1 median "
            f"{fmt(b_ratios['wip2_vs_wip1_compute_ratio']['median_ratio'], 3)}, "
            f"WIP3/WIP1 median "
            f"{fmt(b_ratios['wip3_vs_wip1_compute_ratio']['median_ratio'], 3)}; "
            "censored pairs only give lower bounds.")
    if c_aggregates:
        lines.append(
            f"- C L-sweep: {c_aggregates['n_cells_entered_depth_L_stage']}/"
            f"{len(experiments.get('C_Lsweep', []))} cells entered the depth-L "
            f"synchronizer stage; {c_aggregates['n_cells_candidate_positive']} "
            f"cells produced candidate words; "
            f"{c_aggregates['n_models_l_candidate_onset']}/"
            f"{c_aggregates['n_base_models']} L-swept models show "
            "candidate-word onset at some L; "
            f"{c_aggregates['n_models_verdict_change_confounded_by_mode']} "
            "show a verdict change that is confounded by mixed modes.")
    if d_rows:
        overall_d = d_rows[0]
        lines.append(
            f"- D difficult: INC {overall_d['n_inc']}/{overall_d['n_total']} "
            f"(state-space cap), timeout {overall_d['n_timeout']}, "
            f"conclusive {overall_d['n_conclusive']}.")
    lines += [
        "",
        "## Files",
        "",
        "- `normalized_tasks.csv` — one row per task (denominators, censoring,",
        "  stage counters/timings, cache status, L-sensitivity flags).",
        "- `denominators.csv` — n_total/n_completed/n_timeout/n_inc/n_error per",
        "  group with the censoring note.",
        "- `A_structural.csv` / `B_wip*.csv` / `C_lsweep*.csv` /",
        "  `D_difficult.csv` — the four experiment tables.",
        "- `failure_stages.csv` — timeout/INC stopping-stage distribution.",
        "- `statistics.json` — everything above plus metadata and caveats.",
        "",
        f"All numbers are recomputable from the raw JSONL under `{raw_dir}` and",
        f"the frozen tasks under `{tasks_dir}`.",
    ]
    (out_dir / "analysis_summary.md").write_text("\n".join(lines) + "\n")

    print(json.dumps({
        "dataset": dataset_label,
        "tasks": len(rows),
        "execution_status": dict(Counter(row["execution_status"] for row in rows)),
        "verdict": dict(Counter(str(row["verdict"]) for row in rows
                                if row["is_completed"])),
        "integrity_pass": integrity["pass"],
        "out": str(out_dir),
        "audit_cross_check": audit_check,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
