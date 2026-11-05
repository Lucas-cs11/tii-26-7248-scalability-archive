#!/usr/bin/env python3
"""One bounded-verification cell. Prints a single JSON record.

spec: {tag, experiment, model, seed, replicate, L, H, mode, timeout_s, params}
mode: "first"    -> verifier default (first-certificate, production behaviour)
      "complete" -> complete_set=True (full candidate/history/LP accounting)
"""
from __future__ import annotations
import hashlib, json, platform, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.benchmarks.rmt_fms_generator import generate_rmt_fms
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


def main() -> int:
    spec = json.loads(sys.argv[1])
    rec = {"tag": spec.get("tag"), "mode": spec.get("mode", "first"),
           "experiment": spec.get("experiment"), "model": spec.get("model"),
           "seed": spec.get("seed"), "replicate": spec.get("replicate"),
           "L": spec.get("L"), "H": spec.get("H"), "params": spec["params"],
           "verdict": None, "execution_status": "ERROR", "error": None,
           "model_hash": None, "n_places": None, "n_transitions": None, "n_arcs": None,
           "counters": None, "runtime_s": None, "python": platform.python_version(),
           "timeout_s": spec.get("timeout_s", 120)}
    try:
        tpn, L, H = generate_rmt_fms(**spec["params"])
        errs = validate_model(tpn)
        rec.update({"n_places": len(tpn.places), "n_transitions": len(tpn.transitions),
                    "n_arcs": len(tpn.arcs)})
        if errs:
            rec.update({"error": "VALIDATION: " + "; ".join(errs), "verdict": "INC",
                        "execution_status": "VALIDATION_FAILURE"})
            print(json.dumps(rec)); return 0
        rec["model_hash"] = model_hash(tpn)
        t0 = time.perf_counter()
        out = verify(tpn, spec.get("L", L), spec.get("H", H),
                     complete_set=(rec["mode"] == "complete"))
        rec["runtime_s"] = time.perf_counter() - t0
        rec["verdict"] = out.get("result")
        rec["execution_status"] = "COMPLETED"
        rec["enumeration_complete"] = out.get("enumeration_complete")
        rec["critical_pair_count"] = len(out.get("certificates", []) or [])
        st = out.get("stats", {})
        rec["counters"] = {k: st.get(k) for k in
                           ("scg_classes", "scg_edges", "sync_states", "candidate_words",
                            "temporal_states", "trace_records", "terminal_pairs",
                            "lp_calls", "confirmed_critical_pairs")}
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(rec, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
