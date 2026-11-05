"""
Single-server strong-time semantics for TPN.

Semantics (supplement S1):
- Time may elapse by d>=0 only if nu(u)+d <= tau_bar_u for every enabled
  transition u with finite latest time.
- An enabled transition t may fire when tau_t <= nu(t)+d <= tau_bar_t.
- Persistently enabled clocks RETAIN their values after firing.
- Newly enabled clocks are RESET to 0.
- Self-reenabling: if t enables itself again after firing, treat as newly enabled.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from ..model.tpn import INF, TPN


@dataclass
class TimedState:
    """A timed state (M, nu) in the TPN.

    marking: dict[place_name -> token_count]
    clock: dict[transition_name -> age since enabled] (only enabled transitions)
    """

    marking: Dict[str, int]
    clock: Dict[str, Fraction]  # transition_name -> age (time since enabled)

    def __post_init__(self):
        # Ensure all clocks are Fraction
        self.clock = {k: Fraction(v) for k, v in self.clock.items()}


def enabled_transitions(tpn: TPN, state: TimedState) -> List[str]:
    """Return list of enabled transition names at state.marking."""
    return tpn.enabled_set(state.marking)


def max_delay(tpn: TPN, state: TimedState) -> object:
    """Maximum time that can elapse from this state.

    = min over enabled transitions with finite latest of (latest - clock[t]).
    Returns float('inf') if no enabled transition has finite latest time.
    """
    result = INF
    enabled = enabled_transitions(tpn, state)
    for t_name in enabled:
        t = tpn.transitions[t_name]
        if not t.interval.is_infinite():
            age = state.clock.get(t_name, Fraction(0))
            remaining = Fraction(t.interval.latest) - age
            if isinstance(result, float) and math.isinf(result):
                result = remaining
            else:
                result = min(result, remaining)
    return result


def fire_transition(
    tpn: TPN, state: TimedState, t_name: str, delay: Fraction
) -> TimedState:
    """Fire transition t_name after waiting delay d.

    Steps:
    1. Advance clocks of all enabled transitions by delay.
    2. Verify t_name is fireable: earliest <= clock[t]+delay is already
       encoded as clock[t] after step 1 being >= earliest(t) and <= latest(t).
    3. Verify no enabled transition exceeds its latest time.
    4. Compute new marking.
    5. Persistently enabled transitions retain (already-advanced) clock values.
    6. Newly enabled transitions get clock = 0.

    Raises ValueError if semantics are violated.
    """
    delay = Fraction(delay)
    if delay < 0:
        raise ValueError(f"Delay must be >= 0, got {delay}")

    enabled = tpn.enabled_set(state.marking)

    # Step 1: advance clocks
    new_clock: Dict[str, Fraction] = {}
    for u in enabled:
        age = state.clock.get(u, Fraction(0))
        new_clock[u] = age + delay

    # Step 2: check urgency - no enabled transition should exceed its latest
    for u in enabled:
        t_obj = tpn.transitions[u]
        if not t_obj.interval.is_infinite():
            if new_clock[u] > Fraction(t_obj.interval.latest):
                raise ValueError(
                    f"Urgency violated: transition '{u}' clock {new_clock[u]} "
                    f"> latest {t_obj.interval.latest} after delay {delay}"
                )

    # Step 3: check t_name is fireable
    if t_name not in enabled:
        raise ValueError(f"Transition '{t_name}' is not enabled at current marking")

    t_obj = tpn.transitions[t_name]
    t_age = new_clock[t_name]
    if t_age < Fraction(t_obj.interval.earliest):
        raise ValueError(
            f"Transition '{t_name}' fired too early: age {t_age} < earliest {t_obj.interval.earliest}"
        )
    if not t_obj.interval.is_infinite() and t_age > Fraction(t_obj.interval.latest):
        raise ValueError(
            f"Transition '{t_name}' fired too late: age {t_age} > latest {t_obj.interval.latest}"
        )

    # Step 4: compute new marking
    new_marking = tpn.fire(state.marking, t_name)

    # Step 5 & 6: compute new clocks
    # Persistent: enabled before AND after, excluding t itself if re-enabled
    before_set = frozenset(enabled)
    after_set = frozenset(tpn.enabled_set(new_marking))

    persistent = tpn.persistent_enabled(state.marking, t_name)
    newly = tpn.newly_enabled(state.marking, t_name)

    final_clock: Dict[str, Fraction] = {}
    for u in after_set:
        if u in persistent:
            # Retain advanced clock
            final_clock[u] = new_clock[u]
        else:
            # Newly enabled (or self-reenabling t) -> reset to 0
            final_clock[u] = Fraction(0)

    return TimedState(marking=new_marking, clock=final_clock)


def classify_fault(tpn: TPN, firing_sequence: List[str]) -> str:
    """Returns 'F' if any transition in sequence is a fault transition, 'N' otherwise."""
    for t_name in firing_sequence:
        if t_name in tpn.transitions and tpn.transitions[t_name].is_fault:
            return "F"
    return "N"


def initial_timed_state(tpn: TPN) -> TimedState:
    """Create the initial timed state (M0, nu0) with all clocks at 0."""
    marking = dict(tpn.initial_marking)
    enabled = tpn.enabled_set(marking)
    clock = {t: Fraction(0) for t in enabled}
    return TimedState(marking=marking, clock=clock)
