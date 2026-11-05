"""
Parse YAML model files into TPN objects.

Expected YAML format:
  model_id: example
  semantics:
    server: single
    time: strong
    completion: first_entry
  places: [p1, p2, p3]
  transitions:
    - id: t1
      label: "a"          # empty string or missing = unobservable
      fault: false
      adjustable: false
      interval: [2, 3]    # [earliest, latest], use "inf" for infinite
      admissible_intervals: [[1,3],[2,4]]  # optional, for adjustable
  arcs:
    - [p1, t1]  # place -> transition
    - [t1, p2]  # transition -> place
  initial_marking:
    p1: 1
  completion_markings:
    - {p3: 1}
  bounds:
    L: 10
    H: 100.0

Returns (tpn, L, H).
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .tpn import Arc, INF, Interval, Place, TPN, Transition


def _parse_endpoint(value: Any) -> object:
    """Parse a YAML interval endpoint to Fraction or float('inf')."""
    if isinstance(value, str):
        if value.lower() in ("inf", "infinity", "+inf"):
            return INF
        return Fraction(value)
    if isinstance(value, float) and math.isinf(value):
        return INF
    return Fraction(value)


def _parse_interval(raw: Any) -> Interval:
    """Parse [earliest, latest] list from YAML."""
    if not (isinstance(raw, (list, tuple)) and len(raw) == 2):
        raise ValueError(f"Interval must be [earliest, latest], got {raw!r}")
    earliest = Fraction(_parse_endpoint(raw[0]))
    latest = _parse_endpoint(raw[1])
    if not math.isinf(latest):
        latest = Fraction(latest)
    return Interval(earliest=earliest, latest=latest)


def parse_model(path: str) -> Tuple[TPN, int, float]:
    """Parse a YAML model file.

    Returns:
        (tpn, L, H) where L is int label bound and H is float time horizon.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Places
    place_names: List[str] = data.get("places", [])
    places: Dict[str, Place] = {name: Place(name=name) for name in place_names}

    # Transitions
    transitions: Dict[str, Transition] = {}
    for t_data in data.get("transitions", []):
        t_id: str = str(t_data["id"])
        label: str = str(t_data.get("label", "") or "")
        is_fault: bool = bool(t_data.get("fault", False))
        is_adjustable: bool = bool(t_data.get("adjustable", False))

        raw_interval = t_data.get("interval", [0, INF])
        interval = _parse_interval(raw_interval)

        raw_admissible = t_data.get("admissible_intervals", None)
        if raw_admissible:
            admissible_intervals = tuple(
                _parse_interval(ri) for ri in raw_admissible
            )
        elif is_adjustable:
            # Default: current interval only
            admissible_intervals = (interval,)
        else:
            admissible_intervals = ()

        transitions[t_id] = Transition(
            name=t_id,
            label=label,
            is_fault=is_fault,
            is_adjustable=is_adjustable,
            interval=interval,
            admissible_intervals=admissible_intervals,
        )

    # Arcs
    arcs: List[Arc] = []
    for arc_data in data.get("arcs", []):
        if isinstance(arc_data, (list, tuple)) and len(arc_data) == 2:
            arcs.append(Arc(from_node=str(arc_data[0]), to_node=str(arc_data[1])))
        elif isinstance(arc_data, dict):
            arcs.append(Arc(from_node=str(arc_data["from"]), to_node=str(arc_data["to"])))
        else:
            raise ValueError(f"Cannot parse arc: {arc_data!r}")

    # Initial marking
    raw_initial = data.get("initial_marking", {})
    initial_marking: Dict[str, int] = {
        str(p): int(v) for p, v in raw_initial.items()
    }

    # Completion markings
    completion_markings: List[Dict[str, int]] = []
    for cm in data.get("completion_markings", []):
        completion_markings.append({str(p): int(v) for p, v in cm.items()})

    # Bounds
    bounds = data.get("bounds", {})
    L: int = int(bounds.get("L", 10))
    H_raw = bounds.get("H", 100.0)
    H: float = float(H_raw)

    tpn = TPN(
        places=places,
        transitions=transitions,
        arcs=arcs,
        initial_marking=initial_marking,
        completion_markings=completion_markings,
    )

    return tpn, L, H
