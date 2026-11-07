#!/usr/bin/env python3
"""Version-agnostic differential driver.

Runs one bounded-verification cell against an arbitrary repository checkout and
dumps a canonical, comparable JSON document.  The driver lives in the optimized
tree but imports *everything* from ``--root``, so the frozen baseline commit can
be measured without modifying it.

Usage:
    python scripts/v6_diff_driver.py --root /path/to/checkout --spec '<json>'
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from fractions import Fraction


def canonical_certificate(cert):
    return {
        "phi": list(cert.phi),
        "sigma_N": list(cert.sigma_N),
        "sigma_F": list(cert.sigma_F),
        "overlap": [str(value) for value in cert.overlap],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec)
    sys.path.insert(0, args.root)
    from src.benchmarks.v6_families import generate_v6_model  # noqa: E402
    from src.model.validation import validate_model  # noqa: E402
    from src.verifier import verify  # noqa: E402

    record = {
        "task_id": spec.get("task_id"),
        "execution_status": "ERROR",
        "error": None,
        "wall_s": None,
        "verdict": None,
        "inconclusive_reason": None,
        "certificate_mode": None,
        "enumeration_complete": None,
        "certificates": [],
        "stats": {},
        "model_hash": None,
    }
    started = time.perf_counter()
    try:
        tpn, default_l, default_h, _ = generate_v6_model(**spec["params"])
        errors = validate_model(tpn)
        if errors:
            raise ValueError("model validation failed: " + "; ".join(errors))
        from runner.run_cell import model_hash  # noqa: E402

        record["model_hash"] = model_hash(tpn)
        L = spec.get("L", default_l)
        H = spec.get("H", default_h)
        result = verify(
            tpn,
            int(L),
            float(H),
            complete_set=(spec.get("mode") == "complete"),
        )
        record.update({
            "execution_status": "COMPLETED",
            "verdict": result.get("result"),
            "inconclusive_reason": result.get("inconclusive_reason"),
            "certificate_mode": result.get("certificate_mode"),
            "enumeration_complete": result.get("enumeration_complete"),
            "certificates": [
                canonical_certificate(cert) for cert in result.get("certificates", [])
            ],
            "stats": {
                key: value for key, value in (result.get("stats") or {}).items()
                if not key.startswith("runtime_")
            },
        })
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["wall_s"] = round(time.perf_counter() - started, 3)
    print(json.dumps(record, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
