"""
Boundary benchmark generator.
Tests positive-duration and zero-time cycle behavior.

Models:
  positive_cycle_count=0: simple linear TPN with normal+fault branches -> VER or AMB
  positive_cycle_count=1,2,4: add unobservable loops with [1,2] duration on normal path
  include_zero_time_cycle=True: add a zero-time loop -> INC expected
"""
from __future__ import annotations

from fractions import Fraction
from typing import Dict, List, Tuple

from ..model.tpn import TPN, Place, Transition, Arc, Interval, INF


def generate_boundary(
    positive_cycle_count: int,
    include_zero_time_cycle: bool,
    n_machines: int = 1,
) -> Tuple[TPN, int, float]:
    """Generate a boundary test TPN model.

    Returns (tpn, L, H).

    Structure:
      - Normal path: p0 -> [t_norm unobs [1,2]] -> p_norm_mid -> (cycles) -> p_pre
      - Fault path:  p0 -> [t_fault fault [1,2]] -> p_fault_mid -> p_pre
      - Shared:      p_pre -> [t_obs obs "done" [0,0]] -> p_done
      - positive_cycle_count loops on normal path (each loop [1,2])
      - optional zero-time loop on normal path (creates INC)
    """
    places: Dict[str, Place] = {}
    transitions: Dict[str, Transition] = {}
    arcs: List[Arc] = []

    def add_place(name: str) -> str:
        places[name] = Place(name=name)
        return name

    def add_transition(
        name: str,
        label: str,
        is_fault: bool,
        is_adjustable: bool,
        interval: Interval,
        admissible_intervals: Tuple[Interval, ...] = (),
    ) -> str:
        transitions[name] = Transition(
            name=name,
            label=label,
            is_fault=is_fault,
            is_adjustable=is_adjustable,
            interval=interval,
            admissible_intervals=admissible_intervals,
        )
        return name

    def add_arc(from_node: str, to_node: str) -> None:
        arcs.append(Arc(from_node=from_node, to_node=to_node))

    # Core places
    add_place("p0")
    add_place("p_norm_mid")
    add_place("p_fault_mid")
    add_place("p_pre")
    add_place("p_done")

    # Normal branch (unobs, not fault)
    add_transition(
        "t_norm",
        label="",
        is_fault=False,
        is_adjustable=False,
        interval=Interval(Fraction(1), Fraction(2)),
    )
    add_arc("p0", "t_norm")
    add_arc("t_norm", "p_norm_mid")

    # Fault branch
    add_transition(
        "t_fault",
        label="",
        is_fault=True,
        is_adjustable=False,
        interval=Interval(Fraction(1), Fraction(2)),
    )
    add_arc("p0", "t_fault")
    add_arc("t_fault", "p_fault_mid")

    # Fault mid to pre (unobs, not fault)
    add_transition(
        "t_fault_body",
        label="",
        is_fault=False,
        is_adjustable=False,
        interval=Interval(Fraction(0), Fraction(0)),
    )
    add_arc("p_fault_mid", "t_fault_body")
    add_arc("t_fault_body", "p_pre")

    # Normal path: chain through cycle places
    prev_place = "p_norm_mid"

    for i in range(positive_cycle_count):
        loop_place = f"p_loop_{i}"
        add_place(loop_place)

        t_loop_enter = f"t_loop_enter_{i}"
        add_transition(
            t_loop_enter,
            label="",
            is_fault=False,
            is_adjustable=False,
            interval=Interval(Fraction(1), Fraction(2)),
        )
        add_arc(prev_place, t_loop_enter)
        add_arc(t_loop_enter, loop_place)
        prev_place = loop_place

    if include_zero_time_cycle:
        # Zero-time self-loop place: creates INC
        zt_place = "p_zero_loop"
        add_place(zt_place)

        t_zt_enter = "t_zero_loop_enter"
        add_transition(
            t_zt_enter,
            label="",
            is_fault=False,
            is_adjustable=False,
            interval=Interval(Fraction(0), Fraction(0)),
        )
        add_arc(prev_place, t_zt_enter)
        add_arc(t_zt_enter, zt_place)

        # Self-loop at zero time
        t_zt_loop = "t_zero_loop_back"
        add_transition(
            t_zt_loop,
            label="",
            is_fault=False,
            is_adjustable=False,
            interval=Interval(Fraction(0), Fraction(0)),
        )
        add_arc(zt_place, t_zt_loop)
        add_arc(t_zt_loop, zt_place)

        # Exit zero-time loop
        t_zt_exit = "t_zero_loop_exit"
        add_transition(
            t_zt_exit,
            label="",
            is_fault=False,
            is_adjustable=False,
            interval=Interval(Fraction(0), Fraction(0)),
        )
        add_arc(zt_place, t_zt_exit)
        add_arc(t_zt_exit, "p_pre")
    else:
        # Connect last normal place to p_pre
        t_to_pre = "t_norm_to_pre"
        add_transition(
            t_to_pre,
            label="",
            is_fault=False,
            is_adjustable=False,
            interval=Interval(Fraction(0), Fraction(0)),
        )
        add_arc(prev_place, t_to_pre)
        add_arc(t_to_pre, "p_pre")

    # Observable exit
    add_transition(
        "t_obs",
        label="done",
        is_fault=False,
        is_adjustable=False,
        interval=Interval(Fraction(0), Fraction(0)),
    )
    add_arc("p_pre", "t_obs")
    add_arc("t_obs", "p_done")

    # Initial and completion markings
    initial_marking = {"p0": 1}
    completion_markings = [{"p_done": 1}]

    # L: observable word length (just 1 "done" label)
    L = 2
    # H: generous horizon
    H = 20.0 + positive_cycle_count * 10.0

    tpn = TPN(
        places=places,
        transitions=transitions,
        arcs=arcs,
        initial_marking=initial_marking,
        completion_markings=completion_markings,
    )

    return tpn, L, H
