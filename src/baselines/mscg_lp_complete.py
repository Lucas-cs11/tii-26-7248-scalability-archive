"""
Reference Baseline: MSCG-LP-Complete (history-preserving enumeration).

This is the *corrected* version of `mscg_lp_adapted`.  It fixes the two
enumeration-completeness bugs that caused 1206 false-negative VER verdicts:

  1. History-insensitive dedup.  The buggy baseline pruned BFS states with
       prune_key = (class_id, obs_word, has_fault)
     which collapses DISTINCT firing sequences that share the same SCG class,
     observable word, and fault flag but differ in the interleaving of
     concurrent (un)observable transitions.  Under strong-time semantics the
     clock-reset history of a firing sequence is part of its timing, so two
     interleavings reaching the same SCG class can have different completion
     time intervals.  Dropping one of them can drop the only sigma_N/sigma_F
     pair that witnesses ambiguity.

  2. Observable-depth boundary.  The buggy baseline also stopped expanding a
     state as soon as `len(obs_word) >= L`, which drops paths whose observable
     projection already has length L but which still need a trailing run of
     unobservable transitions to reach completion.

This reference version preserves FULL history: the BFS state identity is the
complete firing sequence (no aggregation on class/word/fault), and the only
observable-length bound is `len(new_obs_word) > L` (which keeps |phi| <= L
while still allowing a trailing unobservable completion).  It is therefore
exhaustive over all bounded complete executions with |phi| <= L.

It is intended as a *correctness reference*, not an efficient implementation;
the main verifier achieves the same completeness far more efficiently via the
synchronizer + conditioned temporal expansion + envelope screening.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from ..model.tpn import TPN
from ..scg.builder import build_scg, SCG
from ..exact_confirmation.lp_solver import compute_completion_time_interval
from ..repair.algorithm import Certificate


# Tolerance for the interval-overlap test (matches the independent checker).
OVERLAP_TOL = 1e-4


def _find_overlapping_pair(n_ivs, f_ivs, tol=OVERLAP_TOL):
    """Return (sigma_N, sigma_F, (lo, hi)) for the first overlapping pair.

    n_ivs / f_ivs are lists of (l, u, sigma) with l, u as Fraction (or float).
    The overlap condition max(l_N, l_F) <= min(u_N, u_F) + tol is exactly the
    feasibility of the coupled critical-pair LP `confirm_ambiguity`, since the
    completion-time set of a firing sequence is the projection of its path
    polytope onto the duration axis (an interval).
    """
    if not n_ivs or not f_ivs:
        return None
    n_sorted = sorted(n_ivs, key=lambda x: float(x[0]))
    f_sorted = sorted(f_ivs, key=lambda x: float(x[0]))
    i = j = 0
    while i < len(n_sorted) and j < len(f_sorted):
        lN, uN, sN = n_sorted[i]
        lF, uF, sF = f_sorted[j]
        flN, fuN = float(lN), float(uN)
        flF, fuF = float(lF), float(uF)
        if max(flN, flF) <= min(fuN, fuF) + tol:
            lo = lN if flN >= flF else lF
            hi = uN if fuN <= fuF else uF
            return sN, sF, (lo, hi)
        if fuN < fuF:
            i += 1
        else:
            j += 1
    return None


def _enumerate_scg_paths_complete(
    tpn: TPN, scg_truncated: SCG, L: int
) -> Tuple[Dict[Tuple[str, ...], List[Tuple[str, ...]]], Dict[Tuple[str, ...], List[Tuple[str, ...]]]]:
    """Enumerate ALL complete SCG paths with |phi| <= L, preserving full history.

    Returns (normal_paths, fault_paths) where each is a dict mapping
    observable_word_tuple -> list of firing_sequence_tuples.

    A 'complete' path is one that reaches a completion class.
    Normal path: no fault transitions fired.
    Fault path: at least one fault transition fired.

    Unlike `_enumerate_scg_paths`, the BFS state identity is the full firing
    sequence.  Distinct interleavings are never merged, so the enumeration is
    complete over all firing sequences (the SCG is acyclic for the RMT-FMS
    benchmark, so this terminates).
    """
    normal_paths: Dict[Tuple[str, ...], List[Tuple[str, ...]]] = {}
    fault_paths: Dict[Tuple[str, ...], List[Tuple[str, ...]]] = {}

    # BFS state: (class_id, firing_seq, has_fault, obs_word).
    # Identity key: the full firing_seq tuple (history-preserving).  For an
    # acyclic SCG this is a no-op dedup; it exists only as a defensive guard.
    initial_state = (scg_truncated.initial_id, (), False, ())
    seen: set = {initial_state}
    queue: deque = deque([initial_state])

    while queue:
        class_id, firing_seq, has_fault, obs_word = queue.popleft()

        if class_id in scg_truncated.completion_ids:
            if has_fault:
                fault_paths.setdefault(obs_word, []).append(firing_seq)
            else:
                normal_paths.setdefault(obs_word, []).append(firing_seq)
            continue

        # NOTE: do NOT stop when len(obs_word) >= L.  A path may already carry
        # L observable events and still need a trailing unobservable run to
        # reach completion.  The successor loop below enforces |phi| <= L via
        # `if len(new_obs_word) > L: continue`, which is the only bound needed.

        for (t_name, succ_id) in scg_truncated.successors(class_id):
            t = tpn.transitions[t_name]
            new_fault = has_fault or t.is_fault
            new_firing_seq = firing_seq + (t_name,)

            if t.is_observable:
                new_obs_word = obs_word + (t.label,)
                if len(new_obs_word) > L:
                    continue
            else:
                new_obs_word = obs_word

            state = (succ_id, new_firing_seq, new_fault, new_obs_word)
            if state in seen:
                continue
            seen.add(state)
            queue.append(state)

    return normal_paths, fault_paths


def verify_mscg_lp_complete(
    tpn: TPN,
    L: int,
    H: float,
    budget: Optional[int] = None,
    complete_set: bool = False,
) -> Dict:
    """MSCG-LP-Complete reference baseline: exhaustive SCG-path enumeration + LP.

    Returns the same dict structure as verify().
    """
    t_start = time.perf_counter()
    certificate_mode = "COMPLETE_SET" if complete_set else "FIRST_CERTIFICATE"
    stats = {
        "scg_classes": 0,
        "scg_edges": 0,
        "sync_states": 0,
        "candidate_words": 0,
        "candidate_words_processed": 0,
        "temporal_states": 0,
        "trace_records": 0,
        "terminal_pairs": 0,
        "lp_calls": 0,
        # R1-4 additive instrumentation (path counts; the corrected baseline has
        # no enclosure-screening stage, so enclosure_rejected_pairs stays 0).
        "enclosure_rejected_pairs": 0,
        "terminal_normal_records": 0,
        "terminal_faulty_records": 0,
        "runtime_scg": 0.0,
        "runtime_sync": 0.0,
        "runtime_expansion": 0.0,
        "runtime_lp": 0.0,
        "runtime_total": 0.0,
    }

    certificates: List[Certificate] = []
    any_inconclusive = False

    # Step 1: Build SCG
    t0 = time.perf_counter()
    scg = build_scg(tpn)
    stats["runtime_scg"] = time.perf_counter() - t0
    stats["scg_classes"] = len(scg.classes)
    stats["scg_edges"] = len(scg.edges)

    if len(scg.completion_ids) == 0:
        stats["runtime_total"] = time.perf_counter() - t_start
        return {
            "result": "INC",
            "inconclusive_reason": "COMPLETION_UNREACHABLE",
            "certificates": [],
            "certificate_mode": certificate_mode,
            "enumeration_complete": False,
            "stats": stats,
        }

    if scg.has_zero_time_unobservable_cycle:
        stats["runtime_total"] = time.perf_counter() - t_start
        return {
            "result": "INC",
            "inconclusive_reason": "ZERO_TIME_UNOBSERVABLE_CYCLE",
            "certificates": [],
            "certificate_mode": certificate_mode,
            "enumeration_complete": False,
            "stats": stats,
        }

    if scg.scg_state_limit_reached:
        stats["runtime_total"] = time.perf_counter() - t_start
        return {
            "result": "INC",
            "inconclusive_reason": "SCG_STATE_LIMIT",
            "certificates": [],
            "certificate_mode": certificate_mode,
            "enumeration_complete": False,
            "stats": stats,
        }

    # Step 2: Completion-truncated SCG
    scg_truncated = scg.build_truncated()

    # Step 3: Enumerate normal and fault paths by observable word (complete)
    t0 = time.perf_counter()
    normal_paths, fault_paths = _enumerate_scg_paths_complete(tpn, scg_truncated, L)
    stats["runtime_sync"] = time.perf_counter() - t0
    stats["sync_states"] = 0

    candidate_words = sorted(set(normal_paths.keys()) & set(fault_paths.keys()))
    stats["candidate_words"] = len(candidate_words)
    stats["temporal_states"] = 0
    stats["trace_records"] = 0

    if not candidate_words:
        stats["runtime_total"] = time.perf_counter() - t_start
        return {
            "result": "VER",
            "inconclusive_reason": None,
            "certificates": [],
            "certificate_mode": certificate_mode,
            "enumeration_complete": True,
            "stats": stats,
        }

    # Step 4: interval-overlap matching for each candidate word.
    # For each path we compute its completion-time interval ONCE (a single path
    # LP), then test whether any normal interval overlaps any fault interval.
    # This is mathematically equivalent to the coupled critical-pair LP
    # `confirm_ambiguity` (feasible iff the two intervals intersect) but scales
    # linearly in the number of paths instead of quadratically, which is what
    # makes the corrected baseline tractable on the full benchmark.
    t_lp_total = 0.0
    total_terminal_pairs = 0
    total_lp_calls = 0
    words_processed = 0

    for phi in candidate_words:
        words_processed += 1
        n_paths = normal_paths[phi]
        f_paths = fault_paths[phi]
        total_terminal_pairs += len(n_paths) * len(f_paths)
        stats["terminal_normal_records"] += len(n_paths)
        stats["terminal_faulty_records"] += len(f_paths)

        n_intervals = []
        for sigma_N in n_paths:
            t0 = time.perf_counter()
            iv = compute_completion_time_interval(tpn, sigma_N, H)
            t_lp_total += time.perf_counter() - t0
            total_lp_calls += 2  # one min + one max LP solve
            if iv is not None:
                n_intervals.append((iv[0], iv[1], sigma_N))

        f_intervals = []
        for sigma_F in f_paths:
            t0 = time.perf_counter()
            iv = compute_completion_time_interval(tpn, sigma_F, H)
            t_lp_total += time.perf_counter() - t0
            total_lp_calls += 2
            if iv is not None:
                f_intervals.append((iv[0], iv[1], sigma_F))

        pair = _find_overlapping_pair(n_intervals, f_intervals)
        if pair is not None:
            sigma_N, sigma_F, overlap = pair
            cert = Certificate(
                phi=phi,
                sigma_N=sigma_N,
                sigma_F=sigma_F,
                overlap=overlap,
            )
            certificates.append(cert)

            if not complete_set:
                stats["candidate_words_processed"] = words_processed
                stats["terminal_pairs"] = total_terminal_pairs
                stats["lp_calls"] = total_lp_calls
                stats["runtime_expansion"] = 0.0
                stats["runtime_lp"] = t_lp_total
                stats["runtime_total"] = time.perf_counter() - t_start
                return {
                    "result": "AMB",
                    "inconclusive_reason": None,
                    "certificates": certificates,
                    "certificate_mode": certificate_mode,
                    "enumeration_complete": False,
                    "stats": stats,
                }

    enumeration_complete = not any_inconclusive
    stats["candidate_words_processed"] = words_processed
    stats["terminal_pairs"] = total_terminal_pairs
    stats["lp_calls"] = total_lp_calls
    stats["runtime_expansion"] = 0.0
    stats["runtime_lp"] = t_lp_total
    stats["runtime_total"] = time.perf_counter() - t_start

    if certificates:
        return {
            "result": "AMB",
            "inconclusive_reason": None,
            "certificates": certificates,
            "certificate_mode": certificate_mode,
            "enumeration_complete": enumeration_complete,
            "stats": stats,
        }

    if any_inconclusive:
        return {
            "result": "INC",
            "inconclusive_reason": "BUDGET_EXHAUSTED",
            "certificates": [],
            "certificate_mode": certificate_mode,
            "enumeration_complete": enumeration_complete,
            "stats": stats,
        }

    return {
        "result": "VER",
        "inconclusive_reason": None,
        "certificates": [],
        "certificate_mode": certificate_mode,
        "enumeration_complete": enumeration_complete,
        "stats": stats,
    }
