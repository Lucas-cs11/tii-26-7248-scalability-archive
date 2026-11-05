#!/usr/bin/env python3
"""Run one bounded-verification cell; print one JSON record.

usage: run_cell.py '<json spec>'
spec keys: tag, params{...}, L, H, method, timeout_s
"""
from __future__ import annotations
import hashlib, json, platform, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.benchmarks.rmt_fms_generator import generate_rmt_fms
from src.model.validation import validate_model
from src.verifier import verify
from src.baselines.mscg_lp_complete import verify_mscg_lp_complete

FNS = {"main": verify, "mscg_lp_complete": verify_mscg_lp_complete}


def model_hash(tpn) -> str:
    data = {"places": sorted(tpn.places.keys()),
            "transitions": sorted([{"name": n, "label": t.label, "fault": t.is_fault,
                                    "earliest": str(t.interval.earliest),
                                    "latest": str(t.interval.latest)}
                                   for n, t in tpn.transitions.items()], key=lambda x: x["name"]),
            "arcs": sorted(f"{a.from_node}->{a.to_node}" for a in tpn.arcs),
            "initial_marking": sorted(tpn.initial_marking.items()),
            "completion_markings": [sorted(m.items()) for m in tpn.completion_markings]}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def main() -> int:
    spec = json.loads(sys.argv[1])
    rec = {"tag": spec.get("tag"), "method": spec.get("method", "main"),
           "L": spec.get("L"), "H": spec.get("H"), "params": spec["params"],
           "verdict": None, "execution_status": "ERROR", "error": None,
           "model_hash": None, "n_places": None, "n_transitions": None,
           "counters": None, "runtime_total_s": None,
           "python": platform.python_version(),
           "timeout_s": spec.get("timeout_s", 120)}
    try:
        tpn, L, H = generate_rmt_fms(**spec["params"])
        errs = validate_model(tpn)
        rec.update({"n_places": len(tpn.places), "n_transitions": len(tpn.transitions)})
        if errs:
            rec.update({"error": "VALIDATION: " + "; ".join(errs), "verdict": "INC"})
            print(json.dumps(rec)); return 0
        rec["model_hash"] = model_hash(tpn)
        t0 = time.perf_counter()
        out = FNS[rec["method"]](tpn, spec.get("L", L), spec.get("H", H))
        rec["runtime_total_s"] = time.perf_counter() - t0
        rec["verdict"] = out.get("result")
        rec["execution_status"] = "COMPLETED"
        st = out.get("stats", {})
        rec["counters"] = {k: st.get(k) for k in
                           ("scg_classes", "scg_edges", "sync_states", "candidate_words",
                            "temporal_states", "trace_records", "terminal_pairs",
                            "lp_calls", "confirmed_critical_pairs")}
        rec["run_runtime_total_s"] = st.get("runtime_total")
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(rec, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
