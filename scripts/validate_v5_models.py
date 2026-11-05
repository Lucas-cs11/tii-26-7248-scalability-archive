#!/usr/bin/env python3
"""Untimed semantic validation for v5 synthetic benchmark models."""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.run_cell import model_hash
from src.benchmarks.v5_families import FAMILY_SPECS, generate_v5_model
from src.model.validation import validate_model


def marking_key(marking):
    return tuple(sorted(marking.items()))


def validate_one(family: str, seed: int, wip: int | None = None, limit: int = 250_000):
    kwargs = {"v5_family": family, "seed": seed}
    if wip is not None:
        kwargs["wip_bound"] = wip
    tpn, default_L, default_H, attributes = generate_v5_model(**kwargs)
    structural_errors = validate_model(tpn)
    queue = deque([dict(tpn.initial_marking)])
    seen = {marking_key(tpn.initial_marking)}
    fired = set()
    fault_enabled = set()
    completion_reachable = False
    max_in_process = 0
    truncated = False
    while queue:
        marking = queue.popleft()
        max_in_process = max(
            max_in_process,
            attributes["WIP"] - marking.get("p_wip_capacity", 0),
        )
        if tpn.in_completion(marking):
            completion_reachable = True
            continue
        for transition in tpn.enabled_set(marking):
            fired.add(transition)
            if tpn.transitions[transition].is_fault:
                fault_enabled.add(transition)
            nxt = tpn.fire(marking, transition)
            key = marking_key(nxt)
            if key not in seen:
                seen.add(key)
                queue.append(nxt)
                if len(seen) >= limit:
                    truncated = True
                    queue.clear()
                    break
    reworks = {name for name in tpn.transitions if name.startswith("t_rework_")}
    routes = {name for name in tpn.transitions if name.startswith("t_route_alt_")}
    unreachable_transitions = sorted(set(tpn.transitions) - fired)
    required_concurrency = min(attributes["WIP"], attributes["number_of_parts"])
    passed = all([
        not structural_errors,
        not truncated,
        completion_reachable,
        bool(fault_enabled),
        max_in_process == required_concurrency,
        reworks <= fired,
        routes <= fired,
        not unreachable_transitions,
    ])
    return {
        "PASS": passed,
        "benchmark_family": family,
        "seed": seed,
        "model_hash": model_hash(tpn),
        "default_L": default_L,
        "default_H": default_H,
        "number_of_places": len(tpn.places),
        "number_of_transitions": len(tpn.transitions),
        "number_of_arcs": len(tpn.arcs),
        "reachable_markings_untimed": len(seen),
        "reachability_truncated": truncated,
        "completion_reachable": completion_reachable,
        "fault_transitions_reachable": len(fault_enabled),
        "max_parts_in_process": max_in_process,
        "required_reachable_concurrency": required_concurrency,
        "rework_transitions": len(reworks),
        "route_alternative_transitions": len(routes),
        "unreachable_transitions": unreachable_transitions,
        "structural_errors": structural_errors,
        "attributes": attributes,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="0")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    seeds = [int(value) for value in args.seeds.split(",")]
    cases = [
        validate_one(family, seed)
        for family in FAMILY_SPECS
        for seed in seeds
    ]
    result = {"PASS": all(case["PASS"] for case in cases), "cases": cases}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "PASS": result["PASS"],
        "cases": len(cases),
        "failed": [f"{c['benchmark_family']}:s{c['seed']}" for c in cases if not c["PASS"]],
        "max_reachable_markings": max(c["reachable_markings_untimed"] for c in cases),
    }, indent=2))
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
