"""
Certificate-guided LP-based interval search.

Replaces the discrete admissible_interval enumeration with continuous
parameter search using the existing path-polytope LP infrastructure.

Core idea
---------
For a certificate kappa = (phi, sigma_N, sigma_F, J), the overlap J is
non-empty iff the coupled LP (sigma_N and sigma_F share a common total
duration) is feasible.  Making that LP infeasible eliminates the overlap.

Only transitions whose occurrence count DIFFERS between sigma_N and sigma_F
can separate the two paths.  For the RMT-FMS benchmark, t_recov appears
only in sigma_F; increasing its lower bound pushes the fault-path minimum
above the normal-path maximum, which is sufficient for separation.

Transitions that appear equally in both paths (t_proc, the shared
processing transitions) cannot separate the paths directly, but
TIGHTENING their intervals reduces the variance that each path's LP
can exploit independently, narrowing the reachable overlap window.
"""
from __future__ import annotations

import math
import time
from fractions import Fraction
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

from ..model.tpn import AuthorizedRange, Interval, TPN
from ..exact_confirmation.lp_solver import confirm_ambiguity
from .algorithm import Certificate, _lex_less, lexicographic_objective, _find_modified
from .task_constraint import ProposalContext, TaskConstraintResult

# Per-LP solver guard: if a single confirm_ambiguity call exceeds this
# many seconds, the LP is assumed to be pathological and the search
# returns the best-known answer rather than risking a repair-level timeout.
_LP_CALL_LIMIT_S: float = 30.0


def _assert_tightening_direction(
    direction: str, orig: Interval, new: Interval
) -> None:
    """Code contract for the monotone families P1/P3 rely on.

    P1 families move only the lower bound upward: l' >= l and
    u' = max(l', u).  P3 families move only the upper bound downward:
    l' = l and u' <= u.  These characterizations make the overlap
    infeasibility of a returned point meaningful (the binary search /
    candidate sweep search one-dimensional monotone families).  Violating
    the contract is a programming error — fail fast.
    """
    if direction == "P1":
        assert new.earliest >= orig.earliest, (
            f"P1 direction contract violated: {orig} -> {new}"
        )
        assert (
            math.isinf(new.latest) and math.isinf(orig.latest)
        ) or (
            new.latest == orig.latest
            or (not math.isinf(new.latest) and new.earliest >= orig.latest)
        ), f"P1 direction contract violated: {orig} -> {new}"
    elif direction == "P3":
        assert new.earliest == orig.earliest, (
            f"P3 direction contract violated: {orig} -> {new}"
        )
        assert (
            math.isinf(orig.latest) or new.latest <= orig.latest
        ), f"P3 direction contract violated: {orig} -> {new}"
    else:
        raise ValueError(f"unknown tightening direction: {direction!r}")


def _proposal_passes(
    candidate_gate,
    candidate_tpn: TPN,
    ctx: ProposalContext,
) -> bool:
    """Gate a proposal; un-gated search (candidate_gate=None) passes all."""
    if candidate_gate is None:
        return True
    res: TaskConstraintResult = candidate_gate(candidate_tpn, ctx)
    return res.passed


def _certificate_overlap_exists(
    tpn: TPN,
    sigma_N: Tuple[str, ...],
    sigma_F: Tuple[str, ...],
    H: float,
    lp_timeout_s: float = _LP_CALL_LIMIT_S,
) -> Optional[bool]:
    """True iff the coupled LP has a feasible solution (overlap exists).

    Returns None when the LP solver exceeds *lp_timeout_s* or fails
    in a way that prevents a conclusive answer.
    """
    t0 = time.perf_counter()
    try:
        result = confirm_ambiguity(tpn, sigma_N, sigma_F, H)
    except Exception:
        return None  # solver crash → conservatively inconclusive
    elapsed = time.perf_counter() - t0
    if elapsed > lp_timeout_s:
        return None
    if result is None:
        return False  # infeasible → no overlap
    if result.get("status") == "FEASIBLE":
        return True
    return None  # SOLVER_UNKNOWN → inconclusive


def find_separating_lower_bound(
    tpn: TPN,
    sigma_N: Tuple[str, ...],
    sigma_F: Tuple[str, ...],
    H: float,
    t_name: str,
    authorized: AuthorizedRange,
) -> Optional[Fraction]:
    """Binary-search the minimal l_t for t_name that eliminates the overlap.

    Precondition: t_name appears in sigma_F but not in sigma_N (T_delta_F).
    Increasing the lower bound of t_name pushes the fault-path minimum
    upward until the coupled LP becomes infeasible.

    Returns the smallest l_t (as Fraction) that achieves infeasibility,
    or None if even l_t = l_max does not suffice.
    """
    t = tpn.transitions[t_name]
    orig_iv = t.interval

    # Feasibility check at the maximum permitted lower bound.
    hi_iv = Interval(authorized.l_max, max(authorized.l_max, orig_iv.latest))
    test_tpn = tpn.copy_with_interval(t_name, hi_iv)
    if _certificate_overlap_exists(test_tpn, sigma_N, sigma_F, H) is not False:
        return None  # authorised range insufficient (or inconclusive)

    lo = orig_iv.earliest
    hi = authorized.l_max
    best: Optional[Fraction] = None

    while hi - lo > authorized.resolution:
        mid = Fraction((lo + hi) / 2).limit_denominator(1000)
        if mid <= lo:
            mid = lo + Fraction(1, 1000)
        if mid >= hi:
            mid = hi - Fraction(1, 1000)

        mid_iv = Interval(mid, max(mid, orig_iv.latest))
        test_tpn = tpn.copy_with_interval(t_name, mid_iv)

        if _certificate_overlap_exists(test_tpn, sigma_N, sigma_F, H) is not False:
            lo = mid  # still overlapping (or inconclusive) → need larger l_t
        else:
            best = mid
            hi = mid  # try to go lower

    # Final check at the identified point.
    if best is None:
        best = hi
    return best


def find_tightening_interval(
    tpn: TPN,
    sigma_N: Tuple[str, ...],
    sigma_F: Tuple[str, ...],
    H: float,
    t_name: str,
    authorized: AuthorizedRange,
    candidate_gate=None,
) -> Optional[Interval]:
    """Find an interval for a shared adjustable transition that minimises overlap.

    Precondition: t_name appears equally in and (T_cap).
    Tightening (u − l) reduces the delay-variance band the two paths can
    exploit, narrowing the overlap window without moving the mean.

    *candidate_gate* (R3-2a): each swept (l, u) point is a distinct
    candidate assignment; every point is gated before its LP cost is
    computed, so task-rejected points are skipped, not selected.  Points
    rejected by the gate are recorded in the audit with lp_stage='P2'.

    Returns the interval that yields the smallest overlap width among the
    gate-accepted points, or None if no tightening gives measurable
    improvement.
    """
    t = tpn.transitions[t_name]
    orig_iv = t.interval

    # Sweep (l, u) pairs at the authorized resolution.
    res = authorized.resolution
    best_width: Optional[Fraction] = None
    best_iv: Optional[Interval] = None

    l = authorized.l_min
    while l <= authorized.l_max:
        u = l
        while u <= authorized.u_max:
            if u < l:
                u = l
            cand_iv = Interval(l, u)
            if cand_iv == orig_iv:
                u += res
                continue

            test_tpn = tpn.copy_with_interval(t_name, cand_iv)
            if not _proposal_passes(
                candidate_gate,
                test_tpn,
                ProposalContext(
                    lp_stage="P2",
                    path="lp",
                    changes=((t_name, str(orig_iv), str(cand_iv)),),
                ),
            ):
                u += res
                continue

            result = confirm_ambiguity(test_tpn, sigma_N, sigma_F, H)

            if result is not None and result.get("status") == "FEASIBLE":
                overlap = result.get("overlap")
                if overlap is not None:
                    lo_t, hi_t = overlap
                    width = Fraction(hi_t) - Fraction(lo_t)
                    width = max(Fraction(0), width)
                    if best_width is None or width < best_width:
                        best_width = width
                        best_iv = cand_iv
                        if width == Fraction(0):
                            return best_iv  # eliminated entirely
            elif result is None:
                # Infeasible — overlap eliminated.
                return cand_iv

            u += res
        l += res

    return best_iv


def compute_repair_for_certificate(
    tpn: TPN,
    cert: Certificate,
    H: float,
    candidate_gate=None,
) -> Optional[List[Tuple[str, Interval]]]:
    """Compute minimal interval modifications that eliminate one certificate's overlap.

    Priority order:
      P1  Increase l_t for transitions in sigma_F but not sigma_N (T_delta_F).
      P2  Tighten interval for shared adjustable transitions (T_cap).
      P3  Decrease u_t for transitions in sigma_N but not sigma_F (T_delta_N).
      P4  Joint: tighten T_cap + increase l_t for T_delta_F simultaneously.

    *candidate_gate* (R3-2a): every return point gates the full candidate
    assignment before it is proposed, and P2 sweeps / P4 combinations gate
    each intermediate point.  A gate-rejected proposal is skipped (fall
    through to the next transition / candidate), so the LP search only
    ever proposes candidates inside the task domain.  When P1's minimal
    separating point is a singleton [l, l] with l > u0 and is rejected,
    the monotone-continuation probe at the far end of the authorized
    domain (l_max) is tried — the singleton family is not guaranteed to
    be monotone, and the far end may be task-acceptable where the minimal
    point is not.

    Returns [(t_name, new_interval), ...] on success, or None.
    """
    sigma_N = cert.sigma_N
    sigma_F = cert.sigma_F

    # Quick check: does overlap still exist?
    if _certificate_overlap_exists(tpn, sigma_N, sigma_F, H) is False:
        return None  # conclusively no overlap — nothing to repair

    # Partition adjustable transitions by occurrence pattern.
    t_delta_F: List[str] = []  # appears only in sigma_F (or more often there)
    t_delta_N: List[str] = []  # appears only in sigma_N (or more often there)
    t_cap: List[str] = []      # appears equally in both

    for t_name, t in tpn.transitions.items():
        if not t.is_adjustable:
            continue
        if t.authorized_range is None:
            continue
        n_N = sigma_N.count(t_name)
        n_F = sigma_F.count(t_name)
        if n_F > n_N:
            t_delta_F.append(t_name)
        elif n_N > n_F:
            t_delta_N.append(t_name)
        elif n_N == n_F and n_N > 0:
            t_cap.append(t_name)

    # Sort T_cap by current interval width (descending).  Tightening a
    # wide interval eliminates more shared variance per step, so it is
    # tried first — fewer LP evaluations and faster convergence.
    t_cap.sort(
        key=lambda tn: tpn.transitions[tn].interval.latest
        - tpn.transitions[tn].interval.earliest,
        reverse=True,
    )

    # P1: increase lower bound of T_delta_F transitions.
    for t_name in t_delta_F:
        t = tpn.transitions[t_name]
        ar = t.authorized_range
        best_l = find_separating_lower_bound(
            tpn, sigma_N, sigma_F, H, t_name, ar,
        )
        if best_l is not None:
            new_iv = Interval(best_l, max(best_l, t.interval.latest))
            _assert_tightening_direction("P1", t.interval, new_iv)
            if _proposal_passes(
                candidate_gate,
                tpn.copy_with_interval(t_name, new_iv),
                ProposalContext(
                    lp_stage="P1",
                    path="lp",
                    changes=((t_name, str(t.interval), str(new_iv)),),
                ),
            ):
                return [(t_name, new_iv)]
            # Gate-rejected minimal point.  If it is a singleton
            # [l, l] with l > u0, probe the far end of the authorized
            # domain (monotone-continuation contract): the singleton
            # family is not guaranteed monotone, and the far end may be
            # task-acceptable.
            if best_l > t.interval.latest:
                probe_iv = Interval(ar.l_max, ar.l_max)
                _assert_tightening_direction("P1", t.interval, probe_iv)
                if _proposal_passes(
                    candidate_gate,
                    tpn.copy_with_interval(t_name, probe_iv),
                    ProposalContext(
                        lp_stage="P1",
                        path="lp",
                        changes=((t_name, str(t.interval), str(probe_iv)),),
                    ),
                ):
                    return [(t_name, probe_iv)]
            # else: fall through to the next T_delta_F transition.

    # P2: tighten shared adjustable transitions.
    best_tightening: Optional[Tuple[str, Interval]] = None
    best_tightening_width: Optional[Fraction] = None
    for t_name in t_cap:
        t = tpn.transitions[t_name]
        ar = t.authorized_range
        best_iv = find_tightening_interval(
            tpn, sigma_N, sigma_F, H, t_name, ar, candidate_gate,
        )
        if best_iv is not None and best_iv != t.interval:
            # best_iv already passed the gate (per-point gating inside the
            # sweep); the same candidate is re-evaluated here for the
            # elimination check below.
            test_tpn = tpn.copy_with_interval(t_name, best_iv)
            if _certificate_overlap_exists(test_tpn, sigma_N, sigma_F, H) is False:
                return [(t_name, best_iv)]
            # Track best partial improvement.
            result = confirm_ambiguity(test_tpn, sigma_N, sigma_F, H)
            if result is not None and result.get("status") == "FEASIBLE":
                overlap = result.get("overlap")
                if overlap is not None:
                    w = Fraction(overlap[1]) - Fraction(overlap[0])
                    w = max(Fraction(0), w)
                    if best_tightening_width is None or w < best_tightening_width:
                        best_tightening_width = w
                        best_tightening = (t_name, best_iv)

    # P3: decrease upper bound of T_delta_N transitions.
    for t_name in t_delta_N:
        t = tpn.transitions[t_name]
        ar = t.authorized_range
        orig_iv = t.interval
        for u_cand in [ar.u_min,
                       Fraction((orig_iv.latest + ar.u_min) / 2).limit_denominator(1000),
                       orig_iv.latest]:
            cand_u = max(ar.u_min, min(ar.u_max, u_cand))
            cand_iv = Interval(max(orig_iv.earliest, ar.l_min), cand_u)
            if cand_iv == orig_iv:
                continue
            test_tpn = tpn.copy_with_interval(t_name, cand_iv)
            _assert_tightening_direction("P3", orig_iv, cand_iv)
            if not _proposal_passes(
                candidate_gate,
                test_tpn,
                ProposalContext(
                    lp_stage="P3",
                    path="lp",
                    changes=((t_name, str(orig_iv), str(cand_iv)),),
                ),
            ):
                continue
            if _certificate_overlap_exists(test_tpn, sigma_N, sigma_F, H) is False:
                return [(t_name, cand_iv)]

    # P4: joint search — tighten T_cap THEN increase T_delta_F lower bound.
    # When neither single-parameter approach eliminates overlap, the
    # combination often does (tightening reduces shared variance so a
    # smaller l_recov increase suffices).
    if best_tightening is not None and t_delta_F:
        mid_tpn = tpn.copy_with_interval(best_tightening[0], best_tightening[1])
        for t_name in t_delta_F:
            t = mid_tpn.transitions[t_name]
            ar = t.authorized_range
            best_l = find_separating_lower_bound(
                mid_tpn, sigma_N, sigma_F, H, t_name, ar,
            )
            if best_l is not None:
                new_iv = Interval(best_l, max(best_l, t.interval.latest))
                _assert_tightening_direction("P1", t.interval, new_iv)
                combined = mid_tpn.copy_with_interval(t_name, new_iv)
                # The combined assignment is gated as a whole (the task
                # constraint sees the full candidate model, not the parts).
                if _proposal_passes(
                    candidate_gate,
                    combined,
                    ProposalContext(
                        lp_stage="P4",
                        path="lp",
                        changes=(
                            (
                                best_tightening[0],
                                str(tpn.transitions[best_tightening[0]].interval),
                                str(best_tightening[1]),
                            ),
                            (t_name, str(t.interval), str(new_iv)),
                        ),
                    ),
                ):
                    return [
                        best_tightening,
                        (t_name, new_iv),
                    ]

    # P4 fallback: accept best single-parameter partial improvement
    # (already gate-accepted during the P2 sweep).
    if best_tightening is not None:
        return [best_tightening]

    return None


def try_lp_repair(
    tpn: TPN,
    current_certs: List[Certificate],
    selected_cert: Certificate,
    H: float,
    verifier_fn: Callable[[TPN], Dict],
    original_tpn: TPN,
    current_obj: Tuple,
    cache: Dict,
    candidate_gate=None,
    objective_variant: str = "O0",
    counters: Optional[Dict] = None,
) -> Optional[Tuple[TPN, List[Certificate], Tuple]]:
    """One LP-guided repair attempt for a single certificate.

    Computes an LP-optimal interval modification for *selected_cert*,
    applies it, and globally re-verifies.  The candidate is accepted
    only when the lexicographic objective strictly improves.

    *candidate_gate* (R3-2a): passed to the internal search so every
    proposal (P1-P4, per-point and per-combination) is constrained to
    the task domain before it can be selected.

    *objective_variant* (R3-5B): passed through to the objective projection
    used for the lexicographic comparison; it changes only comparison
    semantics, never the LP candidate search.

    Returns (repaired_tpn, remaining_certs, new_objective) or None.
    """
    modifications = compute_repair_for_certificate(
        tpn, selected_cert, H, candidate_gate,
    )
    if modifications is None:
        return None

    # Apply modifications.
    candidate = tpn
    for t_name, new_iv in modifications:
        candidate = candidate.copy_with_interval(t_name, new_iv)

    # Cache check.
    ckey = frozenset(
        (name, tr.interval.earliest, tr.interval.latest)
        for name, tr in candidate.transitions.items()
    )
    if ckey in cache:
        if counters is not None:
            counters["verifier_cache_hits"] += 1
        analysis = cache[ckey]
    else:
        if counters is not None:
            counters["verifier_evaluations"] += 1
        analysis = verifier_fn(candidate)
        if counters is not None:
            counters["verification_lp_calls"] += (
                analysis.get("stats", {}).get("lp_calls", 0)
            )
        cache[ckey] = analysis

    if analysis.get("result") == "INC":
        return None
    if analysis.get("enumeration_complete", True) is False:
        return None

    cand_certs = analysis.get("certificates", [])
    cand_obj = lexicographic_objective(
        candidate, cand_certs, original_tpn, objective_variant
    )

    if _lex_less(cand_obj, current_obj):
        return (candidate, cand_certs, cand_obj)

    return None
