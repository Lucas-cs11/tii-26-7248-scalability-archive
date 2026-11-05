"""
Baseline 3: MSCG-LP-Adapted (based on Basile et al. 2017)

Enumerate bounded complete normal/fault executions directly from the SCG
(no synchronizer, no conditioned temporal expansion, no envelope check),
then LP-pair matching on shared observable words.

Algorithm:
1. Build SCG (same as main verifier)
2. From the truncated SCG, enumerate all paths from initial to completion_ids
   where:
   - Normal path: no fault transitions used
   - Fault path: at least one fault transition used
   - Observable projection length <= L
3. Group paths by observable word
4. For each word where both normal and fault paths exist:
   - For each (sigma_N, sigma_F) pair: call confirm_ambiguity directly
   - If feasible -> certificate

Does NOT use temporal expansion. Uses raw SCG firing sequences as sigma_N, sigma_F.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Dict, List, Optional, Set, Tuple

from ..model.tpn import TPN
from ..scg.builder import build_scg, SCG
from ..exact_confirmation.lp_solver import confirm_ambiguity
from ..repair.algorithm import Certificate


def _enumerate_scg_paths(
    tpn: TPN, scg_truncated: SCG, L: int
) -> Tuple[Dict[Tuple[str, ...], List[Tuple[str, ...]]], Dict[Tuple[str, ...], List[Tuple[str, ...]]]]:
    """Enumerate complete SCG paths grouped by observable word.

    Returns (normal_paths, fault_paths) where each is a dict mapping
    observable_word_tuple -> list of firing_sequence_tuples.

    A 'complete' path is one that reaches a completion class.
    Normal path: no fault transitions fired.
    Fault path: at least one fault transition fired.

    BFS state: (class_id, firing_seq_tuple, has_fault, obs_word_tuple)
    Cycle pruning: if (class_id, obs_word) was already visited with the
    same fault status, skip to bound path lengths.
    """
    # normal_paths[obs_word] = list of sigma tuples (no fault)
    normal_paths: Dict[Tuple[str, ...], List[Tuple[str, ...]]] = {}
    # fault_paths[obs_word] = list of sigma tuples (has fault)
    fault_paths: Dict[Tuple[str, ...], List[Tuple[str, ...]]] = {}

    # BFS state: (class_id, firing_seq, has_fault, obs_word)
    # Cycle pruning key: (class_id, obs_word, has_fault)
    initial_state = (scg_truncated.initial_id, (), False, ())
    visited: Set[Tuple[int, Tuple[str, ...], bool]] = {
        (scg_truncated.initial_id, (), False)
    }
    queue: deque = deque([initial_state])

    while queue:
        class_id, firing_seq, has_fault, obs_word = queue.popleft()

        # Check if this is a completion class
        if class_id in scg_truncated.completion_ids:
            if has_fault:
                fault_paths.setdefault(obs_word, []).append(firing_seq)
            else:
                normal_paths.setdefault(obs_word, []).append(firing_seq)
            # Do not expand from completion classes
            continue

        # Prune if at observable depth limit
        if len(obs_word) >= L:
            continue

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

            prune_key = (succ_id, new_obs_word, new_fault)
            if prune_key in visited:
                continue
            visited.add(prune_key)
            queue.append((succ_id, new_firing_seq, new_fault, new_obs_word))

    return normal_paths, fault_paths


def verify_mscg_lp_adapted(
    tpn: TPN,
    L: int,
    H: float,
    budget: Optional[int] = None,
    complete_set: bool = False,
) -> Dict:
    """MSCG-LP-Adapted baseline: enumerate SCG paths directly and LP-pair match.

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

    # Step 3: Enumerate normal and fault paths by observable word (no synchronizer)
    t0 = time.perf_counter()
    normal_paths, fault_paths = _enumerate_scg_paths(tpn, scg_truncated, L)
    stats["runtime_sync"] = time.perf_counter() - t0
    stats["sync_states"] = 0

    # Candidate words: words with both normal and fault paths
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

    # Step 4: LP-pair matching for each candidate word
    t_lp_total = 0.0
    total_terminal_pairs = 0
    total_lp_calls = 0
    words_processed = 0

    for phi in candidate_words:
        words_processed += 1
        n_paths = normal_paths[phi]
        f_paths = fault_paths[phi]

        found_cert_this_word = False
        for sigma_N in n_paths:
            for sigma_F in f_paths:
                total_terminal_pairs += 1

                t0 = time.perf_counter()
                result = confirm_ambiguity(tpn, sigma_N, sigma_F, H)
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
                        sigma_N=sigma_N,
                        sigma_F=sigma_F,
                        overlap=overlap,
                    )
                    certificates.append(cert)
                    found_cert_this_word = True

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

            if found_cert_this_word and not complete_set:
                break

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
