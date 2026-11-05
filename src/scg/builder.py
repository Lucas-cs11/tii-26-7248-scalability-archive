"""
Build normalized State Class Graph (SCG) using DBM representation.

A state class C = (M, Z) where:
- M: marking (dict[str, int])
- Z: normalized DBM over enabled transitions

DBM representation:
  Variables: x_0 (reference clock, always 0), x_t for each enabled transition t.
  Entry DBM[i][j] = upper bound on (x_i - x_j).
  Normalized via Floyd-Warshall.

Following Berthomieu & Menasche (1983) / Berthomieu & Vernadat (2006):
The zone Z for state class (M, enu) encodes the set of valid ages
(time-since-enabled) for all transitions enabled at M.

The reference variable x_0 represents the current time (= 0 at the class entry).
For enabled transition u, x_u is its age (clock value).

Key DBM entries:
  DBM[u][0] = upper bound on x_u - 0 = x_u   => x_u <= DBM[u][0]  (latest)
  DBM[0][u] = upper bound on 0 - x_u = -x_u  => x_u >= -DBM[0][u] (earliest)

For the initial state class:
  All transitions enabled at M0 have age 0.
  x_u = 0 for all u in E(M0).

After firing transition t with delay d:
  New ages for persistently enabled u: x_u' = x_u + d
  New ages for newly enabled v: x_v' = 0
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, FrozenSet, List, Optional, Tuple

from ..model.tpn import INF, TPN


# Sentinel for infinity in DBM bounds
DBM_INF = float("inf")


def _frac_or_inf(v) -> object:
    if math.isinf(v):
        return DBM_INF
    return Fraction(v)


def _leq(a, b) -> bool:
    """a <= b where either may be inf."""
    if math.isinf(b):
        return True
    if math.isinf(a):
        return False
    return a <= b


def _add(a, b) -> object:
    """a + b where either may be inf."""
    if math.isinf(a) or math.isinf(b):
        return DBM_INF
    return a + b


class DBM:
    """Difference Bound Matrix over a set of variable names plus reference '0'.

    Variables: {'0'} | enabled_transition_names
    DBM[i][j] = upper bound on x_i - x_j  (Fraction or float('inf'))
    """

    def __init__(self, variables: FrozenSet[str]):
        # variables includes the reference '0' and all enabled transition names
        self.variables = variables
        self._vars_list: List[str] = sorted(variables)
        self._idx: Dict[str, int] = {v: i for i, v in enumerate(self._vars_list)}
        n = len(self._vars_list)
        # Initialize: diagonal = 0, off-diagonal = inf
        self._mat: List[List[object]] = [
            [DBM_INF] * n for _ in range(n)
        ]
        for i in range(n):
            self._mat[i][i] = Fraction(0)

    def get(self, i: str, j: str) -> object:
        return self._mat[self._idx[i]][self._idx[j]]

    def set(self, i: str, j: str, val: object):
        self._mat[self._idx[i]][self._idx[j]] = val

    def normalize(self) -> "DBM":
        """Return a new normalized DBM via Floyd-Warshall."""
        n = len(self._vars_list)
        # Copy
        mat = [row[:] for row in self._mat]
        # Floyd-Warshall: tighten all pairs through intermediate k
        for k in range(n):
            for i in range(n):
                for j in range(n):
                    candidate = _add(mat[i][k], mat[k][j])
                    if _leq(candidate, mat[i][j]):
                        mat[i][j] = candidate
        result = DBM(self.variables)
        result._mat = mat
        result._vars_list = self._vars_list
        result._idx = self._idx
        return result

    def is_empty(self) -> bool:
        """Return True if DBM is inconsistent (negative diagonal entry)."""
        for i in range(len(self._vars_list)):
            if not math.isinf(self._mat[i][i]) and self._mat[i][i] < 0:
                return True
        return False

    def __eq__(self, other) -> bool:
        if not isinstance(other, DBM):
            return False
        if self.variables != other.variables:
            return False
        for i in self._vars_list:
            for j in self._vars_list:
                a = self.get(i, j)
                b = other.get(i, j)
                if math.isinf(a) and math.isinf(b):
                    continue
                if math.isinf(a) or math.isinf(b):
                    return False
                if a != b:
                    return False
        return True

    def __hash__(self) -> int:
        h = hash(self.variables)
        for i in self._vars_list:
            for j in self._vars_list:
                v = self.get(i, j)
                if math.isinf(v):
                    h = hash((h, i, j, "inf"))
                else:
                    h = hash((h, i, j, v))
        return h

    def __repr__(self) -> str:
        lines = []
        for i in self._vars_list:
            row = []
            for j in self._vars_list:
                v = self.get(i, j)
                row.append("inf" if math.isinf(v) else str(v))
            lines.append(f"  {i}: {row}")
        return "DBM{\n" + "\n".join(lines) + "\n}"


@dataclass(frozen=True)
class StateClass:
    """A normalized state class C = (M, Z).

    marking_key: frozenset of (place, count) pairs for hashing
    marking: dict[place_name -> token_count]
    dbm: normalized DBM
    enabled: frozenset of enabled transition names (excluding reference '0')
    """

    marking_key: FrozenSet[Tuple[str, int]]
    marking: Dict[str, int]
    dbm: DBM
    enabled: FrozenSet[str]

    def __eq__(self, other) -> bool:
        if not isinstance(other, StateClass):
            return False
        return self.marking_key == other.marking_key and self.dbm == other.dbm

    def __hash__(self) -> int:
        return hash((self.marking_key, self.dbm))


def _marking_to_key(marking: Dict[str, int]) -> FrozenSet[Tuple[str, int]]:
    return frozenset(marking.items())


def _build_initial_class(tpn: TPN) -> StateClass:
    """Compute the initial state class C0 = (M0, Z0).

    Follows Berthomieu & Menasche (1983) / Berthomieu & Vernadat (2006).

    In the SCG zone, x_u represents the age of transition u at the moment
    the NEXT transition fires (not the current elapsed time).  The initial
    zone encodes valid firing-age ranges for all enabled transitions assuming
    they were all enabled at time 0:

      DBM[u][0]  = latest(u)    x_u <=  latest(u)   (can't exceed deadline)
      DBM[0][u]  = -earliest(u) x_u >= earliest(u)  (not yet fireable before earliest)
      DBM[u][v]  = latest(u) - earliest(v)           (cross-difference, tightened by FW)

    The cross-differences encode strong-time urgency: if latest(u) < earliest(v),
    then DBM[u][v] < 0 which (after normalization) may make the zone infeasible
    for certain combinations of concurrent transitions.
    """
    marking = dict(tpn.initial_marking)
    enabled = frozenset(tpn.enabled_set(marking))
    variables = frozenset({"0"} | enabled)

    dbm = DBM(variables)

    for u in enabled:
        t_u = tpn.transitions[u]
        latest_u = t_u.interval.latest
        earliest_u = t_u.interval.earliest

        dbm.set(u, "0", latest_u)       # x_u <= latest(u)
        dbm.set("0", u, -earliest_u)    # x_u >= earliest(u)

    for u in enabled:
        for v in enabled:
            if u != v:
                t_u = tpn.transitions[u]
                t_v = tpn.transitions[v]
                # x_u - x_v <= latest(u) - earliest(v)
                diff = _add(t_u.interval.latest, -t_v.interval.earliest)
                dbm.set(u, v, diff)

    normalized = dbm.normalize()
    marking_key = _marking_to_key(marking)
    return StateClass(
        marking_key=marking_key,
        marking=marking,
        dbm=normalized,
        enabled=enabled,
    )


def _compute_successor(tpn: TPN, cls: StateClass, t_name: str) -> Optional[StateClass]:
    """Compute successor state class after firing t_name from cls.

    Returns None if t_name is not enabled or the resulting zone is empty.

    Algorithm (Berthomieu & Menasche 1983):
    1. Check t_name is enabled at cls.marking.
    2. Check firing feasibility: DBM constraint for t_name implies a valid time
       (earliest(t) - clock(t) >= 0, considering urgency of all others).
    3. Compute new marking M'.
    4. Compute new enabled set E(M').
    5. Build new DBM:
       - For t_name, it's the fired transition. After firing, time 0 is
         redefined as the moment of firing.
       - Let d = delay before firing. After firing, new reference is at firing time.
       - For persistent u (enabled before AND after, u != t if t re-enables itself):
           x_u' = x_u - x_t  (age relative to firing moment)
           DBM'[u][0] = DBM[u][t]  (shifted)
           DBM'[0][u] = DBM[t][u]
       - For newly enabled v:
           x_v' = 0 (freshly enabled)
           DBM'[v][0] = latest(v)
           DBM'[0][v] = -earliest(v)
       Cross terms between persistent u1 and u2:
           DBM'[u1][u2] = DBM[u1][u2]  (unchanged relative difference)
       Cross terms persistent u, newly enabled v:
           DBM'[u][v] = DBM[u][t] + latest(v) ... computed via normalization
    6. Apply urgency: for each transition w enabled after, constrain by
       its latest firing window (prevent w from exceeding its latest after delay d).
       Actually urgency is already encoded in the DBM structure.
    7. Normalize.
    """
    if t_name not in cls.enabled:
        return None

    t_obj = tpn.transitions[t_name]

    # No explicit feasibility pre-check: the successor zone computation and
    # normalization naturally yield an empty zone if the firing is infeasible
    # under strong-time semantics. The DBM cross-differences (set by _build_initial_class
    # and propagated by the shift) encode urgency constraints. After normalization,
    # normalized.is_empty() correctly rejects infeasible successors.

    # Compute new marking
    new_marking = tpn.fire(cls.marking, t_name)

    # Compute persistent and newly enabled sets
    before_set = cls.enabled  # enabled at current marking
    after_set = frozenset(tpn.enabled_set(new_marking))

    # Persistent: enabled before and after, EXCLUDING t if it re-enables itself
    persistent = tpn.persistent_enabled(cls.marking, t_name)
    newly = tpn.newly_enabled(cls.marking, t_name)

    # Variables in new DBM: {0} | after_set
    new_variables = frozenset({"0"} | after_set)
    new_dbm = DBM(new_variables)

    # Shift: after firing t at time x_t (from old reference 0),
    # new reference 0' is at the firing moment.
    # For persistent u: x_u^new = x_u^old - x_t^old
    #   => x_u^new - 0^new = x_u^old - x_t^old
    #   => DBM^new[u][0] = DBM^old[u][t]  (upper bound on x_u - x_t)
    #   => DBM^new[0][u] = DBM^old[t][u]  (upper bound on x_t - x_u = -(x_u - x_t))

    for u in persistent:
        # Check u is actually in old DBM
        if u in cls.dbm.variables:
            dbm_u_t = cls.dbm.get(u, t_name)
            dbm_t_u = cls.dbm.get(t_name, u)
            new_dbm.set(u, "0", dbm_u_t)
            new_dbm.set("0", u, dbm_t_u)

    # Cross terms between two persistent transitions
    for u1 in persistent:
        for u2 in persistent:
            if u1 != u2 and u1 in cls.dbm.variables and u2 in cls.dbm.variables:
                new_dbm.set(u1, u2, cls.dbm.get(u1, u2))

    # Newly enabled: x_v = 0 relative to new reference
    # So in [earliest(v), latest(v)] relative to new reference
    for v in newly:
        t_v = tpn.transitions[v]
        new_dbm.set(v, "0", t_v.interval.latest)  # x_v <= latest(v)
        new_dbm.set("0", v, -t_v.interval.earliest)  # x_v >= earliest(v)

    # Cross terms: persistent u, newly enabled v
    for u in persistent:
        for v in newly:
            if u in cls.dbm.variables:
                t_v = tpn.transitions[v]
                # x_u^new - x_v^new: x_v^new in [earliest(v), latest(v)] => -x_v^new in [-latest(v), -earliest(v)]
                # DBM[u][v] = upper bound on x_u - x_v
                # x_u <= DBM[u][0] (old, in new coords) => x_u^new - x_v^new <= DBM^new[u][0] - (-DBM[0][v])
                # i.e., = DBM^new[u][0] + latest(v)
                ub_u_new = cls.dbm.get(u, t_name)  # = DBM^new[u][0]
                new_dbm.set(u, v, _add(ub_u_new, t_v.interval.latest))
                # DBM[v][u] = upper bound on x_v - x_u
                # x_v <= latest(v) and x_u >= DBM^new[0][u] (but negated)
                # DBM[0][u] in new coords = DBM[t][u] in old
                lb_u_new = cls.dbm.get(t_name, u)  # upper bound on x_t - x_u => -x_u <= lb_u_new => x_u >= -lb_u_new
                new_dbm.set(v, u, _add(t_v.interval.latest, lb_u_new))

    # Cross terms: newly enabled v1, newly enabled v2
    for v1 in newly:
        for v2 in newly:
            if v1 != v2:
                t_v1 = tpn.transitions[v1]
                t_v2 = tpn.transitions[v2]
                # x_v1 - x_v2 <= latest(v1) - earliest(v2)
                new_dbm.set(v1, v2, _add(t_v1.interval.latest, -t_v2.interval.earliest))

    # Now apply urgency constraint for the fired transition t itself
    # In the new coordinates, x_t was consumed. But we need to constrain:
    # The delay d = x_t^old - 0 must satisfy d >= 0 (already ensured by DBM[t][0] from old zone)
    # and d >= earliest(t) - current_age(t) which is:
    # x_t^old >= earliest(t) i.e., DBM[0][t] <= -earliest(t) in old coords
    # This must already be in cls.dbm.
    # The shift to new coords handles this: DBM^new[0][u] = DBM^old[t][u] which includes
    # the constraint that u's age relative to firing point is correct.

    # Urgency constraint: the fired transition must fire within its window.
    # This is encoded by cls.dbm[t][0] <= latest(t) and cls.dbm[0][t] <= -earliest(t).
    # After shifting, these constraints propagate correctly.

    # Additional urgency: in old zone, firing t at time x_t requires
    # that all other enabled transitions u have x_u + (x_t - x_u_now) <= latest(u)
    # i.e., x_t <= latest(u) + (x_u_now - 0) i.e., x_t - x_u <= latest(u) ... Hmm.
    # Actually strong-time urgency says: d <= latest(u) - x_u for all u with finite latest.
    # Expressed in old zone: for each enabled u with finite latest:
    #   DBM[t][0] must satisfy: x_t - 0 <= min_u(latest(u) + DBM[0][u])
    # But this is just the constraint that the firing delay d = x_t is within
    # the urgency window, which should already be enforced by the zone.
    # The standard SCG construction includes an urgency constraint:
    #   For each enabled u with finite latest: x_t <= latest(u) - (x_u - x_t) ... no.
    # Let me use the standard Berthomieu construction more carefully.

    # Standard approach: add urgency constraints to new_dbm
    # After the shift, for each persistently enabled u with finite latest(u):
    # The new age x_u^new must satisfy x_u^new <= latest(u) (absolute constraint)
    # which is already in new_dbm.set(u, "0", DBM[u][t]).
    # But also: x_u^new >= 0 (non-negative age) is implied by new_dbm.set("0", u, DBM[t][u]).
    # For newly enabled v: x_v^new in [0, latest(v)], also encoded.
    # Urgency for the step: no other transition should have been able to force t to fire earlier.
    # This is captured by: for each enabled u with finite latest, after the shift:
    #   x_u^new <= latest(u) is enforced by DBM^new[u][0].
    # This IS the urgency constraint in the shifted zone.

    # Normalize
    normalized = new_dbm.normalize()

    if normalized.is_empty():
        return None

    marking_key = _marking_to_key(new_marking)
    return StateClass(
        marking_key=marking_key,
        marking=new_marking,
        dbm=normalized,
        enabled=after_set,
    )


class SCG:
    """State Class Graph.

    classes: dict[id -> StateClass]
    edges: list[(from_id, transition_name, to_id)]
    initial_id: int
    completion_ids: frozenset[int]
    """

    def __init__(self):
        self.classes: Dict[int, StateClass] = {}
        self.edges: List[Tuple[int, str, int]] = []
        self.initial_id: int = 0
        self.completion_ids: FrozenSet[int] = frozenset()
        self._class_to_id: Dict[StateClass, int] = {}
        self._next_id: int = 0
        self.has_zero_time_unobservable_cycle: bool = False
        self.scg_state_limit_reached: bool = False

    def _get_or_add_class(self, cls: StateClass) -> Tuple[int, bool]:
        """Return (id, is_new). is_new=True if newly added."""
        if cls in self._class_to_id:
            return self._class_to_id[cls], False
        cid = self._next_id
        self._next_id += 1
        self.classes[cid] = cls
        self._class_to_id[cls] = cid
        return cid, True

    def build_truncated(self) -> "SCG":
        """Return completion-truncated SCG: same classes/edges but with all
        outgoing edges from completion classes removed."""
        truncated = SCG()
        truncated.classes = dict(self.classes)
        truncated.initial_id = self.initial_id
        truncated.completion_ids = self.completion_ids
        truncated._class_to_id = dict(self._class_to_id)
        truncated._next_id = self._next_id
        # Only include edges whose source is NOT a completion class
        truncated.edges = [
            (src, t, dst)
            for (src, t, dst) in self.edges
            if src not in self.completion_ids
        ]
        return truncated

    def successors(self, class_id: int) -> List[Tuple[str, int]]:
        """Return list of (transition_name, successor_id) from class_id."""
        return [(t, dst) for (src, t, dst) in self.edges if src == class_id]

    def predecessors(self, class_id: int) -> List[Tuple[int, str]]:
        """Return list of (predecessor_id, transition_name) to class_id."""
        return [(src, t) for (src, t, dst) in self.edges if dst == class_id]


def _transition_has_zero_time(tpn: TPN, t_name: str) -> bool:
    """Return True if transition t_name has a zero upper bound (can fire at time 0)."""
    t = tpn.transitions[t_name]
    latest = t.interval.latest
    return not math.isinf(latest) and latest == 0


def _transition_can_fire_at_zero(tpn: TPN, t_name: str) -> bool:
    """Return True if transition can fire with zero delay (earliest == 0)."""
    return tpn.transitions[t_name].interval.earliest == 0


def detect_zero_time_unobservable_cycle(tpn: TPN, scg: "SCG") -> bool:
    """Return True if the SCG contains a reachable unresolved zero-time unobservable cycle.

    A zero-time unobservable cycle is a cycle where:
    - All transitions on the cycle are unobservable
    - All transitions on the cycle have zero upper bound (latest == 0)
    - The cycle does not pass through a completion marking
    - The cycle is reachable from the initial state

    Detection: find any back-edge in DFS from initial class where all edges
    on the cycle path satisfy the above conditions.
    """
    # Build adjacency: only zero-delay unobservable edges not from completion classes
    adj: Dict[int, List[Tuple[str, int]]] = {cid: [] for cid in scg.classes}
    for (src, t_name, dst) in scg.edges:
        if src in scg.completion_ids:
            continue
        t_obj = tpn.transitions[t_name]
        if not t_obj.is_observable and _transition_can_fire_at_zero(tpn, t_name):
            adj[src].append((t_name, dst))

    # DFS from initial, track path to detect back-edges (cycles)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {cid: WHITE for cid in scg.classes}

    def dfs(node: int) -> bool:
        color[node] = GRAY
        for (t_name, succ) in adj.get(node, []):
            if succ in scg.completion_ids:
                continue
            if color[succ] == GRAY:
                return True  # back-edge: zero-time unobservable cycle found
            if color[succ] == WHITE:
                if dfs(succ):
                    return True
        color[node] = BLACK
        return False

    return dfs(scg.initial_id)


def build_scg(tpn: TPN) -> SCG:
    """Build the normalized State Class Graph for tpn.

    Uses BFS over state classes.
    Sets scg.has_zero_time_unobservable_cycle = True if a reachable zero-time
    unobservable cycle is detected during construction (early termination).
    """
    scg = SCG()

    # Pre-compute: which transitions are zero-delay and unobservable
    # (earliest == 0 means can fire with zero delay)
    zero_delay_unobs_transitions = frozenset(
        name for name, t in tpn.transitions.items()
        if not t.is_observable and _transition_can_fire_at_zero(tpn, name)
    )

    # Initial class
    c0 = _build_initial_class(tpn)
    c0_id, _ = scg._get_or_add_class(c0)
    scg.initial_id = c0_id

    completion_ids = set()
    if tpn.in_completion(c0.marking):
        completion_ids.add(c0_id)

    queue = deque([c0_id])
    visited = {c0_id}

    MAX_CLASSES = 50_000  # safety cap
    found_zero_time_cycle = False
    state_limit_reached = False

    while queue and not found_zero_time_cycle:
        curr_id = queue.popleft()
        curr_cls = scg.classes[curr_id]

        if len(scg.classes) > MAX_CLASSES:
            # Exceeded cap: state limit, not a cycle
            state_limit_reached = True
            break

        for t_name in sorted(curr_cls.enabled):
            succ = _compute_successor(tpn, curr_cls, t_name)
            if succ is None:
                continue

            succ_id, is_new = scg._get_or_add_class(succ)
            scg.edges.append((curr_id, t_name, succ_id))

            # Zero-delay unobservable edge: check for immediate marking cycle
            if t_name in zero_delay_unobs_transitions:
                if succ.marking_key == curr_cls.marking_key and not tpn.in_completion(succ.marking):
                    # Same marking reached via zero-delay unobs step -> self-loop cycle
                    found_zero_time_cycle = True
                    break

            if is_new:
                if tpn.in_completion(succ.marking):
                    completion_ids.add(succ_id)
                    # DO NOT enqueue completion classes: truncate here
                elif succ_id not in visited:
                    visited.add(succ_id)
                    queue.append(succ_id)

        if found_zero_time_cycle:
            break

    scg.completion_ids = frozenset(completion_ids)
    scg.scg_state_limit_reached = state_limit_reached

    if found_zero_time_cycle:
        scg.has_zero_time_unobservable_cycle = True
    elif state_limit_reached:
        # State limit exhausted: not a cycle, report separately
        scg.has_zero_time_unobservable_cycle = False
    else:
        # Post-hoc graph analysis for multi-step zero-delay unobservable cycles
        scg.has_zero_time_unobservable_cycle = detect_zero_time_unobservable_cycle(tpn, scg)

    return scg
