"""
Coquand-inspired global constraint synthesis repair baseline.

Based on Coquand, Pencolé, Subias (IFAC WC 2023):
"Diagnosabilization of Time Petri Net for Timed Fault."

Key idea (Coquand-style):
  For each AMB certificate, compute the minimal per-transition interval
  restrictions independently.  Collect ALL restrictions into a global
  constraint set.  If the union is mutually satisfiable (no conflicting
  requirements on the same transition), apply all at once and verify.

Contrast with our iterative greedy (Algorithm S4.3):
  - Coquand-style: one-shot global constraint collection; fails when
    certificates impose conflicting requirements on the same transition.
  - Our iterative: certificate-by-certificate resolution with re-verification
    after each step; handles conflicts by sequential refinement.
"""
from __future__ import annotations

import time
from fractions import Fraction
from typing import Dict, List, Optional, Tuple, Union

from ..model.tpn import Interval, TPN
from ..exact_confirmation.lp_solver import confirm_ambiguity
from .algorithm import Certificate
from .lp_interval_search import (
    _certificate_overlap_exists,
    compute_repair_for_certificate,
)

# Per-transition requirement: (required_l, required_u).
# req_u may be float('inf') for unbounded-interval transitions.
_Requirement = Tuple[Fraction, Union[Fraction, float]]


def _collect_global_constraints(
    tpn: TPN,
    certificates: List[Certificate],
    H: float,
) -> Optional[Dict[str, _Requirement]]:
    """Collect minimal per-certificate modifications into a global constraint set.

    Returns a dict {t_name: (required_l, required_u)} or None if any
    certificate cannot be independently resolved (insufficient authorized
    range) or the consolidated constraints are contradictory (required_l >
    required_u).

    Certificates whose overlap is already eliminated on the original TPN
    are silently skipped (they contribute zero constraints).
    """
    global_reqs: Dict[str, _Requirement] = {}

    for cert in certificates:
        # Skip certificates whose overlap is already gone on the original TPN.
        overlap_exists = _certificate_overlap_exists(
            tpn, cert.sigma_N, cert.sigma_F, H,
        )
        if overlap_exists is False:
            continue  # already eliminated — no constraints needed
        # overlap_exists is True or None (inconclusive → treat as unrepairable)

        modifications = compute_repair_for_certificate(tpn, cert, H)
        if modifications is None:
            # Certificate could not be independently resolved within
            # authorized ranges.  Global synthesis cannot help.
            return None

        for t_name, new_iv in modifications:
            req_l = new_iv.earliest
            req_u = new_iv.latest

            if t_name in global_reqs:
                cur_l, cur_u = global_reqs[t_name]
                req_l = max(cur_l, req_l)
                req_u = min(cur_u, req_u)
            global_reqs[t_name] = (req_l, req_u)

    if not global_reqs:
        # All certificates were already eliminated (should not happen for
        # AMB TPN, but guard against degenerate cases).
        return {}

    # Check for inter-certificate contradictions.
    for t_name, (req_l, req_u) in global_reqs.items():
        if req_l > req_u:
            return None

    return global_reqs


def global_constraint_repair(
    tpn: TPN,
    certificates: List[Certificate],
    H: float,
) -> Dict:
    """Attempt one-shot global constraint synthesis repair.

    Args:
        tpn: Original (AMB) TPN.
        certificates: All AMB certificates from complete_set verification.
        H: Observation horizon.

    Returns:
        Dict with keys: status, modified_tpn, modified_transitions,
        n_certificates_input, n_certificates_remaining, repair_time_s,
        failure_reason.
    """
    t_start = time.perf_counter()
    n_input = len(certificates)

    result = {
        "status": None,
        "modified_tpn": None,
        "modified_transitions": [],
        "n_certificates_input": n_input,
        "n_certificates_remaining": None,
        "repair_time_s": 0.0,
        "failure_reason": None,
    }

    if not certificates:
        result["status"] = "SUCCESS"
        result["n_certificates_remaining"] = 0
        result["repair_time_s"] = 0.0
        return result

    # Step 1: Collect global constraints.
    global_reqs = _collect_global_constraints(tpn, certificates, H)
    if global_reqs is None:
        result["status"] = "FAILURE"
        result["failure_reason"] = "INTER_CERT_CONFLICT"
        result["repair_time_s"] = round(time.perf_counter() - t_start, 4)
        return result

    # Step 2: Apply all consolidated modifications, clamping to authorized
    # ranges and original intervals (restriction-only principle).
    modified = tpn
    mod_list: List[Tuple[str, Interval]] = []
    for t_name, (req_l, req_u) in sorted(global_reqs.items()):
        t = modified.transitions[t_name]
        orig = t.interval

        # Clamp to authorized range.
        if t.authorized_range is not None:
            ar = t.authorized_range
            req_l = max(req_l, ar.l_min)
            req_u = min(req_u, ar.u_max)

        # Never lower the lower bound below original (firing window must not
        # start earlier — that would increase ambiguity, not reduce it).
        # NOTE: we deliberately do NOT clamp req_u to orig.latest here.
        # compute_repair_for_certificate may return an upward-shifted interval
        # (best_l > orig.latest) which forms a singleton [best_l, best_l] within
        # the authorized range.  Clamping req_u to orig.latest in that case would
        # produce req_l > req_u and block a valid repair.  The authorized range
        # check above (ar.u_max) is the correct upper-bound guard.
        req_l = max(req_l, orig.earliest)

        if req_l > req_u:
            result["status"] = "FAILURE"
            result["failure_reason"] = "AUTHORIZED_RANGE_CLIP"
            result["repair_time_s"] = round(time.perf_counter() - t_start, 4)
            return result

        new_iv = Interval(req_l, req_u)
        if new_iv != orig:
            modified = modified.copy_with_interval(t_name, new_iv)
            mod_list.append((t_name, new_iv))

    result["modified_transitions"] = [t_name for t_name, _ in mod_list]
    result["modified_tpn"] = modified

    # Step 3: Check if all input certificates are eliminated.
    remaining = 0
    for cert in certificates:
        overlap = _certificate_overlap_exists(
            modified, cert.sigma_N, cert.sigma_F, H,
        )
        if overlap is not False:
            remaining += 1

    result["n_certificates_remaining"] = remaining
    result["repair_time_s"] = round(time.perf_counter() - t_start, 4)

    if remaining == 0:
        result["status"] = "SUCCESS"
    else:
        result["status"] = "FAILURE"
        if result["failure_reason"] is None:
            result["failure_reason"] = "VERIFY_FAILED"

    return result
