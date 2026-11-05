"""
Certificate-scoped exhaustive timing repair.

Rationale
---------
The greedy repair (``algorithm.repair``) modifies one transition interval at a
time and, as a fallback, one pair. It is neither complete nor globally optimal
within the authorized interval domain. A full enumeration over every adjustable
transition is intractable: with up to 18 adjustable transitions and 5
authorized intervals each, the assignment space reaches 5**18.

However, the completion-time overlap of an ambiguity certificate
kappa = (phi, sigma_N, sigma_F, overlap) depends only on the timing of the
transitions that occur in sigma_N or sigma_F. Adjustable transitions that do
not appear in any certificate sequence cannot change any certificate's overlap.
Restricting the enumeration to

    T_cert = ( union over certs of set(sigma_N) | set(sigma_F) ) & T_adj

captures every transition whose interval can influence the confirmed
ambiguities, while shrinking the assignment space to a tractable size
(typically 5**2 .. 5**4 on the RMT-FMS benchmark).

This backend performs a complete enumeration of the Cartesian product of
authorized intervals over T_cert. Candidates are visited in nondecreasing
lexicographic change cost (C_I, m_I); because a successful repair returns a VER
model (no certificates, zero overlap width), the first VER-verified candidate in
this order is the lexicographically minimal repair.

Completeness is stated relative to T_cert: the search is complete for repairs
that modify only certificate-relevant transitions. Every accepted repair is
independently re-verified, so soundness is unconditional. Exhausting T_cert
without a VER candidate yields a 'UNSAT' solve status, distinguishing a scoped
infeasibility from the greedy backend's implicit FAILURE.
"""
from __future__ import annotations

import itertools
import math
from fractions import Fraction
from typing import Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from ..model.tpn import Interval, TPN
from .algorithm import (
    Certificate,
    RepairResult,
    _find_modified,
    _make_gate,
    lexicographic_objective,
)
from .task_constraint import (
    AlwaysTrueTaskConstraint,
    LegacyPredicateTaskConstraint,
    ProposalContext,
    ReferenceBehaviorSet,
    TaskAudit,
    TaskConstraint,
    task_candidate_id,
)


def _certificate_scope(tpn: TPN, certificates: List[Certificate]) -> List[str]:
    """T_cert: adjustable transitions occurring in any certificate sequence."""
    occurring: Set[str] = set()
    for cert in certificates:
        occurring.update(cert.sigma_N)
        occurring.update(cert.sigma_F)
    scoped = [
        name
        for name in occurring
        if name in tpn.transitions and tpn.transitions[name].is_adjustable
    ]
    return sorted(scoped)


def _change_cost(
    scoped: List[str],
    combo: Tuple[Interval, ...],
    current: Dict[str, Interval],
) -> Tuple[Fraction, int]:
    """(C_I, m_I) for one scoped assignment relative to the current intervals.

    Mirrors the endpoint-difference cost used by
    ``algorithm.lexicographic_objective`` so both backends rank repairs on the
    same scale.
    """
    m_i = 0
    c_i = Fraction(0)
    for name, iv in zip(scoped, combo):
        orig = current[name]
        if iv == orig:
            continue
        m_i += 1
        cost = abs(Fraction(iv.earliest) - Fraction(orig.earliest))
        if not math.isinf(iv.latest) and not math.isinf(orig.latest):
            cost += abs(Fraction(iv.latest) - Fraction(orig.latest))
        elif math.isinf(iv.latest) != math.isinf(orig.latest):
            cost += Fraction(10**6)
        c_i += cost
    return c_i, m_i


def exhaustive_repair(
    tpn: TPN,
    certificates: List[Certificate],
    L: int,
    H: float,
    task_predicate: Optional[Callable[[TPN], bool]] = None,
    verifier_fn: Callable[[TPN], Dict] = None,
    cache: Optional[Dict] = None,
    max_scope_combinations: int = 200_000,
    *,
    task_constraint: Optional[TaskConstraint] = None,
    reference_model: Optional[TPN] = None,
    metadata: Optional[ReferenceBehaviorSet] = None,
) -> RepairResult:
    """Certificate-scoped exhaustive timing repair.

    Enumerates the full Cartesian product of authorized intervals over the
    certificate-relevant adjustable transitions and returns the
    lexicographically minimal assignment under which independent re-verification
    yields VER.

    Args mirror ``algorithm.repair`` (including the R3-2a task_constraint /
    metadata interface, applied with the same audited gate);
    ``max_scope_combinations`` bounds the enumeration so a pathological scope
    reports BUDGET_EXCEEDED rather than running unboundedly.

    solve_status values: 'SAT' (repair found and re-verified), 'UNSAT' (scope
    exhausted conclusively, no repair), 'INCONCLUSIVE' (some candidate verdict
    was inconclusive, so UNSAT cannot be claimed), 'BUDGET_EXCEEDED' (scope
    larger than the enumeration bound).
    """
    if cache is None:
        cache = {}

    if task_constraint is not None and task_predicate is not None:
        raise ValueError(
            "pass either task_constraint or task_predicate, not both"
        )
    if task_constraint is None:
        if task_predicate is not None:
            task_constraint = LegacyPredicateTaskConstraint(task_predicate)
        else:
            task_constraint = AlwaysTrueTaskConstraint()
    if reference_model is None:
        reference_model = tpn

    audit = TaskAudit(
        constraint_name=task_constraint.name,
        reference_snapshot={
            "model_id": metadata.model_id if metadata is not None else None,
            "reference_fingerprint": (
                metadata.reference_fingerprint if metadata is not None else None
            ),
            "L": metadata.L if metadata is not None else None,
            "H": metadata.H if metadata is not None else None,
        },
    )

    original_tpn = tpn

    def _cache_key(t: TPN) -> FrozenSet:
        return frozenset(
            (name, tr.interval.earliest, tr.interval.latest)
            for name, tr in t.transitions.items()
        )

    # Already diagnosable: nothing to repair.
    if not certificates:
        return RepairResult(
            status="SUCCESS",
            repaired_tpn=original_tpn,
            iterations=0,
            modified_transitions=[],
            repair_backend="exhaustive",
            candidates_evaluated=0,
            solve_status="SAT",
            scoped_transitions=[],
            scope_size=1,
            task_audit=audit.to_dict(),
        )

    scoped = _certificate_scope(tpn, certificates)

    # No adjustable transition can influence the confirmed certificates.
    if not scoped:
        return RepairResult(
            status="FAILURE",
            repaired_tpn=None,
            iterations=0,
            modified_transitions=[],
            repair_backend="exhaustive",
            candidates_evaluated=0,
            solve_status="UNSAT",
            scoped_transitions=[],
            scope_size=0,
            task_audit=audit.to_dict(),
        )

    interval_choices = [
        tpn.transitions[name].admissible_intervals for name in scoped
    ]
    scope_size = 1
    for choices in interval_choices:
        scope_size *= max(1, len(choices))

    if scope_size > max_scope_combinations:
        return RepairResult(
            status="INCONCLUSIVE",
            repaired_tpn=None,
            iterations=0,
            modified_transitions=[],
            repair_backend="exhaustive",
            candidates_evaluated=0,
            solve_status="BUDGET_EXCEEDED",
            scoped_transitions=scoped,
            scope_size=scope_size,
            task_audit=audit.to_dict(),
        )

    current_intervals = {name: tpn.transitions[name].interval for name in scoped}

    # Rank every modified assignment by nondecreasing change cost. The overlap
    # width and certificate count are zero for any VER assignment, so ordering
    # on (C_I, m_I) makes the first VER candidate the lex-minimal repair.
    ranked: List[Tuple[Fraction, int, Tuple[Interval, ...]]] = []
    for combo in itertools.product(*interval_choices):
        c_i, m_i = _change_cost(scoped, combo, current_intervals)
        if m_i == 0:
            continue  # skip the unmodified TPN
        ranked.append((c_i, m_i, combo))
    ranked.sort(key=lambda x: (x[0], x[1]))

    evaluated = 0
    had_inconclusive = False
    best_tpn: Optional[TPN] = None
    best_candidate_id: Optional[str] = None

    # One gate for the whole enumeration; candidate_id distinguishes entries.
    gate = _make_gate(
        task_constraint, audit, reference_model, metadata,
        "exhaustive", lexicographic_objective(tpn, certificates, tpn),
    )

    for _c_i, _m_i, combo in ranked:
        candidate_tpn = tpn
        changes: List[Tuple[str, str, str]] = []
        for name, iv in zip(scoped, combo):
            if iv != current_intervals[name]:
                candidate_tpn = candidate_tpn.copy_with_interval(name, iv)
                changes.append((name, str(current_intervals[name]), str(iv)))

        # Task gate (audited); rejected candidates are never verified.
        gate_res = gate(
            candidate_tpn,
            ProposalContext(path="exhaustive", lp_stage="", changes=tuple(changes)),
        )
        if not gate_res.passed:
            continue

        ckey = _cache_key(candidate_tpn)
        cached = ckey in cache
        if cached:
            analysis = cache[ckey]
        else:
            analysis = verifier_fn(candidate_tpn)
            cache[ckey] = analysis
        evaluated += 1
        audit.patch(
            "exhaustive",
            task_candidate_id(candidate_tpn, metadata),
            verification_status=("CACHED" if cached else "VERIFIED"),
        )

        result = analysis.get("result")
        if result == "INC":
            had_inconclusive = True
            continue
        if analysis.get("enumeration_complete", True) is False:
            had_inconclusive = True
            continue
        if result == "VER":
            best_tpn = candidate_tpn
            best_candidate_id = task_candidate_id(candidate_tpn, metadata)
            break  # ordering guarantees the lex-minimal repair

    if best_tpn is None:
        status = "INCONCLUSIVE" if had_inconclusive else "FAILURE"
        solve = "INCONCLUSIVE" if had_inconclusive else "UNSAT"
        return RepairResult(
            status=status,
            repaired_tpn=None,
            iterations=evaluated,
            modified_transitions=[],
            repair_backend="exhaustive",
            candidates_evaluated=evaluated,
            solve_status=solve,
            scoped_transitions=scoped,
            scope_size=scope_size,
            task_audit=audit.to_dict(),
        )

    # Independent re-verification of the selected repair (soundness gate).
    independent = verifier_fn(best_tpn)
    independent_verdict = independent.get("result")
    if independent_verdict != "VER":
        status = "INCONCLUSIVE" if independent_verdict == "INC" else "FAILURE"
        return RepairResult(
            status=status,
            repaired_tpn=None,
            iterations=evaluated,
            modified_transitions=_find_modified(original_tpn, best_tpn),
            independent_final_verdict=independent_verdict,
            repair_backend="exhaustive",
            candidates_evaluated=evaluated,
            solve_status="SAT",
            scoped_transitions=scoped,
            scope_size=scope_size,
            task_audit=audit.to_dict(),
        )

    # Final task recheck on the accepted model (belt-and-suspenders).
    final_res = task_constraint.evaluate(best_tpn, reference_model, metadata)
    audit.record(
        "final",
        task_candidate_id(best_tpn, metadata),
        "final",
        "",
        (),
        final_res,
        None,
    )
    if not final_res.passed:
        return RepairResult(
            status="FAILURE",
            repaired_tpn=None,
            iterations=evaluated,
            modified_transitions=_find_modified(original_tpn, best_tpn),
            independent_final_verdict=independent_verdict,
            repair_backend="exhaustive",
            candidates_evaluated=evaluated,
            solve_status="SAT",
            scoped_transitions=scoped,
            scope_size=scope_size,
            task_audit=audit.to_dict(),
            failure_reason="TASK_PRESERVATION_VIOLATION",
        )
    if best_candidate_id is not None:
        audit.patch("exhaustive", best_candidate_id, disposition="adopted")

    return RepairResult(
        status="SUCCESS",
        repaired_tpn=best_tpn,
        iterations=evaluated,
        modified_transitions=_find_modified(original_tpn, best_tpn),
        independent_final_verdict=independent_verdict,
        repair_backend="exhaustive",
        candidates_evaluated=evaluated,
        solve_status="SAT",
        scoped_transitions=scoped,
        scope_size=scope_size,
        task_audit=audit.to_dict(),
    )
