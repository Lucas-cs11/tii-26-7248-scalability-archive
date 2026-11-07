"""
Top-level verification pipeline.

Pipeline:
  1. Build SCG
  2. Build completion-truncated SCG
  3. Build synchronizer -> get candidate words
  4. For each candidate word: build temporal expansion
  5. For each terminal pair with overlapping enclosures: exact LP confirmation
  6. Return result dict
"""
from __future__ import annotations

import math
import time
from fractions import Fraction
from typing import Callable, Dict, List, Optional, Tuple

from .model.tpn import TPN
from .scg.builder import build_scg, SCG
from .synchronizer.builder import build_synchronizer
from .temporal_expansion.builder import build_expansion, TemporalExpansion
from .exact_confirmation.lp_solver import confirm_ambiguity
from .repair.algorithm import Certificate


def verify(
    tpn: TPN,
    L: int,
    H: float,
    budget: Optional[int] = None,
    complete_set: bool = False,
    progress_hook: Optional[Callable[[str, Dict], None]] = None,
    prebuilt_scg=None,
    scg_build_seconds: Optional[float] = None,
) -> Dict:
    """Full verification pipeline.

    Args:
        complete_set: If False (default), return AMB immediately on first confirmed
            certificate (FIRST_CERTIFICATE mode). If True, enumerate all candidates
            before returning (COMPLETE_SET mode) — required for repair and ambiguity
            set metrics.
        prebuilt_scg: Optional already-built SCG for this exact TPN (e.g. loaded
            from the same-model SCG cache).  When given, ``build_scg`` is skipped
            and the cache loader's timing is *not* attributed to construction.
        scg_build_seconds: Optional externally measured construction time for
            ``prebuilt_scg``.  Only used to attribute the build cost to the cell
            that actually paid it; a cache hit passes ``None`` and records 0.

    Returns:
        {
            'result': 'VER' | 'AMB' | 'INC',
            'certificates': list[Certificate],
            'certificate_mode': 'FIRST_CERTIFICATE' | 'COMPLETE_SET',
            'enumeration_complete': bool,
            'stats': {
                'scg_classes': int,
                'scg_edges': int,
                'sync_states': int,
                'candidate_words': int,
                'candidate_words_processed': int,
                'temporal_states': int,
                'trace_records': int,
                'terminal_pairs': int,
                'lp_calls': int,
                'runtime_scg': float,
                'runtime_sync': float,
                'runtime_expansion': float,
                'runtime_lp': float,
                'runtime_total': float,
            }
        }
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
        # R1-4 additive instrumentation (workload counters; no semantic change).
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
    enumeration_complete = False

    def progress(stage: str) -> None:
        if progress_hook is not None:
            progress_hook(stage, dict(stats))

    # Step 1: Build SCG (or reuse an externally built / cache-loaded one).
    # A prebuilt SCG never contributes construction time unless the caller
    # supplies the build duration it actually measured.
    if prebuilt_scg is not None:
        scg = prebuilt_scg
        stats["runtime_scg"] = 0.0 if scg_build_seconds is None else float(scg_build_seconds)
    else:
        t0 = time.perf_counter()
        scg = build_scg(tpn)
        stats["runtime_scg"] = time.perf_counter() - t0
        if scg_build_seconds is not None:
            stats["runtime_scg"] = float(scg_build_seconds)
    stats["scg_classes"] = len(scg.classes)
    stats["scg_edges"] = len(scg.edges)
    progress("SCG_COMPLETE")

    # A capped SCG is incomplete.  In particular, an empty completion_ids set
    # cannot establish that completion is unreachable when exploration stopped
    # at the class limit.  Report the limiting mechanism before semantic checks.
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

    # Guard: completion must be reachable for the property to be meaningful
    if len(scg.completion_ids) == 0:
        stats["scg_classes"] = len(scg.classes)
        stats["scg_edges"] = len(scg.edges)
        stats["runtime_total"] = time.perf_counter() - t_start
        return {
            "result": "INC",
            "inconclusive_reason": "COMPLETION_UNREACHABLE",
            "certificates": [],
            "certificate_mode": certificate_mode,
            "enumeration_complete": False,
            "stats": stats,
        }

    # P0-2: Detect zero-time unobservable cycle before any further analysis
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

    # Step 2: Completion-truncated SCG
    scg_truncated = scg.build_truncated()

    # Step 3: Build synchronizer -> candidate words
    t0 = time.perf_counter()
    sync = build_synchronizer(tpn, scg_truncated, L)
    stats["runtime_sync"] = time.perf_counter() - t0
    stats["sync_states"] = len(sync.states)
    stats["candidate_words"] = len(sync.candidate_words)
    progress("SYNCHRONIZER_COMPLETE")

    # No candidates: diagnosable (exhaustive absence established)
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

    # Step 4 & 5: For each candidate word, build temporal expansion and check pairs
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

        # Collect stats
        total_temporal_states += len(expansion.nodes)
        for nid, records in expansion.records.items():
            total_trace_records += len(records)

        # Get terminal records
        terminal_normal = expansion.terminal_normal
        terminal_faulty = expansion.terminal_faulty

        stats["terminal_normal_records"] += len(terminal_normal)
        stats["terminal_faulty_records"] += len(terminal_faulty)

        if not terminal_normal or not terminal_faulty:
            continue

        # Step 5: Check overlapping enclosures, then LP confirmation
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
                    # Disjoint enclosures: skip LP
                    stats["enclosure_rejected_pairs"] += 1
                    continue

                total_terminal_pairs += 1

                # Exact LP confirmation
                t0 = time.perf_counter()
                result = confirm_ambiguity(tpn, rec_n.sigma, rec_f.sigma, H)
                t_lp_total += time.perf_counter() - t0
                total_lp_calls += 1

                if result is None:
                    # LP proved infeasible: no ambiguity for this pair
                    pass
                elif result.get("status") == "SOLVER_UNKNOWN":
                    # LP solver inconclusive: mark as inconclusive, continue
                    any_inconclusive = True
                else:
                    # FEASIBLE: confirmed ambiguity certificate
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
                        # FIRST_CERTIFICATE mode: return immediately
                        stats["candidate_words_processed"] = words_processed
                        stats["terminal_pairs"] = total_terminal_pairs
                        stats["lp_calls"] = total_lp_calls
                        stats["temporal_states"] = total_temporal_states
                        stats["trace_records"] = total_trace_records
                        stats["runtime_expansion"] = t_exp_total
                        stats["runtime_lp"] = t_lp_total
                        stats["runtime_total"] = time.perf_counter() - t_start
                        progress("FIRST_CERTIFICATE_COMPLETE")
                        return {
                            "result": "AMB",
                            "inconclusive_reason": None,
                            "certificates": certificates,
                            "certificate_mode": certificate_mode,
                            "enumeration_complete": False,
                            "stats": stats,
                        }

            if found_cert_this_word and not complete_set:
                break  # inner loop already returned above; this is belt-and-suspenders
        stats["candidate_words_processed"] = words_processed
        stats["temporal_states"] = total_temporal_states
        stats["trace_records"] = total_trace_records
        stats["terminal_pairs"] = total_terminal_pairs
        stats["lp_calls"] = total_lp_calls
        stats["runtime_expansion"] = t_exp_total
        stats["runtime_lp"] = t_lp_total
        stats["runtime_total"] = time.perf_counter() - t_start
        progress("HISTORY_ANALYSIS_PARTIAL")

    # enumeration_complete is True only if ALL candidates were fully analyzed
    # (no INC expansions, no solver unknowns that prevented full enumeration)
    enumeration_complete = not any_inconclusive
    stats["candidate_words_processed"] = words_processed
    stats["terminal_pairs"] = total_terminal_pairs
    stats["lp_calls"] = total_lp_calls
    stats["temporal_states"] = total_temporal_states
    stats["trace_records"] = total_trace_records
    stats["runtime_expansion"] = t_exp_total
    stats["runtime_lp"] = t_lp_total
    stats["runtime_total"] = time.perf_counter() - t_start
    progress("VERIFICATION_COMPLETE")

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


def _min_bound(a, b):
    """min(a, b) where either may be float('inf')."""
    if math.isinf(a):
        return b
    if math.isinf(b):
        return a
    return min(a, b)


def _bound_lt(a, b) -> bool:
    """True if a < b, handling inf."""
    if math.isinf(a):
        return False
    if math.isinf(b):
        return True
    return a < b
