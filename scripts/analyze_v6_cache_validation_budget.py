#!/usr/bin/env python3
"""Recompute the formal v6 cache-validation budget projection from raw JSONL.

This is a thin, standalone entry point around the audit pipeline: it reads the
raw ``raw/shard_XXX.jsonl`` artifacts together with the frozen manifest / task
CSVs (never a runner summary), and writes ``budget_projection.json``.

The audit pipeline itself lives in ``audit_v6_cache_validation_results.py`` so
that the audit JSON and the budget JSON can never disagree about the measured
sample.  Use the audit script directly if you want all five audit outputs.

Usage:
    python scripts/analyze_v6_cache_validation_budget.py \\
        --manifest protocol/v6_cache_validation_manifest.json \\
        --tasks protocol/v6_cache_validation_tasks \\
        --artifacts work/v6_cache_validation_<runid> \\
        --run-id <runid> --source-sha <sha> --out <dir>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_v6_cache_validation_results as core  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Recompute the v6 formal-matrix budget projection from raw artifacts."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--audit-json",
        action="store_true",
        help="also write cache_validation_audit.json next to budget_projection.json",
    )
    args = parser.parse_args(argv)

    payload = core.run_pipeline(
        args.manifest, args.tasks, args.artifacts, args.run_id, args.source_sha
    )
    budget = payload["budget"]
    result = payload["result"]
    args.out.mkdir(parents=True, exist_ok=True)
    budget_path = args.out / "budget_projection.json"
    budget_path.write_text(
        json.dumps(budget, indent=2, sort_keys=True, default=str) + "\n"
    )
    if args.audit_json:
        (args.out / "cache_validation_audit.json").write_text(
            json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
        )
    print(
        json.dumps(
            {
                "budget_json": str(budget_path),
                "audit_PASS": result["PASS"],
                "CACHE_INTEGRITY_PASS": budget["gates"]["CACHE_INTEGRITY_PASS"],
                "CACHE_DIFFERENTIAL_PASS": budget["gates"]["CACHE_DIFFERENTIAL_PASS"],
                "BUDGET_COMPLETE": budget["gates"]["BUDGET_COMPLETE"],
                "budget": budget["gates"]["budget"],
                "missing_data": budget["missing_data"],
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
