"""
Task-preservation constraints for certificate-guided repair (R3-2a).

Motivation
----------
The submitted repair pipeline runs with a trivial task predicate
(``task_predicate=lambda t: True``, i.e. psi_task == 1 everywhere), so
the reported success rates are unconstrained repair success rates.  R3-2a
introduces a *task constraint* interface so a repair can be required to
preserve the reference model's critical behavior while eliminating
ambiguity certificates.

Design (frozen, Phase 2)
------------------------
* A task constraint is ``evaluate(candidate_model, reference_model,
  metadata) -> TaskConstraintResult``.  The gate is applied uniformly at
  every adoption path (L1/L2/L3 singles, LP search stages P1-P4, pair
  fallback, and a final recheck), so a candidate can never be adopted
  without passing the task gate (soundness), and LP-internal candidate
  selection is constrained to the task domain (search completeness).
* v1 constraint: ``ExactCriticalSequencePreservation``.  The reference
  behavior set is frozen from the initial (N0) complete-set AMB
  certificate enumeration.  Only the *exact critical sequences* — the
  sigma_N / sigma_F pairs that witness the confirmed ambiguities — are
  preserved, as timed firing sequences (feasibility of the path polytope
  under the horizon H).  Structure is never modified by repair (only
  intervals change), so preservation reduces to timed feasibility of each
  reference sequence in the candidate model.  This is deliberately the
  *smallest* preservation contract: it does not preserve all firing
  sequences, behavior families, or timing profiles of N0.
* ``AlwaysTrueTaskConstraint`` reproduces the legacy psi_task == 1
  behavior exactly; ``LegacyPredicateTaskConstraint`` wraps the old
  ``task_predicate: TPN -> bool`` callable so existing call sites (CLI,
  non-vacuity pilot) keep working unchanged.
* Every gate decision is recorded in a ``TaskAudit`` (candidate-level
  audit chain: candidate_id, changes, objective before, verification
  status SKIPPED_TASK/VERIFIED/CACHED, disposition).

Contracts (must not be violated)
--------------------------------
* Verifier semantics, FIRST_CERTIFICATE/COMPLETE_SET behavior, the joint
  LP formulation, and the CPG-LD lexicographic objective
  (|K|, W, C_I, m_I) are untouched.
* The constraint is a *post-processor oracle*: it calls the existing
  path-feasibility LP ``compute_completion_time_interval`` and never
  modifies the TPN, the verifier, or the search objective.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple

from ..model.tpn import TPN
from ..exact_confirmation.lp_solver import compute_completion_time_interval


# ---------------------------------------------------------------------------
# Canonicalization / identity helpers
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    """Deterministic JSON encoding (Fractions via str, keys sorted)."""
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _jsonable(value: Any) -> Any:
    """Recursively convert Fraction -> 'n/d', tuple -> list for JSON-safe audit."""
    if isinstance(value, Fraction):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _structural_hash(tpn: TPN) -> str:
    """Canonical hash of the TPN structure (independent of interval values)."""
    transitions = []
    for name in sorted(tpn.transitions):
        t = tpn.transitions[name]
        transitions.append([
            name,
            t.label,
            t.is_fault,
            t.is_adjustable,
            str(t.interval),
            [str(iv) for iv in t.admissible_intervals],
            str(t.authorized_range),
        ])
    payload = {
        "places": sorted(tpn.places),
        "transitions": transitions,
        "arcs": sorted((a.from_node, a.to_node) for a in tpn.arcs),
        "initial_marking": sorted(tpn.initial_marking.items()),
        "completion_markings": sorted(
            sorted(m.items()) for m in tpn.completion_markings
        ),
    }
    return _sha256(_canonical_json(payload))


def _full_model_key(tpn: TPN) -> List[Tuple[str, str, str]]:
    """Complete model interval fingerprint: sorted (name, earliest, latest)
    over ALL transitions.  This is the candidate identity used by the task
    cache and the audit; it represents the full candidate assignment, not a
    local delta."""
    return sorted(
        (name, str(tr.interval.earliest), str(tr.interval.latest))
        for name, tr in tpn.transitions.items()
    )


def task_candidate_id(tpn: TPN, reference: Optional["ReferenceBehaviorSet"] = None) -> str:
    """Stable per-(candidate model, reference set) id for audit chaining."""
    digest = _sha1_hex(_canonical_json(_full_model_key(tpn)))
    if reference is not None:
        digest = _sha1_hex(reference.reference_fingerprint + digest)
    return digest


# ---------------------------------------------------------------------------
# Reference behavior set (frozen, immutable)
# ---------------------------------------------------------------------------


def _cert_sequences(cert: Any) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Extract (sigma_N, sigma_F) from a Certificate dataclass or mapping."""
    if isinstance(cert, Mapping):
        return tuple(cert["sigma_N"]), tuple(cert["sigma_F"])
    return tuple(cert.sigma_N), tuple(cert.sigma_F)


@dataclass(frozen=True)
class ReferenceBehaviorSet:
    """Frozen snapshot of the reference model's critical behavior.

    Constructed ONLY via ``from_verification``, which enforces the hard
    preconditions: a conclusive AMB verdict, a complete certificate
    enumeration (COMPLETE_SET with enumeration_complete=True), and at
    least one certificate.  The set is immutable for the lifetime of a
    repair run; every candidate is evaluated against the same snapshot.
    """

    normal_sequences: Tuple[Tuple[str, ...], ...]
    fault_sequences: Tuple[Tuple[str, ...], ...]
    H: float
    model_id: str
    L: Optional[int]
    n_source_certificates: int
    structural_hash: str = ""
    reference_fingerprint: str = ""

    def __post_init__(self) -> None:
        payload = {
            "model_id": self.model_id,
            "structural_hash": self.structural_hash,
            "L": str(self.L),
            "H": str(self.H),
            "normal": [list(s) for s in self.normal_sequences],
            "fault": [list(s) for s in self.fault_sequences],
        }
        object.__setattr__(
            self, "reference_fingerprint", _sha256(_canonical_json(payload))
        )

    @classmethod
    def from_verification(
        cls,
        verify_out: Mapping[str, Any],
        tpn: TPN,
        H: float,
        model_id: str = "",
        L: Optional[int] = None,
    ) -> "ReferenceBehaviorSet":
        """Freeze the reference behavior from a complete-set AMB verification.

        Raises ValueError if the verification does not satisfy the hard
        preconditions (conclusive AMB, COMPLETE_SET, enumeration complete,
        non-empty certificate set).  These are frozen at construction and
        cannot be relaxed later.
        """
        if verify_out.get("result") != "AMB":
            raise ValueError(
                "ReferenceBehaviorSet requires an AMB verdict, got "
                f"{verify_out.get('result')!r}"
            )
        if verify_out.get("certificate_mode") != "COMPLETE_SET":
            raise ValueError(
                "ReferenceBehaviorSet requires a COMPLETE_SET certificate "
                f"enumeration, got {verify_out.get('certificate_mode')!r}"
            )
        if verify_out.get("enumeration_complete") is not True:
            raise ValueError(
                "ReferenceBehaviorSet requires enumeration_complete=True "
                "(a conclusive complete enumeration), got "
                f"{verify_out.get('enumeration_complete')!r}"
            )
        certs = verify_out.get("certificates") or []
        if not certs:
            raise ValueError(
                "ReferenceBehaviorSet requires at least one certificate"
            )

        normal = tuple(sorted({_cert_sequences(c)[0] for c in certs}))
        fault = tuple(sorted({_cert_sequences(c)[1] for c in certs}))
        if not normal or not fault:
            raise ValueError(
                "ReferenceBehaviorSet requires non-empty normal and fault "
                "sequence sets"
            )

        return cls(
            normal_sequences=normal,
            fault_sequences=fault,
            H=H,
            model_id=model_id,
            L=L,
            n_source_certificates=len(certs),
            structural_hash=_structural_hash(tpn),
        )


# ---------------------------------------------------------------------------
# Constraint protocol and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalContext:
    """Context of one gate evaluation, for candidate-level auditing.

    lp_stage: 'P1' | 'P2' | 'P3' | 'P4' within the LP search, '' otherwise.
    path:    'level1' | 'level2' | 'level3' | 'lp' | 'pair' | 'final' | ...
    changes: ((transition, old_interval_str, new_interval_str), ...)
    """

    lp_stage: str = ""
    path: str = ""
    changes: Tuple[Tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class TaskConstraintResult:
    """Outcome of one task-constraint evaluation."""

    passed: bool
    constraint_name: str
    failed_constraints: Tuple[str, ...] = ()
    # Metric namespace: task_* metrics belong to the task constraint; the
    # verifier's own cache hits stay in the verifier stats (verification_*).
    metrics: Mapping[str, Any] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)


class TaskConstraint(Protocol):
    """A repair-time task constraint.

    evaluate(candidate_model, reference_model, metadata) -> result:
      candidate_model: the proposed repaired TPN (intervals modified).
      reference_model: the N0 reference TPN (structure-identical).
      metadata:        the frozen ReferenceBehaviorSet (or None for
                       constraints that need no reference).
    """

    name: str

    def evaluate(
        self,
        candidate_model: TPN,
        reference_model: Optional[TPN],
        metadata: Optional[ReferenceBehaviorSet],
    ) -> TaskConstraintResult: ...


class AlwaysTrueTaskConstraint:
    """psi_task == 1: every candidate is accepted.

    Exact reproduction of the submitted batch, which ran with
    ``task_predicate=lambda t: True``.  Used as the default so that
    unconstrained runs behave bit-identically to the legacy pipeline.
    """

    name = "always_true"

    def evaluate(
        self,
        candidate_model: TPN,
        reference_model: Optional[TPN],
        metadata: Optional[ReferenceBehaviorSet],
    ) -> TaskConstraintResult:
        return TaskConstraintResult(passed=True, constraint_name=self.name)


class LegacyPredicateTaskConstraint:
    """Adapter wrapping the legacy ``task_predicate: TPN -> bool`` callable.

    Keeps existing call sites (CLI completion-reachability check, the
    non-vacuity pilot's structural check) working unchanged: the callable
    is invoked on each candidate exactly as before, including any side
    effects (diagnostics).
    """

    name = "legacy_predicate"

    def __init__(self, predicate: Any):
        self._predicate = predicate

    def evaluate(
        self,
        candidate_model: TPN,
        reference_model: Optional[TPN],
        metadata: Optional[ReferenceBehaviorSet],
    ) -> TaskConstraintResult:
        return TaskConstraintResult(
            passed=bool(self._predicate(candidate_model)),
            constraint_name=self.name,
        )


class ExactCriticalSequencePreservation:
    """v1 task constraint: preserve the exact critical sequences of N0.

    For every sigma_N / sigma_F sequence frozen in the reference set, the
    candidate model must still admit that exact firing sequence as a
    timed-feasible path under the horizon H.  Repair only changes static
    intervals (structure is never modified), so preservation is exactly
    the feasibility of each reference sequence's path polytope
    P_H(sigma) in the candidate model, checked with the existing
    completion-time LP oracle.

    Scope (deliberately narrow): only the sequences that witness the
    confirmed ambiguities of N0 are preserved.  Other firing sequences,
    behavior families, and timing profiles of N0 are NOT preserved by
    this constraint (a future constraint can add them).

    Failure is fail-closed: if the oracle cannot determine feasibility
    (solver crash), the sequence is treated as lost.
    """

    name = "exact_critical_sequence_preservation"

    def __init__(
        self,
        reference: ReferenceBehaviorSet,
        task_cache: Optional[Dict] = None,
    ):
        self._reference = reference
        # Task-constraint cache, separate namespace from the verifier cache.
        # Key: (constraint name, reference fingerprint, L, H, full model key).
        self._task_cache: Dict[Tuple, Tuple] = task_cache if task_cache is not None else {}

    def _cache_key(self, candidate_model: TPN) -> Tuple:
        return (
            self.name,
            self._reference.reference_fingerprint,
            str(self._reference.L),
            str(self._reference.H),
            _canonical_json(_full_model_key(candidate_model)),
        )

    def evaluate(
        self,
        candidate_model: TPN,
        reference_model: Optional[TPN],
        metadata: Optional[ReferenceBehaviorSet],
    ) -> TaskConstraintResult:
        if metadata is not None and (
            metadata.reference_fingerprint != self._reference.reference_fingerprint
        ):
            raise ValueError(
                "metadata reference set does not match the constraint's "
                "bound reference set"
            )

        ckey = self._cache_key(candidate_model)
        if ckey in self._task_cache:
            passed, failed, details, miss_metrics = self._task_cache[ckey]
            return TaskConstraintResult(
                passed=passed,
                constraint_name=self.name,
                failed_constraints=failed,
                metrics={
                    "task_cache_hit": 1,
                    "task_lp_calls_actual": 0,
                    "n_normal_sequences": miss_metrics.get("n_normal_sequences", 0),
                    "n_fault_sequences": miss_metrics.get("n_fault_sequences", 0),
                    "n_normal_retained": miss_metrics.get("n_normal_retained", 0),
                    "n_fault_retained": miss_metrics.get("n_fault_retained", 0),
                    "n_oracle_failures": 0,
                },
                details=details,
            )

        failed: List[str] = []
        lost_normal: List[List[str]] = []
        lost_fault: List[List[str]] = []
        intervals_normal: List[List[str]] = []
        intervals_fault: List[List[str]] = []
        n_oracle_failures = 0

        def _check(sigma: Tuple[str, ...]) -> Optional[Tuple[str, str]]:
            """Return (t_min, t_max) string pair, or None if infeasible/lost."""
            nonlocal n_oracle_failures
            try:
                iv = compute_completion_time_interval(
                    candidate_model, sigma, self._reference.H
                )
            except Exception:
                iv = None
            if iv is None:
                n_oracle_failures += 1
                return None
            return str(iv[0]), str(iv[1])

        # Canonical (sorted) order — failure names are stable indices.
        for i, sigma in enumerate(self._reference.normal_sequences):
            bounds = _check(sigma)
            if bounds is None:
                failed.append(f"normal:{i}")
                lost_normal.append(list(sigma))
            else:
                intervals_normal.append([f"normal:{i}", bounds[0], bounds[1]])
        for j, sigma in enumerate(self._reference.fault_sequences):
            bounds = _check(sigma)
            if bounds is None:
                failed.append(f"fault:{j}")
                lost_fault.append(list(sigma))
            else:
                intervals_fault.append([f"fault:{j}", bounds[0], bounds[1]])

        metrics = {
            "task_cache_hit": 0,
            "task_lp_calls_actual": (
                len(self._reference.normal_sequences)
                + len(self._reference.fault_sequences)
            ),
            "n_normal_sequences": len(self._reference.normal_sequences),
            "n_fault_sequences": len(self._reference.fault_sequences),
            "n_normal_retained": len(intervals_normal),
            "n_fault_retained": len(intervals_fault),
            "n_oracle_failures": n_oracle_failures,
        }
        details = {
            "intervals_normal": intervals_normal,
            "intervals_fault": intervals_fault,
            "lost_normal": lost_normal,
            "lost_fault": lost_fault,
        }
        result = TaskConstraintResult(
            passed=not failed,
            constraint_name=self.name,
            failed_constraints=tuple(failed),
            metrics=metrics,
            details=details,
        )
        self._task_cache[ckey] = (
            result.passed,
            result.failed_constraints,
            result.details,
            metrics,
        )
        return result


# ---------------------------------------------------------------------------
# Candidate-level audit chain
# ---------------------------------------------------------------------------


class TaskAudit:
    """Per-repair candidate-level audit of every task-gate decision.

    Every gate evaluation — accepted or rejected, in every adoption path,
    including rejected LP-internal proposals (P1-P4) — is recorded with its
    candidate id, changes, objective before, and verification status
    ('SKIPPED_TASK' for gate-rejected candidates, which are never
    verified).  Entries are keyed by (step, candidate_id) and capped by
    *detail_cap*; overflow is counted, not silently truncated.
    """

    def __init__(
        self,
        constraint_name: str,
        reference_snapshot: Optional[Mapping[str, Any]] = None,
        detail_cap: int = 10_000,
    ):
        self.constraint_name = constraint_name
        self.reference_snapshot = _jsonable(dict(reference_snapshot or {}))
        self.detail_cap = detail_cap
        self._entries: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._order: List[Tuple[str, str]] = []
        self.dropped = 0
        self.rejected_by_task = 0
        self.adopted = 0

    def record(
        self,
        step: str,
        candidate_id: str,
        path: str,
        lp_stage: str,
        changes: Tuple[Tuple[str, str, str], ...],
        task_result: TaskConstraintResult,
        objective_before: Optional[Tuple] = None,
    ) -> None:
        key = (step, candidate_id)
        if key in self._entries:
            return  # first record wins; later decisions patch via .patch()
        if len(self._order) >= self.detail_cap:
            self.dropped += 1
            return
        entry = {
            "step": step,
            "candidate_id": candidate_id,
            "path": path,
            "lp_stage": lp_stage,
            "changes": [list(c) for c in changes],
            "task_passed": bool(task_result.passed),
            "failed_constraints": list(task_result.failed_constraints),
            "task_metrics": _jsonable(dict(task_result.metrics)),
            "objective_before": _jsonable(objective_before),
            "objective_candidate": None,
            "verification_status": (
                "SKIPPED_TASK" if not task_result.passed else None
            ),
            "disposition": None,
        }
        self._entries[key] = entry
        self._order.append(key)
        if not task_result.passed:
            self.rejected_by_task += 1

    def patch(
        self,
        step: str,
        candidate_id: str,
        **fields: Any,
    ) -> None:
        """Update a recorded entry (e.g. after verification / adoption)."""
        key = (step, candidate_id)
        if key not in self._entries:
            raise KeyError(f"audit patch on unrecorded candidate: {key}")
        entry = self._entries[key]
        for k, v in fields.items():
            if k == "objective_candidate":
                v = _jsonable(v)
            entry[k] = v
        if fields.get("disposition") == "adopted":
            self.adopted += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "constraint_name": self.constraint_name,
            "reference_snapshot": self.reference_snapshot,
            "n_entries": len(self._order),
            "n_dropped_overflow": self.dropped,
            "rejected_by_task": self.rejected_by_task,
            "adopted": self.adopted,
            "entries": [self._entries[k] for k in self._order],
        }
