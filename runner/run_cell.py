#!/usr/bin/env python3
"""One bounded-verification cell. Prints a single JSON record.

spec: {tag, experiment, model, seed, replicate, L, H, mode, timeout_s, params}
mode: "first"    -> verifier default (first-certificate, production behaviour)
      "complete" -> complete_set=True (full candidate/history/LP accounting)
"""
from __future__ import annotations
import hashlib, json, os, platform, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.benchmarks.rmt_fms_generator import generate_rmt_fms
from src.benchmarks.v5_families import generate_v5_model
from src.model.validation import validate_model
from src.verifier import verify


def model_hash(tpn) -> str:
    data = {"places": sorted(tpn.places.keys()),
            "transitions": sorted([{"name": n, "label": t.label, "fault": t.is_fault,
                                    "earliest": str(t.interval.earliest),
                                    "latest": str(t.interval.latest)}
                                   for n, t in tpn.transitions.items()], key=lambda x: x["name"]),
            "arcs": sorted(f"{a.from_node}->{a.to_node}" for a in tpn.arcs),
            "initial_marking": sorted(tpn.initial_marking.items()),
            "completion_markings": [sorted(m.items()) for m in tpn.completion_markings]}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def structure_hash(tpn) -> str:
    """Hash static TPN structure while excluding WIP-dependent markings."""
    data = {"places": sorted(tpn.places.keys()),
            "transitions": sorted([{"name": n, "label": t.label, "fault": t.is_fault,
                                    "earliest": str(t.interval.earliest),
                                    "latest": str(t.interval.latest)}
                                   for n, t in tpn.transitions.items()], key=lambda x: x["name"]),
            "arcs": sorted(f"{a.from_node}->{a.to_node}" for a in tpn.arcs)}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def main() -> int:
    spec = json.loads(sys.argv[1])
    rec = {"tag": spec.get("tag"), "mode": spec.get("mode", "first"),
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
           "model_attributes": None}
    checkpoint_path = os.environ.get("CHECKPOINT_PATH")

    def checkpoint(stage, counters=None):
        rec["failure_stage"] = stage
        if counters is not None:
            rec["counters"] = counters
        if not checkpoint_path:
            return
        path = Path(checkpoint_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(rec, default=str, sort_keys=True) + "\n")
        tmp.replace(path)
    try:
        params = dict(spec["params"])
        if "v5_family" in params:
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
                        "failure_stage": "MODEL_VALIDATION"})
            print(json.dumps(rec)); return 0
        rec["model_hash"] = model_hash(tpn)
        rec["structure_hash"] = structure_hash(tpn)
        checkpoint("MODEL_VALIDATED")
        t0 = time.perf_counter()
        out = verify(tpn, spec.get("L", L), spec.get("H", H),
                     complete_set=(rec["mode"] == "complete"),
                     progress_hook=checkpoint)
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
        rec["failure_stage"] = None
        rec["enumeration_complete"] = out.get("enumeration_complete")
        rec["critical_pair_count"] = len(out.get("certificates", []) or [])
        st = out.get("stats", {})
        rec["counters"] = {k: st.get(k) for k in
                           ("scg_classes", "scg_edges", "sync_states", "candidate_words",
                            "temporal_states", "trace_records", "terminal_pairs",
                            "lp_calls", "confirmed_critical_pairs")}
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["termination_status"] = "ERROR"
    final_stage = rec.get("failure_stage") or "COMPLETE"
    checkpoint(final_stage, rec.get("counters"))
    if rec.get("execution_status") == "COMPLETED":
        rec["failure_stage"] = None
    print(json.dumps(rec, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
