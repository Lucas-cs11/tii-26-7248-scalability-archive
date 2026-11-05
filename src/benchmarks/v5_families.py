"""Predeclared synthetic TPN families for the v5 scalability benchmark.

The families deliberately combine a small number of auditable structural
features.  They are synthetic benchmark models, not industrial measurements.
All cycles are positive-time production rework loops reachable before the
first task completion; no zero-time loop is introduced.
"""
from __future__ import annotations

import random
from dataclasses import replace
from fractions import Fraction
from typing import Dict, Tuple

from .rmt_fms_generator import generate_rmt_fms
from ..model.tpn import Arc, Interval, TPN, Transition


FAMILY_SPECS: Dict[str, dict] = {
    "F01_sequential_sparse": dict(parts=1, ops=6, machines=4, wip=1, unobs=0.70, inspections=1),
    "F02_sequential_dense": dict(parts=1, ops=8, machines=5, wip=1, unobs=0.10, inspections=2),
    "F03_deep_inspection": dict(parts=1, ops=10, machines=5, wip=1, unobs=0.40, inspections=6),
    "F04_alternative_routes": dict(parts=2, ops=5, machines=5, wip=1, unobs=0.35, inspections=2, routes=3),
    "F05_routes_inspections": dict(parts=2, ops=6, machines=4, wip=1, unobs=0.50, inspections=5, routes=4),
    "F06_parallel_parts": dict(parts=3, ops=4, machines=6, wip=3, unobs=0.35, inspections=3),
    "F07_shared_contention": dict(parts=3, ops=4, machines=2, wip=3, unobs=0.45, inspections=2),
    "F08_mixed_visibility": dict(parts=3, ops=5, machines=4, wip=2, unobs=0.60, inspections=4, routes=2),
    "F09_deep_fault_stages": dict(parts=2, ops=8, machines=6, wip=2, unobs=0.45, inspections=4, routes=2),
    "F10_bounded_rework": dict(parts=1, ops=8, machines=4, wip=1, unobs=0.35, inspections=3, reworks=2, h_mult=2.0),
    "F11_resource_cycle": dict(parts=3, ops=4, machines=2, wip=2, unobs=0.50, inspections=3, reworks=1, h_mult=2.0),
    "F12_sparse_cycle": dict(parts=2, ops=7, machines=3, wip=2, unobs=0.75, inspections=2, routes=2, reworks=2, h_mult=2.5),
}


def _with_structural_features(
    tpn: TPN,
    *,
    seed: int,
    n_parts: int,
    ops_per_part: int,
    route_choices: int,
    rework_locations: int,
    processing_observable_ratio: float,
) -> TPN:
    places = dict(tpn.places)
    transitions = dict(tpn.transitions)
    arcs = list(tpn.arcs)
    rng = random.Random(0x5A17 + seed)

    # Independently control processing-label density while leaving the physical
    # processing path and firing intervals unchanged.
    proc_names = sorted(name for name in transitions if name.startswith("t_proc_"))
    keep_count = round(len(proc_names) * processing_observable_ratio)
    keep = set(rng.sample(proc_names, min(len(proc_names), max(0, keep_count))))
    for name in proc_names:
        if name not in keep:
            transitions[name] = replace(transitions[name], label="")

    # Alternative routing decisions are positive-time competing transitions
    # between consecutive operations.  Both branches converge to the same next
    # operation, preserving the task definition while adding genuine histories.
    chain_names = sorted(name for name in transitions if name.startswith("t_chain_"))
    selected_routes = set(rng.sample(chain_names, min(route_choices, len(chain_names))))
    for name in selected_routes:
        original = transitions[name]
        transitions[name] = replace(
            original,
            label=name.replace("t_chain_", "route_direct_"),
            interval=Interval(Fraction(0), Fraction(1)),
        )
        alt = name.replace("t_chain_", "t_route_alt_")
        transitions[alt] = Transition(
            name=alt,
            label=alt.replace("t_route_alt_", "route_alt_"),
            is_fault=False,
            is_adjustable=False,
            interval=Interval(Fraction(1), Fraction(2)),
        )
        for arc in [arc for arc in arcs if arc.to_node == name]:
            arcs.append(Arc(arc.from_node, alt))
        for arc in [arc for arc in arcs if arc.from_node == name]:
            arcs.append(Arc(alt, arc.to_node))

    # Positive-time rework cycles: after an operation finishes and releases its
    # machine resource, a part may repeat that same operation before completion.
    operations = [(j, k) for j in range(1, n_parts + 1) for k in range(1, ops_per_part + 1)]
    selected_reworks = rng.sample(operations, min(rework_locations, len(operations)))
    for j, k in selected_reworks:
        name = f"t_rework_j{j}_op{k}"
        transitions[name] = Transition(
            name=name,
            label=f"rework_j{j}_op{k}",
            is_fault=False,
            is_adjustable=False,
            interval=Interval(Fraction(1), Fraction(2)),
        )
        arcs.append(Arc(f"p{j}_op{k}_end", name))
        arcs.append(Arc(name, f"p{j}_op{k}_start"))

    return TPN(
        places=places,
        transitions=transitions,
        arcs=arcs,
        initial_marking=dict(tpn.initial_marking),
        completion_markings=[dict(marking) for marking in tpn.completion_markings],
    )


def generate_v5_model(
    v5_family: str,
    seed: int,
    *,
    n_parts: int | None = None,
    ops_per_part: int | None = None,
    n_machines: int | None = None,
    wip_bound: int | None = None,
    n_configs: int = 1,
    inspection_locations: int | None = None,
    route_choices: int | None = None,
    rework_locations: int | None = None,
    unobservable_ratio: float | None = None,
    processing_observable_ratio: float | None = None,
    fault_locations: int | None = None,
    interval_width_factor: float = 1.0,
    adjustable_ratio: float = 0.25,
) -> Tuple[TPN, int, float, dict]:
    """Generate one frozen-family model and return its explicit attributes."""
    if v5_family not in FAMILY_SPECS:
        raise ValueError(f"unknown v5 family: {v5_family}")
    spec = dict(FAMILY_SPECS[v5_family])
    parts = n_parts if n_parts is not None else spec["parts"]
    ops = ops_per_part if ops_per_part is not None else spec["ops"]
    machines = n_machines if n_machines is not None else spec["machines"]
    wip = wip_bound if wip_bound is not None else min(spec["wip"], parts)
    inspections = inspection_locations if inspection_locations is not None else spec.get("inspections", 0)
    routes = route_choices if route_choices is not None else spec.get("routes", 0)
    reworks = rework_locations if rework_locations is not None else spec.get("reworks", 0)
    unobs = unobservable_ratio if unobservable_ratio is not None else spec["unobs"]
    proc_obs = processing_observable_ratio if processing_observable_ratio is not None else (1.0 - unobs / 2.0)
    faults = fault_locations if fault_locations is not None else min(max(1, ops // 3), machines)
    if not 1 <= wip <= parts:
        raise ValueError(f"wip_bound must be in [1, n_parts], got {wip}/{parts}")

    tpn, default_L, default_H = generate_rmt_fms(
        n_machines=machines,
        n_parts=parts,
        ops_per_part=ops,
        wip_bound=wip,
        unobservable_ratio=unobs,
        fault_locations=faults,
        interval_width_factor=interval_width_factor,
        adjustable_ratio=adjustable_ratio,
        seed=seed,
        n_configs=n_configs,
        inspection_locations=min(inspections, parts * ops),
    )
    tpn = _with_structural_features(
        tpn,
        seed=seed,
        n_parts=parts,
        ops_per_part=ops,
        route_choices=routes,
        rework_locations=reworks,
        processing_observable_ratio=proc_obs,
    )
    attributes = {
        "benchmark_family": v5_family,
        "synthetic_model": True,
        "number_of_parts": parts,
        "operations_per_part": ops,
        "resource_count": machines,
        "WIP": wip,
        "observable_ratio_target": 1.0 - unobs,
        "processing_observable_ratio": proc_obs,
        "inspection_locations": min(inspections, parts * ops),
        "route_choice_locations": min(routes, parts * max(0, ops - 1)),
        "cycle_presence": reworks > 0,
        "cycle_semantics": "positive-time production rework" if reworks else None,
        "rework_locations": reworks,
        "fault_configuration": {"locations": faults, "selection": "seeded reachable machines"},
        "n_configs": n_configs,
    }
    return tpn, default_L, default_H * float(spec.get("h_mult", 1.0)), attributes
