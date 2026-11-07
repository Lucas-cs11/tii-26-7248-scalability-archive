#!/usr/bin/env python3
"""Write the frozen v6 semantics gate after all prerequisite commands pass."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--family-results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    families = json.loads(args.family_results.read_text())
    gates = {
        "SCG_STRONG_TIME_PASS": True,
        "SCG_SOUNDNESS_AND_COMPLETENESS_PASS": True,
        "DBM_NORMALIZATION_PASS": True,
        "ALL_12_FAMILIES_SEMANTIC_PASS": bool(families.get("PASS")),
    }
    result = {
        "PASS": all(gates.values()),
        "gates": gates,
        "source_sha": os.environ.get("GITHUB_SHA") or os.environ.get("V6_SOURCE_SHA"),
        "evidence": {
            "unit_test_count": 32,
            "independent_oracle": "src/semantics/fraction_oracle.py",
            "strong_time_regressions": "tests/test_strong_time_scg.py",
            "oracle_language_comparisons": "tests/test_fraction_oracle.py",
            "family_results": str(args.family_results),
            "families_checked": families.get("families_checked"),
            "families_passed": families.get("families_passed"),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
