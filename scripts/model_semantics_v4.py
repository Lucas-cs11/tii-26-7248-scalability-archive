#!/usr/bin/env python3
"""Reachability witness for the formal WIP=1/2/3 semantics."""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.benchmarks.rmt_fms_generator import generate_rmt_fms


def marking_key(marking):
    return tuple(sorted(marking.items()))


def probe(wip):
    tpn, _, _ = generate_rmt_fms(
        n_machines=4, n_parts=3, ops_per_part=1, wip_bound=wip,
        unobservable_ratio=0.5, fault_locations=2,
        interval_width_factor=1.0, adjustable_ratio=0.25,
        seed=0, n_configs=1, inspection_locations=0,
    )
    queue = deque([dict(tpn.initial_marking)])
    seen = {marking_key(tpn.initial_marking)}
    max_in_process = 0
    witness = None
    completion_reachable = False
    while queue:
        marking = queue.popleft()
        in_process = wip - marking.get("p_wip_capacity", 0)
        if in_process > max_in_process:
            max_in_process = in_process
            witness = dict(marking)
        if tpn.in_completion(marking):
            completion_reachable = True
            continue
        for transition in tpn.enabled_set(marking):
            nxt = tpn.fire(marking, transition)
            key = marking_key(nxt)
            if key not in seen:
                seen.add(key)
                queue.append(nxt)
    return {
        "wip_bound": wip,
        "reachable_markings_untimed": len(seen),
        "max_parts_in_process": max_in_process,
        "target_reached": max_in_process == wip,
        "completion_reachable": completion_reachable,
        "witness_marking": witness,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    result = {"PASS": True, "cases": [probe(wip) for wip in (1, 2, 3)]}
    result["PASS"] = all(
        case["target_reached"] and case["completion_reachable"]
        for case in result["cases"]
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
