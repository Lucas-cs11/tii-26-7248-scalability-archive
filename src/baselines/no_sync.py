"""
Baseline 1: NoSync

Skip the observation synchronizer. Instead enumerate observable words
directly from the completion-truncated SCG path language up to depth L.

BFS over the truncated SCG collecting all complete paths (paths reaching
completion_ids) whose observable projection has length <= L. For each
distinct observable word, run temporal expansion + LP exactly as the main
verifier.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Dict, List, Optional, Set, Tuple

from ..model.tpn import TPN
from ..scg.builder import build_scg, SCG
from ..temporal_expansion.builder import build_expansion
from ..exact_confirmation.lp_solver import confirm_ambiguity
from ..repair.algorithm import Certificate


def _min_bound(a, b):
    if math.isinf(a):
        return b
    if math.isinf(b):
        return a
    return min(a, b)


def _bound_lt(a, b) -> bool:
    if math.isinf(a):
        return False
    if math.isinf(b):
        return True
    return a < b


def _collect_candidate_words(
    tpn: TPN, scg_truncated: SCG, L: int
) -> List[Tuple[str, ...]]:
    """BFS over truncated SCG collecting observable projections of complete paths.

    A complete path ends at a completion class. We collect the set of distinct
    observable words (projection onto observable transition labels) of length <= L
    for all such paths, where both a normal (no fault) and a fault path yield
    that word (analogous to the synchronizer candidate condition).

    Returns list of distinct candidate words.
    """
    # Track (class_id, observable_word_tuple, has_fault)
    # Use a visited set keyed on (class_id, observable_word, has_fault) to
    # bound the BFS (cycle pruning on same-class + same-obs-prefix state).
    # At completion classes, record word and fault status.

    # Collect words seen in normal paths and fault paths separately
    normal_words: Set[Tuple[str, ...]] = set()
    fault_words: Set[Tuple[str, ...]] = set()

    # BFS state: (class_id, obs_word_tuple, has_fault)
    initial = (scg_truncated.initial_id, (), False)
    visited: Set[Tuple[int, Tuple[str, ...], bool]] = {initial}
    queue: deque = deque([initial])

    while queue:
        class_id, obs_word, has_fault = queue.popleft()

        # Check if this is a completion class
        if class_id in scg_truncated.completion_ids:
            if has_fault:
                fault_words.add(obs_word)
            else:
                normal_words.add(obs_word)
            # Do not expand from completion classes (truncated SCG handles this,
            # but we check explicitly to be safe)
            continue

        # Don't expand if already at label depth limit
        if len(obs_word) >= L:
            continue

        for (t_name, succ_id) in scg_truncated.successors(class_id):
            t = tpn.transitions[t_name]
            new_fault = has_fault or t.is_fault

            # Compute new observable word
            if t.is_observable:
                new_obs = obs_word + (t.label,)
                # Prune if exceeds depth limit
                if len(new_obs) > L:
                    continue
            else:
                new_obs = obs_word

            state = (succ_id, new_obs, new_fault)
            if state not in visited:
                visited.add(state)
                queue.append(state)

    # Candidate words: observable words reachable by both a normal and a fault path
    candidate_set = normal_words & fault_words
    return sorted(candidate_set)  # sorted for determinism


def verify_no_sync(
    tpn: TPN,
    L: int,
    H: float,
    budget: Optional[int] = None,
    complete_set: bool = False,
) -> Dict:
    """NoSync baseline: enumerate observable words from SCG path language.

    Returns same dict structure as verify().
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

    # Step 3 (NoSync): Enumerate candidate words from SCG path language
    t0 = time.perf_counter()
    candidate_words = _collect_candidate_words(tpn, scg_truncated, L)
    stats["runtime_sync"] = time.perf_counter() - t0
    stats["sync_states"] = 0
    stats["candidate_words"] = len(candidate_words)

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

    # Steps 4 & 5: Temporal expansion + LP (identical to main verifier)
    t_exp_total = 0.0
    t_lp_total = 0.0
    total_temporal_states = 0
    total_trace_records = 0
    total_terminal_pairs = 0
    total_lp_calls = 0
    words_processed = 0

    for phi in candidate_words:
        t0 = time.perf_counter()
        expansion = build_expansion(tpn, phi, H, budget=budget)
        t_exp_total += time.perf_counter() - t0
        words_processed += 1

        if expansion == "INC":
            any_inconclusive = True
            continue

        total_temporal_states += len(expansion.nodes)
        for nid, records in expansion.records.items():
            total_trace_records += len(records)

        terminal_normal = expansion.terminal_normal
        terminal_faulty = expansion.terminal_faulty

        if not terminal_normal or not terminal_faulty:
            continue

        found_cert_this_word = False
        for (nid_n, rec_n) in terminal_normal:
            J_n = rec_n.J_hat
            for (nid_f, rec_f) in terminal_faulty:
                J_f = rec_f.J_hat

                lo_n, hi_n = J_n
                lo_f, hi_f = J_f

                lo_max = max(lo_n, lo_f)
                hi_min = _min_bound(hi_n, hi_f)

                if _bound_lt(hi_min, lo_max):
                    continue

                total_terminal_pairs += 1

                t0 = time.perf_counter()
                result = confirm_ambiguity(tpn, rec_n.sigma, rec_f.sigma, H)
                t_lp_total += time.perf_counter() - t0
                total_lp_calls += 1

                if result is None:
                    pass
                elif result.get("status") == "SOLVER_UNKNOWN":
                    any_inconclusive = True
                else:
                    overlap = result["overlap"]
                    cert = Certificate(
                        phi=phi,
                        sigma_N=rec_n.sigma,
                        sigma_F=rec_f.sigma,
                        overlap=overlap,
                    )
                    certificates.append(cert)
                    found_cert_this_word = True

                    if not complete_set:
                        stats["candidate_words_processed"] = words_processed
                        stats["terminal_pairs"] = total_terminal_pairs
                        stats["lp_calls"] = total_lp_calls
                        stats["temporal_states"] = total_temporal_states
                        stats["trace_records"] = total_trace_records
                        stats["runtime_expansion"] = t_exp_total
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

            if found_cert_this_word and not complete_set:
                break

    enumeration_complete = not any_inconclusive
    stats["candidate_words_processed"] = words_processed
    stats["terminal_pairs"] = total_terminal_pairs
    stats["lp_calls"] = total_lp_calls
    stats["temporal_states"] = total_temporal_states
    stats["trace_records"] = total_trace_records
    stats["runtime_expansion"] = t_exp_total
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
