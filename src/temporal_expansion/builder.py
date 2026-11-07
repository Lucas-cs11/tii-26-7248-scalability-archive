"""
Observation-conditioned temporal expansion (Algorithm S3.2).

For a candidate word phi = e1...em, builds the expansion graph G_phi.
Each node s = (M, B, k, q) where:
  M:  marking
  B:  residual-time box (maps enabled transition name -> [a_u, b_u])
  k:  progress index (number of matched labels so far, 0..m)
  q:  fault class 'N' or 'F'

Initial node: s0 = (M0, B0, 0, 'N')
  B0(u) = I(u) for each u in E(M0)

Terminal node: M in M_fin and k == m.
Do NOT expand nodes where M in M_fin (first-entry completion).

Progress update mu_phi(k, t):
  - if t unobservable: k (unchanged)
  - if t observable and label == phi[k]: k+1
  - else: None (incompatible, prune)

Delay interval Delta(s, t) = [a_t, min over all enabled u of b_u]
  (enforces strong-time urgency)

Successor box update after delay interval [d_lo, d_hi]:
  - persistently enabled u: [max(0, a_u - d_hi), b_u - d_lo]
  - newly enabled u: I(u)

Trace record: (sigma, J_hat) where sigma is firing sequence and
  J_hat = (lower, upper) enclosing D_H^N(sigma).
  J_hat' = (J_hat + Delta(s,t)) intersect [0, H]

Node equality: exact (marking, box, k, q) comparison.
Records: trace record added only if sigma not already stored at node.
Returns 'INC' string if budget exhausted.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, FrozenSet, List, Optional, Set, Tuple, Union

from ..model.tpn import INF, TPN
from ..profiling import Profile, bump


# A residual interval for one transition: (earliest_res, latest_res)
# latest_res may be float('inf')
ResidualInterval = Tuple[Fraction, object]  # (a_u, b_u)


def _frac(v) -> Fraction:
    if isinstance(v, Fraction):
        return v
    return Fraction(v)


def _add_bound(a: object, b: object) -> object:
    """Add two bounds, treating inf correctly."""
    if math.isinf(a) or math.isinf(b):
        return INF
    return _frac(a) + _frac(b)


def _sub_bound(a: object, b: object) -> object:
    """a - b, where b is finite Fraction."""
    if math.isinf(a):
        return INF
    return _frac(a) - _frac(b)


# Box: frozenset of (transition_name, a_u, b_u_repr)
# We represent box as a dict for computation and convert to frozenset for hashing.
BoxDict = Dict[str, ResidualInterval]  # name -> (a_u, b_u)


def _box_key(box: BoxDict) -> FrozenSet:
    """Convert box dict to a hashable frozenset."""
    items = []
    for name, (a, b) in box.items():
        b_repr = "inf" if math.isinf(b) else b
        items.append((name, a, b_repr))
    return frozenset(items)


def _marking_key(marking: Dict[str, int]) -> FrozenSet:
    return frozenset(marking.items())


@dataclass(frozen=True)
class ExpansionNode:
    """A node in the temporal expansion graph.

    marking_key: hashable representation of marking
    box_key: hashable representation of residual box
    k: matched label count (progress index into phi)
    q: 'N' or 'F'
    """

    marking_key: FrozenSet
    box_key: FrozenSet
    k: int
    q: str

    # Non-frozen mutable data stored separately (accessed via expansion graph)


@dataclass
class TraceRecord:
    """A trace record stored at an expansion node.

    sigma: firing sequence (tuple of transition names)
    J_hat: (lower, upper) duration enclosure; lower is Fraction, upper is Fraction or H (float)
    """

    sigma: Tuple[str, ...]
    J_hat: Tuple[Fraction, object]  # (lower, upper), upper may be inf or H


class TemporalExpansion:
    """The constructed temporal expansion graph for one candidate word phi."""

    def __init__(self, phi: Tuple[str, ...], profile: Profile | None = None):
        self.phi = phi
        self.profile = profile
        self.nodes: List[ExpansionNode] = []
        self.node_data: Dict[int, Tuple[Dict[str, int], BoxDict]] = {}  # id -> (marking, box)
        self.edges: List[Tuple[int, str, int]] = []
        self.records: Dict[int, List[TraceRecord]] = {}  # node_id -> records
        self.terminal_normal: List[Tuple[int, TraceRecord]] = []
        self.terminal_faulty: List[Tuple[int, TraceRecord]] = []
        self._node_index: Dict[ExpansionNode, int] = {}
        self._record_sigmas: Dict[int, Set[Tuple[str, ...]]] = {}
        self._next_id: int = 0

    def get_or_create_node(
        self,
        node: ExpansionNode,
        marking: Dict[str, int],
        box: BoxDict,
    ) -> Tuple[int, bool]:
        """Return (node_id, is_new)."""
        bump(self.profile, "exp_node_lookups")
        if node in self._node_index:
            bump(self.profile, "exp_node_reuses")
            return self._node_index[node], False
        bump(self.profile, "exp_nodes_created")
        nid = self._next_id
        self._next_id += 1
        self.nodes.append(node)
        self.node_data[nid] = (marking, box)
        self.records[nid] = []
        self._node_index[node] = nid
        return nid, True

    def add_record(self, node_id: int, record: TraceRecord) -> bool:
        """Add record if sigma not already present. Returns True if added."""
        bump(self.profile, "exp_record_adds")
        sigmas = self._record_sigmas.get(node_id)
        if sigmas is None:
            sigmas = set()
            self._record_sigmas[node_id] = sigmas
        bump(self.profile, "exp_record_checks")
        if record.sigma in sigmas:
            bump(self.profile, "exp_record_dedup_hits")
            return False
        sigmas.add(record.sigma)
        self.records[node_id].append(record)
        return True


def build_expansion(
    tpn: TPN,
    phi: Tuple[str, ...],
    H: float,
    budget: Optional[int] = None,
    profile: Profile | None = None,
) -> Union[TemporalExpansion, str]:
    """Algorithm S3.2: observation-conditioned temporal expansion.

    Returns TemporalExpansion on success, or 'INC' if budget exhausted.

    budget: maximum number of trace records to generate before returning INC.
    """
    m = len(phi)
    H_frac = Fraction(H) if not math.isinf(H) else INF

    exp = TemporalExpansion(phi, profile)

    # Initial node
    marking0 = dict(tpn.initial_marking)
    enabled0 = tpn.enabled_set(marking0)
    box0: BoxDict = {}
    for u in enabled0:
        t_u = tpn.transitions[u]
        box0[u] = (t_u.interval.earliest, t_u.interval.latest)

    node0_key = ExpansionNode(
        marking_key=_marking_key(marking0),
        box_key=_box_key(box0),
        k=0,
        q="N",
    )
    n0_id, _ = exp.get_or_create_node(node0_key, marking0, box0)
    initial_record = TraceRecord(sigma=(), J_hat=(Fraction(0), Fraction(0)))
    exp.add_record(n0_id, initial_record)

    total_records = 1

    # Pure-function caches keyed by the marking identity.  ``enabled_set``,
    # ``fire``, ``persistent_enabled``, ``newly_enabled`` and ``in_completion``
    # depend only on the marking (and the fired transition), so memoising them
    # cannot change any box, record or verdict; it only avoids recomputing the
    # same enabling/marking data for every (node, sigma) visit.
    enabled_cache: Dict[FrozenSet, Tuple[str, ...]] = {}
    completion_cache: Dict[FrozenSet, bool] = {}
    fire_cache: Dict[Tuple[FrozenSet, str], Tuple[Dict[str, int], FrozenSet]] = {}
    clocks_cache: Dict[Tuple[FrozenSet, str], Tuple[FrozenSet[str], FrozenSet[str]]] = {}

    def enabled_of(marking: Dict[str, int], marking_key: FrozenSet):
        cached = enabled_cache.get(marking_key)
        if cached is None:
            cached = tuple(tpn.enabled_set(marking))
            enabled_cache[marking_key] = cached
        return cached

    def in_completion(marking: Dict[str, int], marking_key: FrozenSet) -> bool:
        cached = completion_cache.get(marking_key)
        if cached is None:
            cached = tpn.in_completion(marking)
            completion_cache[marking_key] = cached
        return cached

    def fire_of(marking: Dict[str, int], marking_key: FrozenSet, t_name: str):
        cache_key = (marking_key, t_name)
        cached = fire_cache.get(cache_key)
        if cached is None:
            new_marking = tpn.fire(marking, t_name)
            cached = (new_marking, _marking_key(new_marking))
            fire_cache[cache_key] = cached
        return cached

    def clocks_of(marking: Dict[str, int], marking_key: FrozenSet, t_name: str):
        cache_key = (marking_key, t_name)
        cached = clocks_cache.get(cache_key)
        if cached is None:
            cached = (
                tpn.persistent_enabled(marking, t_name),
                tpn.newly_enabled(marking, t_name),
            )
            clocks_cache[cache_key] = cached
        return cached

    # Worklist: (node_id, sigma, J_hat)
    queue: deque = deque()
    queue.append((n0_id, (), (Fraction(0), Fraction(0))))

    while queue:
        if budget is not None and total_records > budget:
            return "INC"

        curr_id, sigma, J_hat = queue.popleft()
        bump(profile, "exp_worklist_pops")
        curr_node = exp.nodes[curr_id]
        marking, box = exp.node_data[curr_id]

        # Do not expand if in completion marking (first-entry)
        if in_completion(marking, curr_node.marking_key):
            # Terminal: check if k == m
            # (handled below when we add the record at terminal node)
            continue

        # Get enabled transitions
        enabled = enabled_of(marking, curr_node.marking_key)

        # Compute upper bound for urgency: min over all enabled u of b_u
        urgency_upper = INF
        for u in enabled:
            b_u = box.get(u, (Fraction(0), INF))[1]
            if not math.isinf(b_u):
                if math.isinf(urgency_upper) or b_u < urgency_upper:
                    urgency_upper = b_u

        for t_name in enabled:
            t_obj = tpn.transitions[t_name]

            # Progress update
            if not t_obj.is_observable:
                k_prime = curr_node.k
            elif curr_node.k < m and t_obj.label == phi[curr_node.k]:
                k_prime = curr_node.k + 1
            else:
                # Incompatible observable transition: prune
                continue

            # Delay interval Delta(s, t) = [a_t, urgency_upper]
            a_u, b_u = box.get(t_name, (t_obj.interval.earliest, t_obj.interval.latest))
            delta_lo = a_u  # earliest residual for t
            delta_hi = urgency_upper  # min over all enabled of b_u

            # Delta must be valid interval
            if not math.isinf(delta_hi) and delta_hi < delta_lo:
                # Empty interval: this transition cannot fire
                continue
            if math.isinf(delta_lo):
                # Cannot fire (a_t = inf means t cannot fire)
                continue

            # J_hat' = (J_hat + Delta) intersect [0, H]
            j_lo = J_hat[0] + delta_lo
            j_hi = _add_bound(J_hat[1], delta_hi)

            # Intersect with [0, H]
            j_lo_clipped = max(Fraction(0), j_lo)
            if not math.isinf(H_frac):
                j_hi_clipped = min(j_hi, H_frac) if not math.isinf(j_hi) else H_frac
            else:
                j_hi_clipped = j_hi

            if not math.isinf(j_hi_clipped) and j_hi_clipped < j_lo_clipped:
                # Empty: prune
                continue

            # Compute new marking (cached by marking identity + transition)
            new_marking, new_marking_key = fire_of(marking, curr_node.marking_key, t_name)

            # Compute successor box
            persistent, newly = clocks_of(marking, curr_node.marking_key, t_name)

            new_box: BoxDict = {}
            for u in enabled_of(new_marking, new_marking_key):
                if u in persistent:
                    a_old, b_old = box.get(u, (Fraction(0), INF))
                    # [max(0, a_u - d_hi), b_u - d_lo]
                    new_a = max(Fraction(0), _sub_bound(a_old, delta_hi) if not math.isinf(delta_hi) else Fraction(0))
                    if math.isinf(delta_hi):
                        # d_hi = inf means delay can be arbitrarily large
                        # max(0, a_u - inf) = max(0, -inf) = 0
                        new_a = Fraction(0)
                    else:
                        sub_val = _frac(a_old) - _frac(delta_hi)
                        new_a = max(Fraction(0), sub_val)
                    new_b = _sub_bound(b_old, delta_lo)
                    if not math.isinf(new_b) and new_b < new_a:
                        # Empty residual: this should not happen for feasible firing
                        new_a = Fraction(0)
                        new_b = Fraction(0)
                    new_box[u] = (new_a, new_b)
                else:
                    # Newly enabled: use static interval
                    t_u = tpn.transitions[u]
                    new_box[u] = (t_u.interval.earliest, t_u.interval.latest)

            # Fault class update
            if curr_node.q == "F" or t_obj.is_fault:
                q_prime = "F"
            else:
                q_prime = "N"

            # Build successor node
            succ_node = ExpansionNode(
                marking_key=new_marking_key,
                box_key=_box_key(new_box),
                k=k_prime,
                q=q_prime,
            )

            sigma_prime = sigma + (t_name,)
            j_hat_prime = (j_lo_clipped, j_hi_clipped)

            succ_id, is_new = exp.get_or_create_node(succ_node, new_marking, new_box)
            exp.edges.append((curr_id, t_name, succ_id))
            bump(profile, "exp_edges")

            # Add trace record if not already present
            record = TraceRecord(sigma=sigma_prime, J_hat=j_hat_prime)
            added = exp.add_record(succ_id, record)

            if added:
                total_records += 1
                if budget is not None and total_records > budget:
                    return "INC"

                # Check if terminal
                if in_completion(new_marking, new_marking_key) and k_prime == m:
                    if q_prime == "N":
                        exp.terminal_normal.append((succ_id, record))
                    else:
                        exp.terminal_faulty.append((succ_id, record))

                # Enqueue for expansion (even if succ_id already existed,
                # we may need to expand with this new sigma)
                # But we only expand each (node, sigma) pair once.
                # The worklist is (node_id, sigma, J_hat).
                queue.append((succ_id, sigma_prime, j_hat_prime))

    return exp
