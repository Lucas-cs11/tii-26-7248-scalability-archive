"""
Build observation synchronizer from completion-truncated SCG.

Implements Algorithm S2.1: Candidate Complete-Word Construction.

Reachability maps (Definition S2.1):
  reach_N(X, alpha): SCG classes reachable from X via paths with
    observable projection alpha and fault_class=N.
  reach_F(X, alpha): same but fault_class=F (at least one fault transition).
  reach_star(X, alpha): reach_N union reach_F.

For alpha = "" (epsilon): paths using only unobservable transitions.
For alpha = e (single label): paths with exactly one observable transition
  labeled e, with any number of unobservable transitions before/after.

Synchronizer transition (Definition S2.2):
  delta((X_N, X_F), e):
    X_N' = reach_N(X_N, e)
    X_F' = reach_star(X_F, e) union reach_F(X_N, e)

The construction is deterministic: the successor of a synchronizer state
depends only on that state and the observable label.  ``profile`` is an
optional instrumentation dict; when it is ``None`` every hook is a no-op and
the produced automaton is bit-for-bit identical.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from ..model.tpn import TPN
from ..profiling import Profile, bump
from ..scg.builder import SCG, StateClass


@dataclass
class SyncState:
    """Observation synchronizer state.

    X_N: frozenset of SCG class IDs reachable via normal histories
    X_F: frozenset of SCG class IDs reachable via fault-containing histories
    """

    X_N: FrozenSet[int]
    X_F: FrozenSet[int]

    def key(self) -> Tuple[FrozenSet[int], FrozenSet[int]]:
        return (self.X_N, self.X_F)


def _successors(scg: SCG, class_id: int, profile: Optional[Profile]):
    """Counted SCG successor lookup (identical result to ``scg.successors``)."""
    bump(profile, "sync_successors_calls")
    return scg.successors(class_id)


def _unobservable_closure_N(
    scg: SCG, tpn: TPN, source_ids: FrozenSet[int], profile: Optional[Profile] = None
) -> FrozenSet[int]:
    """Reach all classes from source_ids via zero or more unobservable,
    non-fault transitions. (Normal reachability with epsilon label.)"""
    bump(profile, "sync_closure_N_calls")
    _started = time.perf_counter() if profile is not None else 0.0
    visited: Set[int] = set(source_ids)
    queue = deque(source_ids)
    while queue:
        curr = queue.popleft()
        for (t_name, succ_id) in _successors(scg, curr, profile):
            t = tpn.transitions[t_name]
            # Unobservable and not fault
            if not t.is_observable and not t.is_fault:
                if succ_id not in visited:
                    visited.add(succ_id)
                    queue.append(succ_id)
    if profile is not None:
        profile["runtime_unobservable_closure"] = profile.get(
            "runtime_unobservable_closure", 0.0
        ) + (time.perf_counter() - _started)
    return frozenset(visited)


def _unobservable_closure_any(
    scg: SCG, tpn: TPN, source_ids: FrozenSet[int], profile: Optional[Profile] = None
) -> FrozenSet[int]:
    """Reach all classes from source_ids via zero or more unobservable
    transitions (may include fault transitions). (Any fault class.)"""
    bump(profile, "sync_closure_any_calls")
    _started = time.perf_counter() if profile is not None else 0.0
    visited: Set[int] = set(source_ids)
    queue = deque(source_ids)
    while queue:
        curr = queue.popleft()
        for (t_name, succ_id) in _successors(scg, curr, profile):
            t = tpn.transitions[t_name]
            if not t.is_observable:
                if succ_id not in visited:
                    visited.add(succ_id)
                    queue.append(succ_id)
    if profile is not None:
        profile["runtime_unobservable_closure"] = profile.get(
            "runtime_unobservable_closure", 0.0
        ) + (time.perf_counter() - _started)
    return frozenset(visited)


def _unobservable_closure_F(
    scg: SCG, tpn: TPN, source_ids: FrozenSet[int], profile: Optional[Profile] = None
) -> FrozenSet[int]:
    """Reach classes from source_ids via paths using only unobservable
    transitions where at least one fault transition occurs.

    Returns the set of classes reachable via such fault-containing
    unobservable paths from source_ids.
    """
    bump(profile, "sync_closure_F_calls")
    _started = time.perf_counter() if profile is not None else 0.0
    # BFS tracking whether a fault has been seen
    # State: (class_id, has_fault)
    # We want all class_ids where has_fault=True is reachable
    start_states = {(cid, False) for cid in source_ids}
    visited: Set[Tuple[int, bool]] = set(start_states)
    queue = deque(start_states)
    fault_reached: Set[int] = set()

    while queue:
        curr_id, has_fault = queue.popleft()
        if has_fault:
            fault_reached.add(curr_id)
        for (t_name, succ_id) in _successors(scg, curr_id, profile):
            t = tpn.transitions[t_name]
            if not t.is_observable:
                new_fault = has_fault or t.is_fault
                state = (succ_id, new_fault)
                if state not in visited:
                    visited.add(state)
                    queue.append(state)
    if profile is not None:
        profile["runtime_unobservable_closure"] = profile.get(
            "runtime_unobservable_closure", 0.0
        ) + (time.perf_counter() - _started)
    return frozenset(fault_reached)


def reach_N(
    scg: SCG,
    tpn: TPN,
    source_ids: FrozenSet[int],
    alpha: str,
    profile: Optional[Profile] = None,
) -> FrozenSet[int]:
    """Classes reachable from source_ids via paths with observable projection
    alpha and fault_class=N (no fault transitions used).

    alpha = "" (epsilon): paths via unobservable, non-fault transitions only.
    alpha = e (single label): paths with exactly one observable transition
      labeled e (non-fault), with non-fault unobservable transitions before/after.
    """
    bump(profile, "sync_reach_N_calls")
    if alpha == "":
        return _unobservable_closure_N(scg, tpn, source_ids, profile)

    # alpha is a single observable label e
    # Step 1: expand via non-fault unobservable transitions
    # Step 2: take exactly one observable non-fault transition labeled alpha
    # Step 3: expand via non-fault unobservable transitions
    result: Set[int] = set()

    # Pre-closure: reach via non-fault unobservable transitions
    pre_closed = _unobservable_closure_N(scg, tpn, source_ids, profile)

    # Take one observable transition labeled alpha (non-fault)
    mid: Set[int] = set()
    for curr_id in pre_closed:
        for (t_name, succ_id) in _successors(scg, curr_id, profile):
            t = tpn.transitions[t_name]
            if t.is_observable and t.label == alpha and not t.is_fault:
                mid.add(succ_id)

    if not mid:
        return frozenset()

    # Post-closure: reach via non-fault unobservable transitions
    post_closed = _unobservable_closure_N(scg, tpn, frozenset(mid), profile)
    return post_closed


def reach_F(
    scg: SCG,
    tpn: TPN,
    source_ids: FrozenSet[int],
    alpha: str,
    profile: Optional[Profile] = None,
) -> FrozenSet[int]:
    """Classes reachable from source_ids via paths with observable projection
    alpha and fault_class=F (at least one fault transition used).

    alpha = "" (epsilon): paths via unobservable transitions (including fault),
      with at least one fault transition.
    alpha = e (single label): paths with exactly one observable transition
      labeled e, with unobservable transitions (some possibly fault) before/after,
      and at least one fault transition overall.
    """
    bump(profile, "sync_reach_F_calls")
    if alpha == "":
        return _unobservable_closure_F(scg, tpn, source_ids, profile)

    # alpha is a single observable label e
    # We need paths where:
    #   - observable projection = e (exactly one observable transition labeled e)
    #   - at least one fault transition occurs anywhere in the path
    #
    # We track (class_id, has_fault) through BFS
    # Path structure: [unobs*] [e-labeled obs] [unobs*]
    # Fault can appear in any unobservable segment.

    result: Set[int] = set()

    # BFS tracking (class_id, has_fault, phase)
    # phase 0: before the observable transition
    # phase 1: after the observable transition
    # State: (class_id, has_fault, phase)
    start_states: Set[Tuple[int, bool, int]] = {(cid, False, 0) for cid in source_ids}
    visited: Set[Tuple[int, bool, int]] = set(start_states)
    queue = deque(start_states)

    while queue:
        curr_id, has_fault, phase = queue.popleft()

        if phase == 1 and has_fault:
            result.add(curr_id)

        for (t_name, succ_id) in _successors(scg, curr_id, profile):
            t = tpn.transitions[t_name]

            if phase == 0:
                if not t.is_observable:
                    # Unobservable in pre-phase
                    new_fault = has_fault or t.is_fault
                    state = (succ_id, new_fault, 0)
                    if state not in visited:
                        visited.add(state)
                        queue.append(state)
                elif t.is_observable and t.label == alpha:
                    # Take the observable transition (fault transitions are unobservable by definition)
                    # Observable transitions cannot be fault (validated in model)
                    state = (succ_id, has_fault, 1)
                    if state not in visited:
                        visited.add(state)
                        queue.append(state)
            else:  # phase == 1
                if not t.is_observable:
                    # Unobservable in post-phase
                    new_fault = has_fault or t.is_fault
                    state = (succ_id, new_fault, 1)
                    if state not in visited:
                        visited.add(state)
                        queue.append(state)
                # No more observable transitions after phase 1 (projection = e means exactly one)

    return frozenset(result)


def reach_star(
    scg: SCG,
    tpn: TPN,
    source_ids: FrozenSet[int],
    alpha: str,
    profile: Optional[Profile] = None,
) -> FrozenSet[int]:
    """reach_N union reach_F."""
    return reach_N(scg, tpn, source_ids, alpha, profile) | reach_F(
        scg, tpn, source_ids, alpha, profile
    )


def synchronizer_transition(
    scg: SCG,
    tpn: TPN,
    state: SyncState,
    e: str,
    profile: Optional[Profile] = None,
) -> SyncState:
    """delta((X_N, X_F), e):
    X_N' = reach_N(X_N, e)
    X_F' = reach_star(X_F, e) union reach_F(X_N, e)
    """
    bump(profile, "sync_transition_calls")
    x_n_prime = reach_N(scg, tpn, state.X_N, e, profile)
    x_f_prime = reach_star(scg, tpn, state.X_F, e, profile) | reach_F(
        scg, tpn, state.X_N, e, profile
    )
    return SyncState(X_N=x_n_prime, X_F=x_f_prime)


@dataclass
class ObservationSynchronizer:
    """The constructed observation synchronizer.

    states: dict[(X_N_key, X_F_key) -> SyncState]
    edges: list[(from_key, label, to_key)]
    initial_key: (X_N, X_F) as tuple of frozensets
    candidate_words: list of words (tuples of str) that are candidates
    """

    states: Dict[Tuple, SyncState]
    edges: List[Tuple[Tuple, str, Tuple]]
    initial_key: Tuple
    candidate_words: List[Tuple[str, ...]]


def build_synchronizer(
    tpn: TPN,
    scg_truncated: SCG,
    L: int,
    profile: Optional[Profile] = None,
) -> ObservationSynchronizer:
    """Algorithm S2.1: BFS over label-prefix tree up to depth L.

    A word phi is candidate if X_N(phi) intersect C_fin != empty
    AND X_F(phi) intersect C_fin != empty.

    Pruning: stop expanding if X_N = empty.
    """
    c_fin = scg_truncated.completion_ids

    # Initial synchronizer state
    initial_x_n = reach_N(
        scg_truncated, tpn, frozenset({scg_truncated.initial_id}), "", profile
    )
    initial_x_f = reach_F(
        scg_truncated, tpn, frozenset({scg_truncated.initial_id}), "", profile
    )
    initial_state = SyncState(X_N=initial_x_n, X_F=initial_x_f)
    initial_key = initial_state.key()

    states: Dict[Tuple, SyncState] = {initial_key: initial_state}
    edges: List[Tuple[Tuple, str, Tuple]] = []
    candidate_words: List[Tuple[str, ...]] = []

    # BFS queue: (word_tuple, sync_state_key)
    queue: deque = deque()
    queue.append(((), initial_key))

    sigma = tpn.observable_alphabet

    # The synchronizer transition is a pure function of (state, label): the
    # reachability maps depend on the SCG and the two class-id sets only.  The
    # BFS visits one (word, state) pair per prefix, so the same state is
    # re-expanded many times; memoising the transition is exact and removes
    # that duplication without changing any reachable state, edge or word.
    transition_cache: Dict[Tuple[Tuple, str], Tuple[SyncState, Tuple]] = {}

    while queue:
        word, curr_key = queue.popleft()
        bump(profile, "sync_words_explored")
        curr_state = states[curr_key]

        # Check if candidate
        if (
            curr_state.X_N & c_fin
            and curr_state.X_F & c_fin
        ):
            candidate_words.append(word)
            bump(profile, "sync_candidate_words")

        # Prune if X_N is empty or at depth limit
        if not curr_state.X_N or len(word) >= L:
            continue

        for e in sorted(sigma):  # sorted for determinism
            memo_key = (curr_key, e)
            cached = transition_cache.get(memo_key)
            if cached is not None:
                bump(profile, "sync_transition_memo_hits")
                next_state, next_key = cached
            else:
                next_state = synchronizer_transition(
                    scg_truncated, tpn, curr_state, e, profile
                )
                next_key = next_state.key()
                transition_cache[memo_key] = (next_state, next_key)

            # Only enqueue if X_N' is nonempty (pruning condition)
            if not next_state.X_N:
                continue

            if next_key not in states:
                states[next_key] = next_state

            edges.append((curr_key, e, next_key))

            next_word = word + (e,)
            queue.append((next_word, next_key))

    bump(profile, "sync_states_final", len(states))
    bump(profile, "sync_edges_final", len(edges))
    bump(profile, "sync_words_final", len(candidate_words))
    return ObservationSynchronizer(
        states=states,
        edges=edges,
        initial_key=initial_key,
        candidate_words=candidate_words,
    )
