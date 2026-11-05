"""
Full model validation per supplement applicability conditions.
"""
from __future__ import annotations

import math
from typing import List

from .tpn import TPN


def validate_model(tpn: TPN) -> List[str]:
    """Validate the TPN model.

    Returns a list of error strings. Empty list means model is valid.
    Raises ValueError on the first critical structural violation.
    """
    errors: List[str] = []

    # 1. Fault transitions must be unobservable
    for t_name, t in tpn.transitions.items():
        if t.is_fault and t.is_observable:
            errors.append(
                f"Fault transition '{t_name}' has label '{t.label}' but must be unobservable."
            )

    # 2. Adjustable transitions must not be fault transitions
    for t_name, t in tpn.transitions.items():
        if t.is_adjustable and t.is_fault:
            errors.append(
                f"Adjustable transition '{t_name}' must not be a fault transition."
            )

    # 3. Adjustable transitions: admissible_intervals must be nonempty
    #    and must contain the current interval
    for t_name, t in tpn.transitions.items():
        if t.is_adjustable:
            if not t.admissible_intervals:
                errors.append(
                    f"Adjustable transition '{t_name}' has empty admissible_intervals."
                )
            else:
                if t.interval not in t.admissible_intervals:
                    errors.append(
                        f"Adjustable transition '{t_name}': current interval "
                        f"{t.interval} not in admissible_intervals."
                    )

    # 4. earliest <= latest for all intervals
    for t_name, t in tpn.transitions.items():
        if not math.isinf(t.interval.latest) and t.interval.latest < t.interval.earliest:
            errors.append(
                f"Transition '{t_name}': latest ({t.interval.latest}) < "
                f"earliest ({t.interval.earliest})."
            )
        if t.interval.earliest < 0:
            errors.append(
                f"Transition '{t_name}': earliest ({t.interval.earliest}) < 0."
            )

    # 5. initial_marking references valid places
    for p_name, count in tpn.initial_marking.items():
        if p_name not in tpn.places:
            errors.append(
                f"Initial marking references unknown place '{p_name}'."
            )
        if count < 0:
            errors.append(
                f"Initial marking for '{p_name}' has negative count {count}."
            )

    # 6. completion_markings reference valid places
    for i, cm in enumerate(tpn.completion_markings):
        for p_name, count in cm.items():
            if p_name not in tpn.places:
                errors.append(
                    f"Completion marking [{i}] references unknown place '{p_name}'."
                )
            if count < 0:
                errors.append(
                    f"Completion marking [{i}] for '{p_name}' has negative count {count}."
                )

    # 7. Arcs reference valid places and transitions
    for arc in tpn.arcs:
        from_in_places = arc.from_node in tpn.places
        from_in_transitions = arc.from_node in tpn.transitions
        to_in_places = arc.to_node in tpn.places
        to_in_transitions = arc.to_node in tpn.transitions

        if not (from_in_places or from_in_transitions):
            errors.append(
                f"Arc from_node '{arc.from_node}' not found in places or transitions."
            )
        if not (to_in_places or to_in_transitions):
            errors.append(
                f"Arc to_node '{arc.to_node}' not found in places or transitions."
            )

        # Arc direction: place->transition or transition->place
        if from_in_places and to_in_places:
            errors.append(
                f"Arc from place '{arc.from_node}' to place '{arc.to_node}' is invalid."
            )
        if from_in_transitions and to_in_transitions:
            errors.append(
                f"Arc from transition '{arc.from_node}' to transition '{arc.to_node}' is invalid."
            )

    # 8. No isolated places (places not connected to any arc)
    all_arc_nodes: set = set()
    for arc in tpn.arcs:
        all_arc_nodes.add(arc.from_node)
        all_arc_nodes.add(arc.to_node)
    for p_name in tpn.places:
        if p_name not in all_arc_nodes:
            errors.append(f"Place '{p_name}' is isolated (not connected to any arc).")

    # 9. completion_markings must be nonempty (applicability condition 2)
    if not tpn.completion_markings:
        errors.append("completion_markings is empty; at least one completion marking required.")

    return errors
