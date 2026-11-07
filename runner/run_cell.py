#!/usr/bin/env python3
"""One bounded-verification cell. Prints a single JSON record.

spec: {tag, experiment, model, seed, replicate, L, H, mode, timeout_s, params}
mode: "first"    -> verifier default (first-certificate, production behaviour)
      "complete" -> complete_set=True (full candidate/history/LP accounting)
"""
from __future__ import annotations
import hashlib, json, os, platform, resource, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.benchmarks.rmt_fms_generator import generate_rmt_fms
from src.benchmarks.v5_families import generate_v5_model
from src.benchmarks.v6_families import generate_v6_model
from src.model.validation import validate_model
from src.scg.builder import build_scg
from src.scg.cache import SCGCache, build_key_material, compute_cache_key
from src.verifier import verify


def _interval_record(interval):
    return {"earliest": str(interval.earliest), "latest": str(interval.latest)}


def _transition_record(name, transition):
    authorized = transition.authorized_range
    return {
        "name": name,
        "label": transition.label,
        "fault": transition.is_fault,
        "adjustable": transition.is_adjustable,
        "interval": _interval_record(transition.interval),
        "admissible_intervals": [
            _interval_record(interval) for interval in transition.admissible_intervals
        ],
        "authorized_range": None if authorized is None else {
            "l_min": str(authorized.l_min), "l_max": str(authorized.l_max),
            "u_min": str(authorized.u_min), "u_max": str(authorized.u_max),
            "resolution": str(authorized.resolution),
        },
    }


def model_hash(tpn) -> str:
    data = {"places": sorted(tpn.places.keys()),
            "transitions": sorted(
                [_transition_record(n, t) for n, t in tpn.transitions.items()],
                key=lambda x: x["name"],
            ),
            "arcs": sorted(f"{a.from_node}->{a.to_node}" for a in tpn.arcs),
            "initial_marking": sorted(tpn.initial_marking.items()),
            "completion_markings": sorted(
                [sorted(m.items()) for m in tpn.completion_markings]
            )}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def structure_hash(tpn) -> str:
    """Hash static TPN structure while excluding WIP-dependent markings."""
    data = {"places": sorted(tpn.places.keys()),
            "transitions": sorted(
                [_transition_record(n, t) for n, t in tpn.transitions.items()],
                key=lambda x: x["name"],
            ),
            "arcs": sorted(f"{a.from_node}->{a.to_node}" for a in tpn.arcs)}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB; macOS reports bytes.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(value / divisor, 3)



SCG_COUNTERS = ("scg_classes", "scg_edges", "runtime_scg")
SYNC_COUNTERS = (
    "sync_states", "sync_edges", "candidate_words",
    "sync_transition_calls", "sync_successors_calls", "sync_words_explored",
    "sync_closure_N_calls", "sync_closure_F_calls", "sync_closure_any_calls",
    "runtime_sync", "runtime_unobservable_closure",
)
EXPANSION_COUNTERS = (
    "temporal_states", "trace_records", "terminal_pairs",
    "candidate_words_processed", "terminal_normal_records",
    "terminal_faulty_records",
    "exp_nodes_created", "exp_edges", "exp_record_checks", "exp_worklist_pops",
    "runtime_expansion", "runtime_certificate",
)
LP_COUNTERS = (
    "lp_calls", "lp_builds", "lp_solves", "lp_feasible", "lp_infeasible",
    "lp_unknown", "confirmed_critical_pairs", "enclosure_rejected_pairs",
    "runtime_lp", "runtime_lp_build", "runtime_lp_solve",
)
STAGE_KEYS = (
    "scg_classes", "scg_edges", "sync_states", "sync_edges", "candidate_words",
    "temporal_states", "trace_records", "terminal_pairs", "lp_calls",
    "confirmed_critical_pairs",
    "sync_transition_calls", "sync_successors_calls", "sync_words_explored",
    "sync_closure_N_calls", "sync_closure_F_calls", "sync_closure_any_calls",
    "exp_nodes_created", "exp_edges", "exp_record_checks", "exp_worklist_pops",
    "lp_builds", "lp_solves", "lp_feasible", "lp_infeasible", "lp_unknown",
    "runtime_scg", "runtime_sync", "runtime_unobservable_closure",
    "runtime_expansion", "runtime_certificate", "runtime_lp",
    "runtime_lp_build", "runtime_lp_solve",
)


def normalized_counters(out):
    stats = out.get("stats", {})
    reason = out.get("inconclusive_reason")
    scg_only = reason in {
        "SCG_STATE_LIMIT", "COMPLETION_UNREACHABLE", "ZERO_TIME_UNOBSERVABLE_CYCLE"
    }
    candidate_words = stats.get("candidate_words")
    expansion_ran = not scg_only and candidate_words not in (None, 0)
    counters = {key: stats.get(key) for key in STAGE_KEYS}
    counters["confirmed_critical_pairs"] = (
        len(out.get("certificates") or []) if expansion_ran else None
    )
    if scg_only:
        for key in SYNC_COUNTERS + EXPANSION_COUNTERS + LP_COUNTERS:
            if key in counters:
                counters[key] = None
        counters["scg_classes"] = stats.get("scg_classes")
        counters["scg_edges"] = stats.get("scg_edges")
        counters["runtime_scg"] = stats.get("runtime_scg")
    elif not expansion_ran:
        # Synchronizer ran but no candidate word survived: expansion/LP did not
        # execute.  Their counters must be null, not zero.
        for key in EXPANSION_COUNTERS + LP_COUNTERS:
            if key in counters:
                counters[key] = None
    return counters


def checkpoint_counters(stage, counters):
    """Use null for stages that have not executed at a progress checkpoint."""
    if counters is None:
        return None
    result = dict(counters)
    if stage == "SCG_COMPLETE":
        for key in SYNC_COUNTERS + EXPANSION_COUNTERS + LP_COUNTERS:
            if key in result:
                result[key] = None
        result["runtime_total"] = None
    elif stage == "SYNCHRONIZER_COMPLETE":
        for key in EXPANSION_COUNTERS + LP_COUNTERS:
            if key in result:
                result[key] = None
        result["runtime_total"] = None
    return result


def apply_stage_fields(rec, counters):
    """Mirror stage counters into the top-level record fields required by the protocol."""
    if not counters:
        return
    rec["scg_classes"] = counters.get("scg_classes")
    rec["scg_edges"] = counters.get("scg_edges")
    rec["sync_states"] = counters.get("sync_states")
    rec["sync_edges"] = counters.get("sync_edges")
    rec["history_prefixes"] = counters.get("candidate_words")
    rec["retained_histories"] = counters.get("trace_records")
    rec["critical_pair_candidates"] = counters.get("terminal_pairs")
    rec["lp_calls"] = counters.get("lp_calls")
    rec["lp_infeasible"] = counters.get("lp_infeasible")
    rec["lp_feasible"] = counters.get("lp_feasible")
    rec["runtime_synchronizer_s"] = counters.get("runtime_sync")
    rec["runtime_unobservable_closure_s"] = counters.get("runtime_unobservable_closure")
    rec["runtime_history_expansion_s"] = counters.get("runtime_expansion")
    rec["runtime_critical_pair_generation_s"] = counters.get("runtime_certificate")
    rec["runtime_certificate_s"] = counters.get("runtime_certificate")
    rec["runtime_lp_build_s"] = counters.get("runtime_lp_build")
    rec["runtime_lp_solve_s"] = counters.get("runtime_lp_solve")
    rec["runtime_synchronizer_s"] = counters.get("runtime_sync")


def checkpoint_counters_placeholder():
    return None


def main() -> int:
    spec = json.loads(sys.argv[1])
    rec = {"tag": spec.get("tag"), "mode": spec.get("mode", "first"),
           "protocol_version": spec.get("protocol_version"),
           "experiment_id": spec.get("experiment_id", spec.get("experiment")),
           "benchmark_family": spec.get("benchmark_family"),
           "model_id": spec.get("model_id", spec.get("model")),
           "bounded_problem_id": spec.get("bounded_problem_id"),
           "configuration_id": spec.get("configuration_id", spec.get("tag")),
           "experiment": spec.get("experiment"), "model": spec.get("model"),
           "seed": spec.get("seed"), "replicate": spec.get("replicate"),
           "replicate_id": spec.get("replicate", 0),
           "L": spec.get("L"), "H": spec.get("H"), "params": spec["params"],
           "verdict": None, "execution_status": "ERROR", "error": None,
           "analysis_status": "NOT_RUN", "inconclusive_reason": None,
           "model_hash": None, "n_places": None, "n_transitions": None, "n_arcs": None,
           "structure_hash": None,
           "counters": None, "runtime_s": None, "python": platform.python_version(),
           "timeout_s": spec.get("timeout_s", 120),
           "partial_workload": False, "failure_stage": "MODEL_GENERATION",
           "model_attributes": None, "validation_status": "NOT_RUN",
           "peak_rss_mb": None,
           "scg_cache_status": None, "scg_cache_key": None,
           "scg_cache_provenance": None,
           "runtime_scg_build_s": None, "runtime_scg_load_s": None,
           "scg_classes": None, "scg_edges": None, "sync_states": None,
           "sync_edges": None, "history_prefixes": None,
           "retained_histories": None, "critical_pair_candidates": None,
           "lp_calls": None, "lp_infeasible": None, "lp_feasible": None,
           "runtime_synchronizer_s": None,
           "runtime_unobservable_closure_s": None,
           "runtime_history_expansion_s": None,
           "runtime_critical_pair_generation_s": None,
           "runtime_certificate_s": None,
           "runtime_lp_build_s": None, "runtime_lp_solve_s": None,
           "runtime_total_s": None, "peak_rss_mib": None,
           "cache_hits": None, "cache_misses": None, "timeout_reason": None}
    checkpoint_path = os.environ.get("CHECKPOINT_PATH")

    def checkpoint(stage, counters=None):
        rec["failure_stage"] = stage
        if counters is not None:
            rec["counters"] = checkpoint_counters(stage, counters)
            apply_stage_fields(rec, rec["counters"])
        rec["peak_rss_mb"] = peak_rss_mb()
        rec["peak_rss_mib"] = rec["peak_rss_mb"]
        if not checkpoint_path:
            return
        path = Path(checkpoint_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(rec, default=str, sort_keys=True) + "\n")
        tmp.replace(path)
    try:
        params = dict(spec["params"])
        if "v6_family" in params:
            tpn, L, H, attributes = generate_v6_model(**params)
            rec["model_attributes"] = attributes
        elif "v5_family" in params:
            tpn, L, H, attributes = generate_v5_model(**params)
            rec["model_attributes"] = attributes
        else:
            tpn, L, H = generate_rmt_fms(**params)
        errs = validate_model(tpn)
        rec.update({"n_places": len(tpn.places), "n_transitions": len(tpn.transitions),
                    "n_arcs": len(tpn.arcs)})
        checkpoint("MODEL_GENERATED")
        if errs:
            rec.update({"error": "VALIDATION: " + "; ".join(errs), "verdict": "INC",
                        "execution_status": "VALIDATION_FAILURE",
                        "termination_status": "VALIDATION_FAILURE",
                        "validation_status": "FAIL",
                        "failure_stage": "MODEL_VALIDATION"})
            print(json.dumps(rec)); return 0
        rec["model_hash"] = model_hash(tpn)
        rec["structure_hash"] = structure_hash(tpn)
        if spec.get("expected_model_hash") and rec["model_hash"] != spec["expected_model_hash"]:
            raise ValueError("generated model hash differs from frozen task")
        if spec.get("expected_structure_hash") and rec["structure_hash"] != spec["expected_structure_hash"]:
            raise ValueError("generated structure hash differs from frozen task")
        rec["validation_status"] = "PASS"
        checkpoint("MODEL_VALIDATED")
        # Same-model SCG reuse.  The cache is keyed on the model hash *and* the
        # executed source / SCG-builder / semantics / Python / format versions,
        # so a hit is only ever the same construction for the same net under the
        # same code.  Construction and load times are recorded separately from
        # the algorithm counters; a hit must never look like construction work.
        source_sha = (
            os.environ.get("EXECUTED_SOURCE_SHA")
            or os.environ.get("GITHUB_SHA")
            or "unknown"
        )
        scg_cache = SCGCache.from_environment()
        prebuilt_scg = None
        scg_build_seconds = None
        if not scg_cache.enabled:
            rec["scg_cache_status"] = "disabled"
        else:
            key_material = build_key_material(
                model_hash=rec["model_hash"], source_sha=source_sha
            )
            cache_key = compute_cache_key(key_material)
            rec["scg_cache_key"] = cache_key
            loaded = scg_cache.load(cache_key)
            if loaded is not None:
                prebuilt_scg, cache_provenance = loaded
                rec["scg_cache_status"] = "hit"
                rec["runtime_scg_build_s"] = 0.0
                rec["runtime_scg_load_s"] = cache_provenance.get("load_s")
                rec["scg_cache_provenance"] = cache_provenance
            else:
                # A miss is decided (and checkpointed) before the build so a
                # wall-clock kill during construction still reports "miss"
                # rather than "unknown".
                rec["scg_cache_status"] = "miss"
                rec["runtime_scg_load_s"] = 0.0
                checkpoint("MODEL_VALIDATED")
                t_build = time.perf_counter()
                prebuilt_scg = build_scg(tpn)
                scg_build_seconds = time.perf_counter() - t_build
                rec["runtime_scg_build_s"] = scg_build_seconds
                cache_provenance = scg_cache.store(
                    cache_key, key_material, prebuilt_scg,
                    build_seconds=scg_build_seconds,
                )
                rec["scg_cache_provenance"] = cache_provenance
            checkpoint("MODEL_VALIDATED")
        # Stage-level instrumentation is on by default; it only accumulates
        # counters/timings and never changes the search order or results.
        profile_enabled = (
            os.environ.get("V6_PROFILE", "1").strip().lower()
            not in {"0", "false", "no"}
        )
        profile = {} if profile_enabled else None
        t0 = time.perf_counter()
        out = verify(tpn, spec.get("L", L), spec.get("H", H),
                     complete_set=(rec["mode"] == "complete"),
                     progress_hook=checkpoint,
                     prebuilt_scg=prebuilt_scg,
                     scg_build_seconds=scg_build_seconds,
                     profile=profile)
        rec["runtime_s"] = time.perf_counter() - t0
        rec["verdict"] = out.get("result")
        if rec["verdict"] not in {"VER", "AMB", "INC"}:
            raise ValueError(f"unknown verifier result: {rec['verdict']!r}")
        rec["execution_status"] = "COMPLETED"
        rec["analysis_status"] = (
            "INCONCLUSIVE" if rec["verdict"] == "INC" else "CONCLUSIVE"
        )
        rec["inconclusive_reason"] = out.get("inconclusive_reason")
        rec["termination_status"] = (
            "INC_STATE_CLASS_LIMIT"
            if rec["verdict"] == "INC" and rec["inconclusive_reason"] == "SCG_STATE_LIMIT"
            else rec["verdict"]
        )
        rec["failure_stage"] = (
            "SCG" if rec["verdict"] == "INC" and out.get("inconclusive_reason") in {
                "SCG_STATE_LIMIT", "COMPLETION_UNREACHABLE", "ZERO_TIME_UNOBSERVABLE_CYCLE"
            } else ("BOUNDED_ANALYSIS" if rec["verdict"] == "INC" else None)
        )
        rec["enumeration_complete"] = out.get("enumeration_complete")
        rec["critical_pair_count"] = len(out.get("certificates", []) or [])
        st = out.get("stats", {})
        rec["counters"] = normalized_counters(out)
        apply_stage_fields(rec, rec["counters"])
        rec["runtime_total_s"] = st.get("runtime_total")
        rec["runtime_scg_build_s"] = rec.get("runtime_scg_build_s")
        if rec.get("scg_cache_status") == "hit":
            rec["cache_hits"] = 1
        elif rec.get("scg_cache_status") == "miss":
            rec["cache_misses"] = 1
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["termination_status"] = "ERROR"
    final_stage = rec.get("failure_stage") or "COMPLETE"
    checkpoint(final_stage, rec.get("counters"))
    rec["peak_rss_mb"] = peak_rss_mb()
    if rec.get("execution_status") == "COMPLETED" and rec.get("verdict") != "INC":
        rec["failure_stage"] = None
    print(json.dumps(rec, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
