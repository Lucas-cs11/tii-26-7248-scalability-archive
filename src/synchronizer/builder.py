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
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from ..model.tpn import TPN
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


def _unobservable_closure_N(
    scg: SCG, tpn: TPN, source_ids: FrozenSet[int]
) -> FrozenSet[int]:
    """Reach all classes from source_ids via zero or more unobservable,
    non-fault transitions. (Normal reachability with epsilon label.)"""
    visited: Set[int] = set(source_ids)
    queue = deque(source_ids)
    while queue:
        curr = queue.popleft()
        for (t_name, succ_id) in scg.successors(curr):
            t = tpn.transitions[t_name]
            # Unobservable and not fault
            if not t.is_observable and not t.is_fault:
                if succ_id not in visited:
                    visited.add(succ_id)
                    queue.append(succ_id)
    return frozenset(visited)


def _unobservable_closure_any(
    scg: SCG, tpn: TPN, source_ids: FrozenSet[int]
) -> FrozenSet[int]:
    """Reach all classes from source_ids via zero or more unobservable
    transitions (may include fault transitions). (Any fault class.)"""
    visited: Set[int] = set(source_ids)
    queue = deque(source_ids)
    while queue:
        curr = queue.popleft()
        for (t_name, succ_id) in scg.successors(curr):
            t = tpn.transitions[t_name]
            if not t.is_observable:
                if succ_id not in visited:
                    visited.add(succ_id)
                    queue.append(succ_id)
    return frozenset(visited)


def _unobservable_closure_F(
    scg: SCG, tpn: TPN, source_ids: FrozenSet[int]
) -> FrozenSet[int]:
    """Reach classes from source_ids via paths using only unobservable
    transitions where at least one fault transition occurs.

    Returns the set of classes reachable via such fault-containing
    unobservable paths from source_ids.
    """
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
        for (t_name, succ_id) in scg.successors(curr_id):
            t = tpn.transitions[t_name]
            if not t.is_observable:
                new_fault = has_fault or t.is_fault
                state = (succ_id, new_fault)
                if state not in visited:
                    visited.add(state)
                    queue.append(state)
    return frozenset(fault_reached)


def reach_N(scg: SCG, tpn: TPN, source_ids: FrozenSet[int], alpha: str) -> FrozenSet[int]:
    """Classes reachable from source_ids via paths with observable projection
    alpha and fault_class=N (no fault transitions used).

    alpha = "" (epsilon): paths via unobservable, non-fault transitions only.
    alpha = e (single label): paths with exactly one observable transition
      labeled e (non-fault), with non-fault unobservable transitions before/after.
    """
    if alpha == "":
        return _unobservable_closure_N(scg, tpn, source_ids)

    # alpha is a single observable label e
    # Step 1: expand via non-fault unobservable transitions
    # Step 2: take exactly one observable non-fault transition labeled alpha
    # Step 3: expand via non-fault unobservable transitions
    result: Set[int] = set()

    # Pre-closure: reach via non-fault unobservable transitions
    pre_closed = _unobservable_closure_N(scg, tpn, source_ids)

    # Take one observable transition labeled alpha (non-fault)
    mid: Set[int] = set()
    for curr_id in pre_closed:
        for (t_name, succ_id) in scg.successors(curr_id):
            t = tpn.transitions[t_name]
            if t.is_observable and t.label == alpha and not t.is_fault:
                mid.add(succ_id)

    if not mid:
        return frozenset()

    # Post-closure: reach via non-fault unobservable transitions
    post_closed = _unobservable_closure_N(scg, tpn, frozenset(mid))
    return post_closed


def reach_F(scg: SCG, tpn: TPN, source_ids: FrozenSet[int], alpha: str) -> FrozenSet[int]:
    """Classes reachable from source_ids via paths with observable projection
    alpha and fault_class=F (at least one fault transition used).

    alpha = "" (epsilon): paths via unobservable transitions (including fault),
      with at least one fault transition.
    alpha = e (single label): paths with exactly one observable transition
      labeled e, with unobservable transitions (some possibly fault) before/after,
      and at least one fault transition overall.
    """
    if alpha == "":
        return _unobservable_closure_F(scg, tpn, source_ids)

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

        for (t_name, succ_id) in scg.successors(curr_id):
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


def reach_star(scg: SCG, tpn: TPN, source_ids: FrozenSet[int], alpha: str) -> FrozenSet[int]:
    """reach_N union reach_F."""
    return reach_N(scg, tpn, source_ids, alpha) | reach_F(scg, tpn, source_ids, alpha)


def synchronizer_transition(
    scg: SCG, tpn: TPN, state: SyncState, e: str
) -> SyncState:
    """delta((X_N, X_F), e):
    X_N' = reach_N(X_N, e)
    X_F' = reach_star(X_F, e) union reach_F(X_N, e)
    """
    x_n_prime = reach_N(scg, tpn, state.X_N, e)
    x_f_prime = reach_star(scg, tpn, state.X_F, e) | reach_F(scg, tpn, state.X_N, e)
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


def build_synchronizer(tpn: TPN, scg_truncated: SCG, L: int) -> ObservationSynchronizer:
    """Algorithm S2.1: BFS over label-prefix tree up to depth L.

    A word phi is candidate if X_N(phi) intersect C_fin != empty
    AND X_F(phi) intersect C_fin != empty.

    Pruning: stop expanding if X_N = empty.
    """
    c_fin = scg_truncated.completion_ids

    # Initial synchronizer state
    initial_x_n = reach_N(
        scg_truncated, tpn, frozenset({scg_truncated.initial_id}), ""
    )
    initial_x_f = reach_F(
        scg_truncated, tpn, frozenset({scg_truncated.initial_id}), ""
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

    while queue:
        word, curr_key = queue.popleft()
        curr_state = states[curr_key]

        # Check if candidate
        if (
            curr_state.X_N & c_fin
            and curr_state.X_F & c_fin
        ):
            candidate_words.append(word)

        # Prune if X_N is empty or at depth limit
        if not curr_state.X_N or len(word) >= L:
            continue

        for e in sorted(sigma):  # sorted for determinism
            next_state = synchronizer_transition(scg_truncated, tpn, curr_state, e)
            next_key = next_state.key()

            # Only enqueue if X_N' is nonempty (pruning condition)
            if not next_state.X_N:
                continue

            if next_key not in states:
                states[next_key] = next_state

            edges.append((curr_key, e, next_key))

            next_word = word + (e,)
            queue.append((next_word, next_key))

    return ObservationSynchronizer(
        states=states,
        edges=edges,
        initial_key=initial_key,
        candidate_words=candidate_words,
    )
