"""Independent exact strong-time feasibility oracle for concrete TPN traces.

This module intentionally does not import or reuse the SCG DBM implementation.
It translates one concrete firing sequence into rational difference constraints
over absolute event times and solves them with an exact Bellman--Ford check.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..model.tpn import TPN


ZERO = "__time_origin__"


@dataclass(frozen=True)
class DifferenceConstraint:
    """Constraint ``left - right <= bound``."""

    left: str
    right: str
    bound: Fraction


@dataclass(frozen=True)
class OracleResult:
    feasible: bool
    reason: Optional[str]
    event_times: Optional[Tuple[Fraction, ...]]
    final_marking: Optional[Dict[str, int]]
    constraints: Tuple[DifferenceConstraint, ...]


def _solve_difference_constraints(
    variables: Iterable[str], constraints: Sequence[DifferenceConstraint]
) -> Optional[Dict[str, Fraction]]:
    """Return one exact feasible potential, or ``None`` on a negative cycle."""
    names = tuple(dict.fromkeys(variables))
    distance = {name: Fraction(0) for name in names}
    # left - right <= c is the edge right -> left with weight c.
    for iteration in range(len(names)):
        changed = False
        for constraint in constraints:
            candidate = distance[constraint.right] + constraint.bound
            if candidate < distance[constraint.left]:
                distance[constraint.left] = candidate
                changed = True
                if iteration == len(names) - 1:
                    return None
        if not changed:
            break
    shift = distance[ZERO]
    return {name: value - shift for name, value in distance.items()}


def check_firing_sequence(tpn: TPN, sequence: Sequence[str]) -> OracleResult:
    """Check a concrete firing sequence under single-server strong-time semantics.

    Persistent transitions retain their enabling epoch. Newly enabled and
    self-reenabling transitions receive the current event as their new epoch.
    First entry into a completion marking terminates an execution.
    """
    marking = dict(tpn.initial_marking)
    origins: Dict[str, str] = {name: ZERO for name in tpn.enabled_set(marking)}
    constraints: List[DifferenceConstraint] = []
    variables: List[str] = [ZERO]
    previous = ZERO

    if tpn.in_completion(marking) and sequence:
        return OracleResult(False, "sequence continues after initial completion", None,
                            None, tuple(constraints))

    for index, fired in enumerate(sequence, start=1):
        event = f"event_{index}"
        variables.append(event)
        enabled = frozenset(tpn.enabled_set(marking))
        if fired not in enabled:
            return OracleResult(False, f"{fired} is not structurally enabled at step {index}",
                                None, None, tuple(constraints))

        # Nondecreasing absolute time: previous - event <= 0.
        constraints.append(DifferenceConstraint(previous, event, Fraction(0)))
        constraints.append(DifferenceConstraint(ZERO, event, Fraction(0)))

        # Strong urgency applies to every transition enabled throughout the delay.
        for name in enabled:
            latest = tpn.transitions[name].interval.latest
            if not math.isinf(latest):
                constraints.append(DifferenceConstraint(
                    event, origins[name], Fraction(latest)
                ))

        earliest = tpn.transitions[fired].interval.earliest
        constraints.append(DifferenceConstraint(
            origins[fired], event, -Fraction(earliest)
        ))

        old_origins = dict(origins)
        persistent = tpn.persistent_enabled(marking, fired)
        newly = tpn.newly_enabled(marking, fired)
        marking = tpn.fire(marking, fired)
        after = frozenset(tpn.enabled_set(marking))
        origins = {}
        for name in after:
            if name in persistent:
                origins[name] = old_origins[name]
            elif name in newly:
                origins[name] = event
            else:  # Defensive: the TPN enabling partition must be exhaustive.
                return OracleResult(False, f"unclassified enabling status for {name}",
                                    None, None, tuple(constraints))

        previous = event
        if tpn.in_completion(marking) and index != len(sequence):
            return OracleResult(False, "sequence continues after first completion entry",
                                None, None, tuple(constraints))

    solution = _solve_difference_constraints(variables, constraints)
    if solution is None:
        return OracleResult(False, "exact difference constraints are infeasible",
                            None, None, tuple(constraints))
    event_times = tuple(solution[f"event_{i}"] for i in range(1, len(sequence) + 1))
    return OracleResult(True, None, event_times, marking, tuple(constraints))


def enumerate_oracle_traces(tpn: TPN, max_depth: int) -> frozenset[Tuple[str, ...]]:
    """Enumerate all structurally possible, exactly time-feasible trace prefixes."""
    accepted = {()}
    frontier = [(dict(tpn.initial_marking), tuple())]
    for _ in range(max_depth):
        next_frontier = []
        for marking, prefix in frontier:
            if tpn.in_completion(marking):
                continue
            for name in sorted(tpn.enabled_set(marking)):
                sequence = prefix + (name,)
                result = check_firing_sequence(tpn, sequence)
                if result.feasible:
                    accepted.add(sequence)
                    next_frontier.append((tpn.fire(marking, name), sequence))
        frontier = next_frontier
    return frozenset(accepted)
