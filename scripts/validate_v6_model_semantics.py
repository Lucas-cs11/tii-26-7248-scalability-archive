#!/usr/bin/env python3
"""Validate exact timed witnesses and invariants for all v6 model families."""
from __future__ import annotations

import argparse
from collections import deque
from fractions import Fraction
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.run_cell import model_hash, structure_hash
from src.benchmarks.v6_families import FAMILY_SPECS, generate_v6_model
from src.model.validation import validate_model
from src.semantics.fraction_oracle import check_firing_sequence
from src.semantics.strong_time import TimedState, fire_transition, initial_timed_state, max_delay


def _fire(tpn, marking, sequence, transition):
    if transition not in tpn.transitions or not tpn.is_enabled(marking, transition):
        raise ValueError(f"cannot fire {transition} from {marking}")
    sequence.append(transition)
    return tpn.fire(marking, transition)


def _enabled_with_prefix(tpn, marking, prefix):
    matches = sorted(
        name for name in tpn.enabled_set(marking) if name.startswith(prefix)
    )
    if len(matches) != 1:
        raise ValueError(f"expected one enabled {prefix!r}, got {matches}")
    return matches[0]


def _execute_operation(
    tpn, marking, sequence, part, operation, *, fault=False, inspect=False
):
    if fault:
        transition = _enabled_with_prefix(
            tpn, marking, f"t_fault_j{part}_op{operation}_"
        )
        marking = _fire(tpn, marking, sequence, transition)
        recovery = transition.replace("t_fault_", "t_recov_", 1)
        marking = _fire(tpn, marking, sequence, recovery)
    else:
        transfer = _enabled_with_prefix(
            tpn, marking, f"t_trans_j{part}_op{operation}_"
        )
        marking = _fire(tpn, marking, sequence, transfer)

    process = _enabled_with_prefix(tpn, marking, f"t_proc_j{part}_op{operation}_")
    marking = _fire(tpn, marking, sequence, process)

    inspect_choices = sorted(
        name for name in tpn.enabled_set(marking)
        if name.startswith(f"t_choose_inspect_j{part}_op{operation}_")
    )
    if inspect_choices:
        choice_prefix = (
            f"t_choose_inspect_j{part}_op{operation}_" if inspect
            else f"t_choose_skip_j{part}_op{operation}_"
        )
        choice = _enabled_with_prefix(tpn, marking, choice_prefix)
        marking = _fire(tpn, marking, sequence, choice)
        if inspect:
            timed = _enabled_with_prefix(
                tpn, marking, f"t_inspect_j{part}_op{operation}_"
            )
            marking = _fire(tpn, marking, sequence, timed)
    elif inspect:
        raise ValueError(f"operation {part}/{operation} has no inspection branch")
    return marking


def build_complete_witness(tpn, attrs, *, fault_at=None, inspect_at=None, rework_at=None):
    marking = dict(tpn.initial_marking)
    sequence = []
    parts = attrs["number_of_parts"]
    operations = attrs["operations_per_part"]
    released_through = min(attrs["WIP"], parts)
    for part in range(1, released_through + 1):
        marking = _fire(tpn, marking, sequence, f"t_release_j{part}")
    for part in range(1, parts + 1):
        for operation in range(1, operations + 1):
            location = (part, operation)
            marking = _execute_operation(
                tpn, marking, sequence, part, operation,
                fault=location == fault_at, inspect=location == inspect_at,
            )
            if location == rework_at:
                marking = _fire(
                    tpn, marking, sequence,
                    f"t_choose_rework_j{part}_op{operation}",
                )
                marking = _fire(
                    tpn, marking, sequence, f"t_rework_j{part}_op{operation}"
                )
                marking = _execute_operation(
                    tpn, marking, sequence, part, operation
                )

            continue_choice = f"t_choose_continue_j{part}_op{operation}"
            if continue_choice in tpn.transitions:
                marking = _fire(tpn, marking, sequence, continue_choice)
            if operation < operations:
                marking = _fire(
                    tpn, marking, sequence,
                    f"t_chain_j{part}_op{operation}_to_op{operation + 1}",
                )
            else:
                marking = _fire(tpn, marking, sequence, f"t_complete_j{part}")
        if released_through < parts:
            released_through += 1
            marking = _fire(
                tpn, marking, sequence, f"t_release_j{released_through}"
            )
    marking = _fire(tpn, marking, sequence, "t_task_done")
    if not tpn.in_completion(marking):
        raise ValueError("witness does not terminate at first completion")
    return tuple(sequence)


def _transition_location(name):
    match = re.search(r"_j([0-9]+)_op([0-9]+)(?:_|\Z)", name)
    return tuple(map(int, match.group(1, 2))) if match else None


def _allowed_transition(name, flags, *, fault_at, inspect_at, rework_at):
    location = _transition_location(name)
    fault_done, inspect_done, rework_done = flags
    if name.startswith("t_fault_"):
        return location == fault_at and not fault_done
    if name.startswith("t_trans_") and location == fault_at and not fault_done:
        return False
    if name.startswith("t_choose_inspect_"):
        return location == inspect_at and not inspect_done
    if name.startswith("t_choose_skip_") and location == inspect_at and not inspect_done:
        return False
    if name.startswith("t_choose_rework_"):
        return location == rework_at and not rework_done
    if name.startswith("t_choose_continue_") and location == rework_at and not rework_done:
        return False
    if name.startswith("t_route_alt_"):
        return False
    return True


def _updated_flags(name, flags, *, fault_at, inspect_at, rework_at):
    fault_done, inspect_done, rework_done = flags
    location = _transition_location(name)
    if name.startswith("t_fault_") and location == fault_at:
        fault_done = True
    if name.startswith("t_inspect_") and location == inspect_at:
        inspect_done = True
    if name.startswith("t_rework_") and location == rework_at:
        rework_done = True
    return fault_done, inspect_done, rework_done


def _state_key(state, flags):
    return (
        tuple(sorted(state.marking.items())),
        tuple(sorted(state.clock.items())),
        flags,
    )


def plan_complete_witness(
    tpn, *, fault_at=None, inspect_at=None, rework_at=None, state_limit=200_000
):
    """Find a concrete exact-time completion trace without using the SCG."""
    initial = initial_timed_state(tpn)
    initial_flags = (fault_at is None, inspect_at is None, rework_at is None)
    frontier = deque([(initial, initial_flags, tuple())])
    seen = {_state_key(initial, initial_flags)}
    while frontier:
        state, flags, sequence = frontier.popleft()
        if tpn.in_completion(state.marking) and all(flags):
            return sequence
        candidates = []
        delay_cap = max_delay(tpn, state)
        for name in tpn.enabled_set(state.marking):
            if not _allowed_transition(
                name, flags, fault_at=fault_at,
                inspect_at=inspect_at, rework_at=rework_at,
            ):
                continue
            transition = tpn.transitions[name]
            age = state.clock.get(name, Fraction(0))
            delay = max(Fraction(0), transition.interval.earliest - age)
            if not math.isinf(delay_cap) and delay > delay_cap:
                continue
            remaining = (
                float("inf") if transition.interval.is_infinite()
                else transition.interval.latest - age
            )
            candidates.append((delay, remaining, name))
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        for delay, _, name in candidates:
            try:
                successor = fire_transition(tpn, state, name, delay)
            except ValueError:
                continue
            new_flags = _updated_flags(
                name, flags, fault_at=fault_at,
                inspect_at=inspect_at, rework_at=rework_at,
            )
            key = _state_key(successor, new_flags)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > state_limit:
                raise RuntimeError(f"independent witness search exceeded {state_limit} states")
            frontier.append((successor, new_flags, sequence + (name,)))
    raise ValueError("no strong-time completion witness found by independent planner")


def _locations(tpn, pattern):
    found = []
    regex = re.compile(pattern)
    for name in sorted(tpn.transitions):
        match = regex.fullmatch(name)
        if match:
            found.append(tuple(map(int, match.group(1, 2))))
    return found


def _incidence_balance(tpn, weights, transition):
    consumed = sum(weights.get(arc.from_node, 0) for arc in tpn.arcs
                   if arc.to_node == transition)
    produced = sum(weights.get(arc.to_node, 0) for arc in tpn.arcs
                   if arc.from_node == transition)
    return produced - consumed


def validate_invariants(tpn, attrs):
    invariants = {}
    parts = attrs["number_of_parts"]
    piece_weights = {
        place: 1 for place in tpn.places if re.match(r"p[0-9]+_", place)
    }
    piece_weights["task_done_place"] = parts
    invariants["workpiece_conservation"] = piece_weights

    active = {
        place: 1 for place in tpn.places
        if re.match(r"p[0-9]+_", place)
        and not place.endswith("_ready") and not place.endswith("_done")
    }
    active["p_wip_capacity"] = 1
    invariants["wip_semaphore"] = active

    for machine in range(1, attrs["resource_count"] + 1):
        resource = {f"p_machine_{machine}_cap": 1}
        for place in tpn.places:
            if f"_m{machine}_cfg" in place and ("_at_" in place or "_fault_" in place):
                resource[place] = 1
        if f"p_machine_{machine}_cap" in tpn.places:
            invariants[f"machine_{machine}_capacity"] = resource

    failures = {}
    for name, weights in invariants.items():
        bad = {
            transition: _incidence_balance(tpn, weights, transition)
            for transition in tpn.transitions
            if _incidence_balance(tpn, weights, transition) != 0
        }
        if bad:
            failures[name] = bad
    return invariants, failures


def _witness_record(tpn, sequence):
    result = check_firing_sequence(tpn, sequence)
    return {
        "PASS": result.feasible,
        "length": len(sequence),
        "observable_length": sum(tpn.transitions[name].is_observable for name in sequence),
        "fault_count": sum(tpn.transitions[name].is_fault for name in sequence),
        "sequence": list(sequence),
        "event_times": [str(value) for value in result.event_times] if result.event_times else None,
        "reason": result.reason,
    }


def validate_family(family, seed):
    tpn, default_l, default_h, attrs = generate_v6_model(family, seed)
    errors = validate_model(tpn)
    invariants, invariant_failures = validate_invariants(tpn, attrs)
    witnesses = {"normal_completion": _witness_record(
        tpn, plan_complete_witness(tpn)
    )}

    faults = _locations(tpn, r"t_fault_j([0-9]+)_op([0-9]+)_.*")
    inspections = _locations(tpn, r"t_choose_inspect_j([0-9]+)_op([0-9]+)_.*")
    reworks = _locations(tpn, r"t_rework_j([0-9]+)_op([0-9]+)")
    if faults:
        witnesses["fault_completion"] = _witness_record(
            tpn, plan_complete_witness(tpn, fault_at=faults[0])
        )
    if inspections:
        witnesses["inspection_completion"] = _witness_record(
            tpn, plan_complete_witness(tpn, inspect_at=inspections[0])
        )
    if reworks:
        witnesses["rework_completion"] = _witness_record(
            tpn, plan_complete_witness(tpn, rework_at=reworks[0])
        )

    concurrency = tuple(
        f"t_release_j{part}" for part in range(1, attrs["WIP"] + 1)
    )
    witnesses["wip_concurrency"] = _witness_record(tpn, concurrency)

    new_observable_choices = [
        name for name, transition in tpn.transitions.items()
        if name.startswith("t_choose_") and transition.is_observable
    ]
    required = ["normal_completion", "fault_completion", "wip_concurrency"]
    if inspections:
        required.append("inspection_completion")
    if reworks:
        required.append("rework_completion")
    passed = (
        not errors and not invariant_failures and not new_observable_choices
        and all(witnesses[name]["PASS"] for name in required)
    )
    return {
        "PASS": passed,
        "family": family,
        "seed": seed,
        "model_hash": model_hash(tpn),
        "structure_hash": structure_hash(tpn),
        "places": len(tpn.places),
        "transitions": len(tpn.transitions),
        "arcs": len(tpn.arcs),
        "default_L": default_l,
        "default_H": default_h,
        "attributes": attrs,
        "validation_errors": errors,
        "invariant_failures": invariant_failures,
        "invariant_count": len(invariants),
        "new_observable_choice_transitions": new_observable_choices,
        "witnesses": witnesses,
        "L_interpretation": (
            "Immediate route-choice transitions are unobservable and do not directly "
            "increase L; selected inspection adds one observable event, while rework "
            "adds its declared observable event plus the repeated operation history."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    witness_dir = args.out / "family_semantic_witnesses"
    witness_dir.mkdir(parents=True, exist_ok=True)

    families = []
    for family in FAMILY_SPECS:
        result = validate_family(family, args.seed)
        families.append(result)
        (witness_dir / f"{family}.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n"
        )
    summary = {
        "PASS": all(item["PASS"] for item in families),
        "gate": "ALL_12_FAMILIES_SEMANTIC_PASS",
        "families_checked": len(families),
        "families_passed": sum(item["PASS"] for item in families),
        "results": families,
    }
    (args.out / "generator_regression_results.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({key: summary[key] for key in (
        "PASS", "gate", "families_checked", "families_passed"
    )}, indent=2))
    return 0 if summary["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
