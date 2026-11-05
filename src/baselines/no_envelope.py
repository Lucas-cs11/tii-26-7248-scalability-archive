"""
Baseline 2: NoEnvelope

Use the synchronizer to get candidate words and run temporal expansion,
but call LP on ALL terminal (normal, fault) pairs — skip the enclosure
overlap check (J_n ∩ J_f check) used as a pruning guard in the main verifier.

Identical to the main verifier EXCEPT: the `if _bound_lt(hi_min, lo_max): continue`
guard is removed, so LP is called for every terminal pair regardless of whether
the enclosure intervals overlap.
"""
from __future__ import annotations

import math
import time
from typing import Dict, List, Optional

from ..model.tpn import TPN
from ..scg.builder import build_scg, SCG
from ..synchronizer.builder import build_synchronizer
from ..temporal_expansion.builder import build_expansion
from ..exact_confirmation.lp_solver import confirm_ambiguity
from ..repair.algorithm import Certificate


def verify_no_envelope(
    tpn: TPN,
    L: int,
    H: float,
    budget: Optional[int] = None,
    complete_set: bool = False,
) -> Dict:
    """NoEnvelope baseline: synchronizer + expansion but no enclosure pruning.

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
    enumeration_complete = False

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

    # Step 3: Build synchronizer -> candidate words
    t0 = time.perf_counter()
    sync = build_synchronizer(tpn, scg_truncated, L)
    stats["runtime_sync"] = time.perf_counter() - t0
    stats["sync_states"] = len(sync.states)
    stats["candidate_words"] = len(sync.candidate_words)

    if not sync.candidate_words:
        stats["runtime_total"] = time.perf_counter() - t_start
        enumeration_complete = True
        return {
            "result": "VER",
            "inconclusive_reason": None,
            "certificates": [],
            "certificate_mode": certificate_mode,
            "enumeration_complete": enumeration_complete,
            "stats": stats,
        }

    # Steps 4 & 5: Temporal expansion + LP on ALL terminal pairs (no envelope pruning)
    t_exp_total = 0.0
    t_lp_total = 0.0
    total_temporal_states = 0
    total_trace_records = 0
    total_terminal_pairs = 0
    total_lp_calls = 0
    words_processed = 0

    for phi in sync.candidate_words:
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
            for (nid_f, rec_f) in terminal_faulty:
                # NoEnvelope: skip the enclosure overlap guard entirely
                # Call LP for every (normal, fault) terminal pair
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
