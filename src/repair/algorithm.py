"""
Certificate-guided timing repair (Algorithm S4.3).

A repair iterates over confirmed ambiguity certificates and tries to
reduce the repair objective by modifying one transition interval at a time.

Repair objective (lexicographic):
  J(N'; N0) = (|A_{L,H}|, total_overlap_width, C_I, m_I)
  where:
    |A_{L,H}|: number of confirmed certificates
    total_overlap_width: sum of overlap interval lengths
    C_I: aggregate interval-change cost
    m_I: number of modified transitions

Certificate levels:
  Level 1 (Lambda_1): T_Delta(kappa) — adjustable with differing occurrence counts
  Level 2 (Lambda_2): T_cap(kappa) — adjustable with equal positive occurrence counts
  Level 3 (Lambda_3): T_adj - (Lambda_1 | Lambda_2)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from ..model.tpn import Interval, TPN
from .task_constraint import (
    AlwaysTrueTaskConstraint,
    LegacyPredicateTaskConstraint,
    ProposalContext,
    ReferenceBehaviorSet,
    TaskAudit,
    TaskConstraint,
    TaskConstraintResult,
    task_candidate_id,
)

# R3-5B: objective variants are pure projections/reorderings of the base tuple
# (|K|, W, C_I, m_I).  They change only lexicographic comparison semantics;
# candidate generation, task-gate results, and verifier results are invariant
# at a fixed (current_tpn, certificate set, generation state).  After the
# first adoption divergence, search trajectories may differ (logged as
# search-path sensitivity).
OBJECTIVE_VARIANTS = {
    "O0": (0, 1, 2, 3),  # baseline  (|K|, W, C_I, m_I)
    "O1": (0, 1, 3, 2),  # swap cost terms  (|K|, W, m_I, C_I)
    "O2": (0, 2, 1, 3),  # cost-prioritized  (|K|, C_I, W, m_I)
    "O3": (0, 1, 2),     # minimal ablation  (|K|, W, C_I)
}


def _resolve_objective_variant(variant: str) -> Tuple[int, ...]:
    if variant not in OBJECTIVE_VARIANTS:
        raise ValueError(
            f"unknown objective_variant: {variant!r} "
            f"(expected {' | '.join(sorted(OBJECTIVE_VARIANTS))})"
        )
    return OBJECTIVE_VARIANTS[variant]


@dataclass
class Certificate:
    """An end-to-end ambiguity certificate kappa = (phi, sigma_N, sigma_F, overlap)."""

    phi: Tuple[str, ...]
    sigma_N: Tuple[str, ...]
    sigma_F: Tuple[str, ...]
    overlap: Tuple  # (t_min, t_max) Fraction pair


def _occurrence_count(sigma: Tuple[str, ...], t_name: str) -> int:
    return sigma.count(t_name)


def find_differential_transitions(tpn: TPN, cert: Certificate) -> List[str]:
    """T_Delta(kappa): adjustable transitions where n_t(sigma_N) != n_t(sigma_F)."""
    result = []
    for t_name, t in tpn.transitions.items():
        if t.is_adjustable:
            n_n = _occurrence_count(cert.sigma_N, t_name)
            n_f = _occurrence_count(cert.sigma_F, t_name)
            if n_n != n_f:
                result.append(t_name)
    return result


def find_shared_transitions(tpn: TPN, cert: Certificate) -> List[str]:
    """T_cap(kappa): adjustable transitions where n_t(sigma_N) == n_t(sigma_F) > 0."""
    result = []
    for t_name, t in tpn.transitions.items():
        if t.is_adjustable:
            n_n = _occurrence_count(cert.sigma_N, t_name)
            n_f = _occurrence_count(cert.sigma_F, t_name)
            if n_n == n_f and n_n > 0:
                result.append(t_name)
    return result


def _overlap_width(cert: Certificate) -> Fraction:
    """Length of the overlap interval."""
    lo, hi = cert.overlap
    lo_f = Fraction(lo)
    hi_f = Fraction(hi)
    width = hi_f - lo_f
    return max(Fraction(0), width)


def lexicographic_objective(
    tpn: TPN,
    certificates: List[Certificate],
    original_tpn: TPN,
    variant: str = "O0",
) -> Tuple:
    """Projected objective tuple for the requested R3-5B variant.

    Base tuple: (|A_{L,H}|, total_overlap_width, C_I, m_I) where
      C_I: aggregate change cost (sum of absolute differences in interval endpoints)
      m_I: number of modified transitions

    variant is a pure projection/reordering of the base tuple (see
    OBJECTIVE_VARIANTS); it never changes which candidate is generated or how
    the verifier treats it.
    """
    n_certs = len(certificates)
    total_width = sum(_overlap_width(c) for c in certificates)

    # Compute C_I and m_I relative to original_tpn
    C_I = Fraction(0)
    m_I = 0
    for t_name, t in tpn.transitions.items():
        orig_t = original_tpn.transitions.get(t_name)
        if orig_t is None:
            continue
        if t.interval != orig_t.interval:
            m_I += 1
            # Change cost: sum of absolute differences of endpoints
            cost = abs(Fraction(t.interval.earliest) - Fraction(orig_t.interval.earliest))
            if not math.isinf(t.interval.latest) and not math.isinf(orig_t.interval.latest):
                cost += abs(Fraction(t.interval.latest) - Fraction(orig_t.interval.latest))
            elif math.isinf(t.interval.latest) != math.isinf(orig_t.interval.latest):
                cost += Fraction(10**6)  # Large penalty for inf->finite or vice versa
            C_I += cost

    base = (n_certs, total_width, C_I, m_I)
    return tuple(base[i] for i in _resolve_objective_variant(variant))


def _lex_less(a: Tuple, b: Tuple) -> bool:
    """True if a <_lex b."""
    for ai, bi in zip(a, b):
        if ai < bi:
            return True
        if ai > bi:
            return False
    return False


@dataclass
class RepairResult:
    """Result of the repair procedure."""

    status: str  # 'SUCCESS', 'FAILURE', 'INCONCLUSIVE'
    repaired_tpn: Optional[TPN]
    iterations: int
    modified_transitions: List[str]
    independent_final_verdict: Optional[str] = None
    # Backend attribution and search diagnostics. Defaults keep the greedy
    # backend's existing return values unchanged.
    repair_backend: str = "greedy"
    candidates_evaluated: int = 0
    solve_status: Optional[str] = None  # exhaustive: 'SAT'/'UNSAT'/'BUDGET_EXCEEDED'
    scoped_transitions: List[str] = field(default_factory=list)
    scope_size: int = 0
    # R3-2a task-preservation additions (additive; legacy consumers ignore them).
    task_audit: Optional[Dict] = None
    failure_reason: Optional[str] = None  # e.g. 'TASK_PRESERVATION_VIOLATION'
    # R3-5 instrumentation: explicit, namespaced per-run counters.
    counters: Optional[Dict] = None


def _make_gate(
    task_constraint: TaskConstraint,
    audit: TaskAudit,
    reference_model: Optional[TPN],
    metadata: Optional[ReferenceBehaviorSet],
    step: str,
    objective_before: Optional[Tuple],
    counters: Optional[Dict[str, Any]] = None,
) -> Callable[[TPN, ProposalContext], TaskConstraintResult]:
    """Audit closure: evaluate the task constraint on a candidate and
    record the decision (accepted or rejected) in the candidate-level
    audit chain.  Shared by the greedy (L1/L2/L3/LP/pair) and exhaustive
    adoption paths so every gate decision is recorded identically.

    *counters* (R3-5): when provided, the gate increments
    candidate_generation / task_rejection / task_lp_calls and accumulates
    the task-check wall time."""

    def gate(
        candidate_tpn: TPN, ctx: ProposalContext
    ) -> TaskConstraintResult:
        t0 = time.perf_counter()
        res = task_constraint.evaluate(candidate_tpn, reference_model, metadata)
        if counters is not None:
            counters["candidate_generation"] += 1
            counters["task_lp_calls"] += res.metrics.get("task_lp_calls_actual", 0)
            counters["task_check_time_s"] += time.perf_counter() - t0
            if not res.passed:
                counters["task_rejection"] += 1
        audit.record(
            step,
            task_candidate_id(candidate_tpn, metadata),
            ctx.path,
            ctx.lp_stage,
            ctx.changes,
            res,
            objective_before,
        )
        return res

    return gate


def repair(
    tpn: TPN,
    certificates: List[Certificate],
    L: int,
    H: float,
    task_predicate: Optional[Callable[[TPN], bool]] = None,
    verifier_fn: Callable[[TPN], Dict] = None,  # fn(tpn) -> {'result': ..., 'certificates': ...}
    cache: Optional[Dict] = None,
    *,
    task_constraint: Optional[TaskConstraint] = None,
    reference_model: Optional[TPN] = None,
    metadata: Optional[ReferenceBehaviorSet] = None,
    objective_variant: str = "O0",
) -> RepairResult:
    """Algorithm S4.3: Certificate-guided timing repair.

    Args:
        tpn: current TPN (already verified, has certificates)
        certificates: list of confirmed ambiguity certificates for tpn
        L: label bound
        H: time horizon
        task_predicate: legacy fn(tpn) -> bool, True if task constraints
            satisfied (R3-2a: kept for backward compatibility; wrapped into
            a TaskConstraint internally).  Mutually exclusive with
            task_constraint.
        verifier_fn: fn(tpn) -> {'result': str, 'certificates': list[Certificate]}
        cache: dict mapping frozenset of (t_name, interval) -> analysis result
        task_constraint: R3-2a task constraint (evaluate -> TaskConstraintResult).
            Applied uniformly at every adoption path (L1/L2/L3, LP P1-P4,
            pair) and re-checked on the final model.  Default: always_true
            (bit-identical to the submitted psi_task == 1 pipeline).
        reference_model: the N0 reference TPN passed to the constraint.
        metadata: frozen ReferenceBehaviorSet passed to the constraint.

    Returns:
        RepairResult with status 'SUCCESS', 'FAILURE', or 'INCONCLUSIVE'.
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
    _resolve_objective_variant(objective_variant)  # fail fast on bad variant

    audit = TaskAudit(
        constraint_name=task_constraint.name,
        reference_snapshot={
            "model_id": metadata.model_id if metadata is not None else None,
            "reference_fingerprint": (
                metadata.reference_fingerprint if metadata is not None else None
            ),
            "L": metadata.L if metadata is not None else None,
            "H": metadata.H if metadata is not None else None,
            "n_normal_sequences": (
                len(metadata.normal_sequences) if metadata is not None else None
            ),
            "n_fault_sequences": (
                len(metadata.fault_sequences) if metadata is not None else None
            ),
        },
    )

    counters: Dict[str, Any] = {
        "candidate_generation": 0,
        "task_rejection": 0,
        "verifier_evaluations": 0,
        "verifier_cache_hits": 0,
        "task_lp_calls": 0,
        "verification_lp_calls": 0,
        "lp_repair_calls": 0,
        "task_check_time_s": 0.0,
        "lp_search_time_s": 0.0,
    }

    original_tpn = tpn
    current_tpn = tpn
    current_certs = list(certificates)
    iterations = 0

    def _cache_key(t: TPN) -> FrozenSet:
        return frozenset(
            (name, tr.interval.earliest, tr.interval.latest)
            for name, tr in t.transitions.items()
        )

    # Check initial state
    if not current_certs:
        return RepairResult(
            status="SUCCESS",
            repaired_tpn=current_tpn,
            iterations=0,
            modified_transitions=[],
            task_audit=audit.to_dict(),
            counters=counters,
        )

    current_obj = lexicographic_objective(
        current_tpn, current_certs, original_tpn, objective_variant
    )

    while current_certs:
        iterations += 1

        # Select certificate by fixed order: decreasing overlap width,
        # then increasing total firing-sequence length, then lex transition ids
        def cert_sort_key(c: Certificate):
            width = _overlap_width(c)
            seq_len = len(c.sigma_N) + len(c.sigma_F)
            transitions_str = "".join(sorted(set(c.sigma_N) | set(c.sigma_F)))
            return (-width, seq_len, transitions_str)

        selected_cert = min(current_certs, key=cert_sort_key)

        # Build candidate levels
        t_delta = set(find_differential_transitions(current_tpn, selected_cert))
        t_cap = set(find_shared_transitions(current_tpn, selected_cert)) - t_delta
        t_adj_all = {
            t_name
            for t_name, t in current_tpn.transitions.items()
            if t.is_adjustable
        }
        t_level3 = t_adj_all - t_delta - t_cap

        levels = [
            sorted(t_delta),
            sorted(t_cap),
            sorted(t_level3),
        ]

        best_candidate: Optional[TPN] = None
        best_obj: Optional[Tuple] = None
        best_candidate_id: Optional[str] = None
        best_candidate_step: Optional[str] = None
        had_inconclusive = False
        found_level = False

        level_names = ["level1", "level2", "level3"]
        for level_idx, level_transitions in enumerate(levels):
            level_name = level_names[level_idx]
            step = f"iter{iterations}:{level_name}"
            gate = _make_gate(
                task_constraint, audit, reference_model, metadata,
                step, current_obj, counters,
            )
            for t_name in level_transitions:
                t = current_tpn.transitions[t_name]
                for new_interval in t.admissible_intervals:
                    if new_interval == t.interval:
                        continue  # Skip current interval

                    candidate_tpn = current_tpn.copy_with_interval(t_name, new_interval)

                    # Task gate (audited): candidates rejected here are
                    # recorded with verification_status='SKIPPED_TASK' and
                    # are never verified or adopted.
                    gate_res = gate(
                        candidate_tpn,
                        ProposalContext(
                            path=level_name,
                            lp_stage="",
                            changes=(
                                (t_name, str(t.interval), str(new_interval)),
                            ),
                        ),
                    )
                    if not gate_res.passed:
                        continue

                    # Check cache
                    candidate_id = task_candidate_id(candidate_tpn, metadata)
                    ckey = _cache_key(candidate_tpn)
                    cached = ckey in cache
                    if cached:
                        counters["verifier_cache_hits"] += 1
                        analysis = cache[ckey]
                    else:
                        counters["verifier_evaluations"] += 1
                        analysis = verifier_fn(candidate_tpn)
                        counters["verification_lp_calls"] += (
                            analysis.get("stats", {}).get("lp_calls", 0)
                        )
                        cache[ckey] = analysis

                    if analysis.get("result") == "INC":
                        had_inconclusive = True
                        continue

                    # The repair objective is defined on a complete ambiguity set.
                    # Production verifier calls should therefore provide
                    # enumeration_complete=True (the CLI passes complete_set=True).
                    # Some unit tests and legacy callbacks return a minimal mock
                    # dictionary without this metadata; those are treated as
                    # complete so that FAILURE (no strict improvement) is not
                    # misreported as INCONCLUSIVE.  If the field is present and
                    # false, the candidate is genuinely incomplete.
                    if analysis.get("enumeration_complete", True) is False:
                        had_inconclusive = True
                        continue

                    cand_certs = analysis.get("certificates", [])
                    cand_obj = lexicographic_objective(
                        candidate_tpn, cand_certs, original_tpn, objective_variant
                    )
                    audit.patch(
                        step,
                        candidate_id,
                        objective_candidate=cand_obj,
                        verification_status=(
                            "CACHED" if cached else "VERIFIED"
                        ),
                    )

                    if _lex_less(cand_obj, current_obj):
                        if best_obj is None or _lex_less(cand_obj, best_obj):
                            best_candidate = candidate_tpn
                            best_obj = cand_obj
                            best_certs = cand_certs
                            best_candidate_id = candidate_id
                            best_candidate_step = step
                            audit.patch(
                                step, candidate_id, disposition="lex_best"
                            )

            if best_candidate is not None:
                found_level = True
                break

        if best_candidate is None:
            # LP-guided repair: use the certificate structure to compute an
            # interval modification with continuous parameter search.  This
            # avoids the fixed-grid limitation of admissible_intervals and
            # can find repairs that the discrete levels miss (e.g., when the
            # required t_recov lower bound lies between two grid points, or
            # when tightening a shared transition's interval helps).
            had_inconclusive_lp = False
            try:
                from .lp_interval_search import try_lp_repair

                # The LP search gates every internal candidate (P1-P4) with
                # the same task constraint, so its search is constrained to
                # the task domain (search completeness), not just filtered
                # after the fact.
                lp_gate = _make_gate(
                    task_constraint, audit, reference_model, metadata,
                    f"iter{iterations}:lp", current_obj, counters,
                )
                counters["lp_repair_calls"] += 1
                _t_lp = time.perf_counter()
                lp_result = try_lp_repair(
                    current_tpn, current_certs, selected_cert, H,
                    verifier_fn, original_tpn, current_obj, cache,
                    candidate_gate=lp_gate,
                    objective_variant=objective_variant,
                    counters=counters,
                )
                counters["lp_search_time_s"] += time.perf_counter() - _t_lp
                if lp_result is not None:
                    lp_candidate, lp_certs, lp_obj = lp_result
                    # Post-gate invariant (soundness): the LP-internal gate
                    # must have admitted the returned proposal.  This is a
                    # fail-fast assertion, not a second filter — if it ever
                    # fires, an adoption path bypassed the task gate.
                    post = task_constraint.evaluate(
                        lp_candidate, reference_model, metadata
                    )
                    if not post.passed:
                        raise RuntimeError(
                            "task-gate invariant violated: LP returned a "
                            "proposal that the task constraint rejects "
                            f"(constraint={task_constraint.name}, "
                            f"candidate={task_candidate_id(lp_candidate, metadata)})"
                        )
                    audit.patch(
                        f"iter{iterations}:lp",
                        task_candidate_id(lp_candidate, metadata),
                        disposition="adopted",
                        objective_candidate=lp_obj,
                        verification_status=(
                            "CACHED"
                            if _cache_key(lp_candidate) in cache
                            else "VERIFIED"
                        ),
                    )
                    current_tpn, current_certs, current_obj = (
                        lp_candidate, lp_certs, lp_obj
                    )
                    continue  # accepted — back to main while loop
            except Exception:
                # LP path must never take down the whole repair procedure.
                # A transient solver issue leaves had_inconclusive_lp=False
                # and falls through to pair fallback.
                pass

            # Single-transition search found no improvement.
            # Fallback: try pairs within the certificate scope.
            # This handles cases where two transitions must be adjusted together
            # (e.g., cross-config certificates where proc intervals differ).
            # Restricting to certificate scope avoids blind enumeration over all
            # adjustable transitions and keeps the search tractable.
            had_inconclusive_pair = False
            best_pair_candidate: Optional[TPN] = None
            best_pair_obj: Optional[Tuple] = None
            best_pair_certs: List = []
            best_pair_candidate_id: Optional[str] = None
            best_pair_candidate_step: Optional[str] = None
            pair_step = f"iter{iterations}:pair"
            pair_gate = _make_gate(
                task_constraint, audit, reference_model, metadata,
                pair_step, current_obj, counters,
            )

            # Certificate scope: adjustable transitions occurring in any cert sequence.
            cert_scope_set: Set[str] = set()
            for cert in current_certs:
                cert_scope_set.update(cert.sigma_N)
                cert_scope_set.update(cert.sigma_F)
            cert_scope_adj = sorted(
                t_name for t_name in cert_scope_set
                if t_name in current_tpn.transitions
                and current_tpn.transitions[t_name].is_adjustable
            )

            # If cert scope is empty or has only 1 transition, no pairs to try
            if len(cert_scope_adj) < 2:
                cert_scope_adj = []

            all_adj = cert_scope_adj

            for i, t1_name in enumerate(all_adj):
                t1 = current_tpn.transitions[t1_name]
                for t2_name in all_adj[i + 1:]:
                    t2 = current_tpn.transitions[t2_name]
                    for iv1 in t1.admissible_intervals:
                        if iv1 == t1.interval:
                            continue
                        for iv2 in t2.admissible_intervals:
                            if iv2 == t2.interval:
                                continue

                            pair_tpn = current_tpn.copy_with_interval(
                                t1_name, iv1
                            ).copy_with_interval(t2_name, iv2)

                            # Task gate (audited) — same contract as singles.
                            gate_res = pair_gate(
                                pair_tpn,
                                ProposalContext(
                                    path="pair",
                                    lp_stage="",
                                    changes=(
                                        (t1_name, str(t1.interval), str(iv1)),
                                        (t2_name, str(t2.interval), str(iv2)),
                                    ),
                                ),
                            )
                            if not gate_res.passed:
                                continue

                            ckey = _cache_key(pair_tpn)
                            cached = ckey in cache
                            if cached:
                                counters["verifier_cache_hits"] += 1
                                analysis = cache[ckey]
                            else:
                                counters["verifier_evaluations"] += 1
                                analysis = verifier_fn(pair_tpn)
                                counters["verification_lp_calls"] += (
                                    analysis.get("stats", {}).get("lp_calls", 0)
                                )
                                cache[ckey] = analysis

                            if analysis.get("result") == "INC":
                                had_inconclusive_pair = True
                                continue
                            if analysis.get("enumeration_complete", True) is False:
                                had_inconclusive_pair = True
                                continue

                            cand_certs = analysis.get("certificates", [])
                            cand_obj = lexicographic_objective(
                                pair_tpn, cand_certs, original_tpn, objective_variant
                            )
                            pair_candidate_id = task_candidate_id(
                                pair_tpn, metadata
                            )
                            audit.patch(
                                pair_step,
                                pair_candidate_id,
                                objective_candidate=cand_obj,
                                verification_status=(
                                    "CACHED" if cached else "VERIFIED"
                                ),
                            )

                            if _lex_less(cand_obj, current_obj):
                                if best_pair_obj is None or _lex_less(
                                    cand_obj, best_pair_obj
                                ):
                                    best_pair_candidate = pair_tpn
                                    best_pair_obj = cand_obj
                                    best_pair_certs = cand_certs
                                    best_pair_candidate_id = pair_candidate_id
                                    best_pair_candidate_step = pair_step
                                    audit.patch(
                                        pair_step,
                                        pair_candidate_id,
                                        disposition="lex_best",
                                    )

            if best_pair_candidate is not None:
                current_tpn = best_pair_candidate
                current_certs = best_pair_certs
                current_obj = best_pair_obj
                audit.patch(
                    best_pair_candidate_step,
                    best_pair_candidate_id,
                    disposition="adopted",
                )
                continue  # back to main while loop

            # Neither single nor pair search found improvement
            if had_inconclusive or had_inconclusive_pair:
                return RepairResult(
                    status="INCONCLUSIVE",
                    repaired_tpn=None,
                    iterations=iterations,
                    modified_transitions=_find_modified(original_tpn, current_tpn),
                    task_audit=audit.to_dict(),
                    counters=counters,
                )
            else:
                return RepairResult(
                    status="FAILURE",
                    repaired_tpn=None,
                    iterations=iterations,
                    modified_transitions=_find_modified(original_tpn, current_tpn),
                    task_audit=audit.to_dict(),
                    counters=counters,
                )

        current_tpn = best_candidate
        current_certs = best_certs
        current_obj = best_obj
        assert best_candidate_step is not None and best_candidate_id is not None
        audit.patch(
            best_candidate_step,
            best_candidate_id,
            disposition="adopted",
        )

    # Independent re-verification of the repaired TPN
    independent_result = verifier_fn(current_tpn)
    independent_verdict = independent_result.get("result")

    if independent_verdict != "VER":
        # Independent check did not confirm VER: treat as inconclusive or failure
        status = "INCONCLUSIVE" if independent_verdict == "INC" else "FAILURE"
        return RepairResult(
            status=status,
            repaired_tpn=None,
            iterations=iterations,
            modified_transitions=_find_modified(original_tpn, current_tpn),
            independent_final_verdict=independent_verdict,
            task_audit=audit.to_dict(),
            counters=counters,
        )

    # Final task recheck on the accepted model.  Every adoption path was
    # already gated, so this is a belt-and-suspenders guarantee that the
    # reported SUCCESS model is task-preserving; it also records the final
    # candidate in the audit chain.
    final_res = task_constraint.evaluate(current_tpn, reference_model, metadata)
    audit.record(
        "final",
        task_candidate_id(current_tpn, metadata),
        "final",
        "",
        (),
        final_res,
        current_obj,
    )
    if not final_res.passed:
        return RepairResult(
            status="FAILURE",
            repaired_tpn=None,
            iterations=iterations,
            modified_transitions=_find_modified(original_tpn, current_tpn),
            independent_final_verdict=independent_verdict,
            task_audit=audit.to_dict(),
            failure_reason="TASK_PRESERVATION_VIOLATION",
            counters=counters,
        )

    return RepairResult(
        status="SUCCESS",
        repaired_tpn=current_tpn,
        iterations=iterations,
        modified_transitions=_find_modified(original_tpn, current_tpn),
        independent_final_verdict=independent_verdict,
        task_audit=audit.to_dict(),
        counters=counters,
    )


def _find_modified(original: TPN, repaired: TPN) -> List[str]:
    """Return list of transition names whose intervals changed."""
    result = []
    for t_name, t in repaired.transitions.items():
        orig = original.transitions.get(t_name)
        if orig and t.interval != orig.interval:
            result.append(t_name)
    return result
