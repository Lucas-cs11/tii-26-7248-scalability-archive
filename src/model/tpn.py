"""
TPN model dataclasses. Strictly follows supplement Module S1.

Single-server strong-time semantics:
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
from typing import Dict, FrozenSet, List, Optional, Tuple


# Sentinel for infinite upper bounds; always check with math.isinf()
INF = float("inf")


@dataclass(frozen=True)
class Interval:
    """Static firing interval I(t) = [earliest, latest].
    latest is Fraction for finite bounds or float('inf') for infinite."""

    earliest: Fraction
    latest: object  # Fraction or float('inf')

    def __post_init__(self):
        if self.earliest < 0:
            raise ValueError(f"earliest must be >= 0, got {self.earliest}")
        if not math.isinf(self.latest) and self.latest < self.earliest:
            raise ValueError(
                f"latest ({self.latest}) must be >= earliest ({self.earliest}) or inf"
            )

    def is_infinite(self) -> bool:
        return math.isinf(self.latest)

    def __repr__(self) -> str:
        latest_str = "inf" if self.is_infinite() else str(self.latest)
        return f"[{self.earliest}, {latest_str}]"


@dataclass(frozen=True)
class AuthorizedRange:
    """Continuous authorized range for LP-based timing repair.

    For an adjustable transition t, its interval [l,u] may be varied
    continuously within:

        l in [l_min, l_max]
        u in [u_min, u_max]

    resolution: controller time-grid resolution (e.g. 0.5).
    """

    l_min: Fraction
    l_max: Fraction
    u_min: Fraction
    u_max: Fraction
    resolution: Fraction = Fraction(1, 2)

    def clamp_interval(self, iv: Interval) -> Interval:
        """Clamp an interval's endpoints into the authorized range."""
        l = max(self.l_min, min(self.l_max, iv.earliest))
        u = max(self.u_min, min(self.u_max, iv.latest))
        if u < l:
            u = l
        return Interval(l, u)

    def contains(self, iv: Interval) -> bool:
        """True if the interval endpoints lie within the authorized range."""
        return (
            self.l_min <= iv.earliest <= self.l_max
            and self.u_min <= iv.latest <= self.u_max
        )


@dataclass(frozen=True)
class Transition:
    """A TPN transition.

    Attributes:
        name: unique identifier string
        label: observable label string, or "" for unobservable (epsilon)
        is_fault: True if this is a fault transition (must be unobservable)
        is_observable: True iff label != ""
        is_adjustable: True if interval can be changed during repair
        interval: static firing interval I(t)
        admissible_intervals: for adjustable transitions, the finite set of
                              authorized intervals (must include current interval)
        authorized_range: continuous authorized range for LP-based repair
                          (None if not authorized for continuous adjustment)
    """

    name: str
    label: str  # "" means unobservable (epsilon)
    is_fault: bool
    is_adjustable: bool
    interval: Interval
    admissible_intervals: Tuple[Interval, ...] = field(default_factory=tuple)
    authorized_range: Optional[AuthorizedRange] = None

    def __post_init__(self):
        # is_observable is derived, not stored as a separate field (frozen dataclass)
        pass

    @property
    def is_observable(self) -> bool:
        return self.label != ""

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        if isinstance(other, Transition):
            return self.name == other.name
        return NotImplemented


@dataclass(frozen=True)
class Place:
    """A TPN place."""

    name: str

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        if isinstance(other, Place):
            return self.name == other.name
        return NotImplemented


@dataclass(frozen=True)
class Arc:
    """Directed arc: place->transition or transition->place."""

    from_node: str  # name of place or transition
    to_node: str  # name of place or transition


class TPN:
    """Time Petri Net.

    N = (P, T, Pre, Post, M0, Sigma, l, I)

    places: dict[name -> Place]
    transitions: dict[name -> Transition]
    arcs: list[Arc]
    initial_marking: dict[place_name -> token_count]
    completion_markings: list[dict[place_name -> token_count]]
    observable_alphabet: frozenset[str]
    """

    def __init__(
        self,
        places: Dict[str, Place],
        transitions: Dict[str, Transition],
        arcs: List[Arc],
        initial_marking: Dict[str, int],
        completion_markings: List[Dict[str, int]],
    ):
        self.places = places
        self.transitions = transitions
        self.arcs = arcs
        self.initial_marking = initial_marking
        self.completion_markings = completion_markings

        self.observable_alphabet: FrozenSet[str] = frozenset(
            t.label for t in transitions.values() if t.is_observable
        )

        # Build preset/postset caches
        self._preset: Dict[str, FrozenSet[str]] = {}
        self._postset: Dict[str, FrozenSet[str]] = {}
        for t_name in transitions:
            pre = frozenset(
                arc.from_node
                for arc in arcs
                if arc.to_node == t_name and arc.from_node in places
            )
            post = frozenset(
                arc.to_node
                for arc in arcs
                if arc.from_node == t_name and arc.to_node in places
            )
            self._preset[t_name] = pre
            self._postset[t_name] = post

    def preset(self, t_name: str) -> FrozenSet[str]:
        """Places with an arc to transition t_name."""
        return self._preset[t_name]

    def postset(self, t_name: str) -> FrozenSet[str]:
        """Places with an arc from transition t_name."""
        return self._postset[t_name]

    def is_enabled(self, marking: Dict[str, int], t_name: str) -> bool:
        """True iff all preset places of t have >= 1 token (single-server: weight=1)."""
        for p in self._preset[t_name]:
            if marking.get(p, 0) < 1:
                return False
        return True

    def fire(self, marking: Dict[str, int], t_name: str) -> Dict[str, int]:
        """Return new marking after firing t (does NOT check enabledness)."""
        new_marking = dict(marking)
        for p in self._preset[t_name]:
            new_marking[p] = new_marking.get(p, 0) - 1
            if new_marking[p] == 0:
                del new_marking[p]
        for p in self._postset[t_name]:
            new_marking[p] = new_marking.get(p, 0) + 1
        return new_marking

    def enabled_set(self, marking: Dict[str, int]) -> List[str]:
        """Return list of enabled transition names at marking."""
        return [
            t_name
            for t_name in self.transitions
            if self.is_enabled(marking, t_name)
        ]

    def persistent_enabled(
        self, marking: Dict[str, int], t_name: str
    ) -> FrozenSet[str]:
        """E^p(M, t): transitions enabled both before and after firing t,
        excluding t itself if it re-enables itself (self-reenabling -> newly enabled)."""
        new_marking = self.fire(marking, t_name)
        before = frozenset(self.enabled_set(marking))
        after = frozenset(self.enabled_set(new_marking))
        # Transitions that were enabled before and remain enabled after,
        # BUT t itself is newly enabled if it appears in after
        # (self-reenabling: treat as newly enabled per supplement).
        persistent = (before & after) - {t_name}
        return persistent

    def newly_enabled(
        self, marking: Dict[str, int], t_name: str
    ) -> FrozenSet[str]:
        """E^n(M, t): transitions enabled after firing t that were NOT enabled before,
        plus t itself if it re-enables itself."""
        new_marking = self.fire(marking, t_name)
        before = frozenset(self.enabled_set(marking))
        after = frozenset(self.enabled_set(new_marking))
        # Newly enabled = in after but not in before (before firing)
        # Self-reenabling: t was in before and is in after -> treat as newly enabled
        newly = (after - before)
        if t_name in after:
            # t re-enabled itself -> treat as newly enabled
            newly = newly | {t_name}
        return newly

    def in_completion(self, marking: Dict[str, int]) -> bool:
        """True if marking matches any completion marking."""
        for m_fin in self.completion_markings:
            if self._marking_matches(marking, m_fin):
                return True
        return False

    def _marking_matches(
        self, marking: Dict[str, int], target: Dict[str, int]
    ) -> bool:
        """True if marking satisfies target (at least target tokens at each place,
        and zero at places not in target). Treat as exact equality for named places."""
        # Exact equality: every place in target has exactly that many tokens,
        # and every place not in target has zero tokens.
        all_places = set(marking.keys()) | set(target.keys())
        for p in all_places:
            if marking.get(p, 0) != target.get(p, 0):
                return False
        return True

    def validate(self):
        """Raise ValueError if model constraints are violated."""
        for t_name, t in self.transitions.items():
            if t.is_fault and t.is_observable:
                raise ValueError(
                    f"Fault transition '{t_name}' must be unobservable"
                )
            if t.is_adjustable and t.is_fault:
                raise ValueError(
                    f"Adjustable transition '{t_name}' must not be a fault transition"
                )

    def copy_with_interval(self, t_name: str, new_interval: Interval) -> "TPN":
        """Return a new TPN with one transition's interval replaced."""
        new_transitions = {}
        for name, t in self.transitions.items():
            if name == t_name:
                new_t = Transition(
                    name=t.name,
                    label=t.label,
                    is_fault=t.is_fault,
                    is_adjustable=t.is_adjustable,
                    interval=new_interval,
                    admissible_intervals=t.admissible_intervals,
                    authorized_range=t.authorized_range,
                )
                new_transitions[name] = new_t
            else:
                new_transitions[name] = t
        return TPN(
            places=self.places,
            transitions=new_transitions,
            arcs=self.arcs,
            initial_marking=self.initial_marking,
            completion_markings=self.completion_markings,
        )
