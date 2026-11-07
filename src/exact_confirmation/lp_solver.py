"""
Exact path-polytope LP confirmation (supplement Section S3.4).

Path polytope P_H^N(sigma) constraints (Definition S3.6):
  Variables: d_i (delay before t_i), x_{i,u} (age of u before step i)

  x_{1,u} = 0              for all u in E(M_0)                  [initial age]
  d_i >= 0                  for i = 1..n                         [non-negative delay]
  x_{i,t_i} + d_i >= earliest(t_i)   for i = 1..n              [earliest firing]
  x_{i,u} + d_i <= latest(u)          for u in E(M_{i-1}), finite latest  [urgency]
  x_{i+1,u} = x_{i,u} + d_i           for u in E_i^p           [persistent clock]
  x_{i+1,u} = 0                        for u in E_i^n           [new clock]
  sum(d_i) <= H                                                   [horizon]

Uses scipy.optimize.linprog (HiGHS backend).
All Fraction endpoints are converted to float for LP.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import linprog

from ..model.tpn import INF, TPN
from ..profiling import Profile, bump, timed


class LPStatus(str, Enum):
    OPTIMAL = "OPTIMAL"
    INFEASIBLE = "INFEASIBLE"
    UNBOUNDED = "UNBOUNDED"
    NUMERICAL_FAILURE = "NUMERICAL_FAILURE"
    UNKNOWN = "UNKNOWN"


DEFAULT_FEASIBILITY_TOLERANCE = 1.0e-9
DEFAULT_OPTIMALITY_TOLERANCE = 1.0e-9


@dataclass
class LPResult:
    status: LPStatus
    value: Optional[float]  # None if not optimal


def _scipy_status_to_lp_status(status: int) -> LPStatus:
    if status == 0:
        return LPStatus.OPTIMAL
    elif status == 2:
        return LPStatus.INFEASIBLE
    elif status == 3:
        return LPStatus.UNBOUNDED
    elif status == 4:
        return LPStatus.NUMERICAL_FAILURE
    else:
        return LPStatus.UNKNOWN


def _frac_to_float(v) -> float:
    if math.isinf(v):
        return float("inf")
    return float(v)


def _build_lp_variables(
    tpn: TPN, sigma: Tuple[str, ...]
) -> Tuple[
    List[str],  # variable names in order
    Dict[str, int],  # name -> index
    List[Dict[str, int]],  # markings M_0..M_n
    List,  # E_p[i]: persistent enabled set after t_i
    List,  # E_n[i]: newly enabled set after t_i
]:
    """Build variable index and marking sequence for sigma.

    Returns (var_names, var_idx, markings, E_p_list, E_n_list).
    markings[i] = M_{i-1} (marking before firing t_i), i=0..n
    markings[n] = M_n (final marking)
    E_p_list[i] = persistent enabled after t_{i+1}
    E_n_list[i] = newly enabled after t_{i+1}
    """
    n = len(sigma)
    var_names: List[str] = []
    var_idx: Dict[str, int] = {}

    def add_var(name: str) -> int:
        if name not in var_idx:
            var_idx[name] = len(var_names)
            var_names.append(name)
        return var_idx[name]

    # d_i variables (i = 1..n, 0-indexed as i=0..n-1)
    for i in range(n):
        add_var(f"d_{i}")

    # Compute marking sequence and enabled sets
    markings: List[Dict[str, int]] = []
    E_p_list = []  # E_p[i] after firing t_{i+1}
    E_n_list = []  # E_n[i] after firing t_{i+1}

    marking = dict(tpn.initial_marking)
    markings.append(dict(marking))

    for i, t_name in enumerate(sigma):
        E_p = tpn.persistent_enabled(marking, t_name)
        E_n = tpn.newly_enabled(marking, t_name)
        E_p_list.append(E_p)
        E_n_list.append(E_n)
        marking = tpn.fire(marking, t_name)
        markings.append(dict(marking))

    # x_{i,u} variables: for each step i (1..n+1) and each u in E(M_{i-1})
    # i is 1-indexed in supplement; we use 0-indexed i=0..n
    for i in range(n + 1):
        m_i = markings[i]
        enabled_i = tpn.enabled_set(m_i)
        for u in enabled_i:
            add_var(f"x_{i}_{u}")

    return var_names, var_idx, markings, E_p_list, E_n_list


def _build_constraints(
    tpn: TPN,
    sigma: Tuple[str, ...],
    H: float,
    var_names: List[str],
    var_idx: Dict[str, int],
    markings: List[Dict[str, int]],
    E_p_list,
    E_n_list,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Build LP constraint matrices.

    Returns (A_ub, b_ub, A_eq, b_eq, bounds_array).
    bounds_array: shape (n_vars, 2) with (lb, ub) per variable.
    """
    n = len(sigma)
    n_vars = len(var_names)

    # We collect:
    # Equality constraints: A_eq @ x = b_eq
    # Inequality constraints: A_ub @ x <= b_ub
    # Variable bounds: lb <= x_j <= ub

    eq_rows: List[Tuple[Dict[str, float], float]] = []  # (coeffs, rhs)
    ub_rows: List[Tuple[Dict[str, float], float]] = []  # (coeffs, rhs)

    # Variable bounds: d_i >= 0, x_{i,u} >= 0
    bounds = [(0.0, None)] * n_vars  # default: >= 0, no upper bound

    def idx(name: str) -> int:
        return var_idx[name]

    # Constraint: x_{1,u} = 0 for all u in E(M_0)
    # (0-indexed: x_{0,u} = 0 for u in E(markings[0]))
    enabled_0 = tpn.enabled_set(markings[0])
    for u in enabled_0:
        var_name = f"x_0_{u}"
        if var_name in var_idx:
            eq_rows.append(({var_name: 1.0}, 0.0))

    for i in range(n):
        t_name = sigma[i]
        t_obj = tpn.transitions[t_name]
        m_i = markings[i]  # M_{i} (before firing t_{i+1} in 1-indexed)
        enabled_i = tpn.enabled_set(m_i)

        d_var = f"d_{i}"

        # Constraint: x_{i,t_i} + d_i >= earliest(t_i)
        # => -x_{i,t_name} - d_i <= -earliest(t_i)
        x_var = f"x_{i}_{t_name}"
        if x_var in var_idx:
            ub_rows.append(({x_var: -1.0, d_var: -1.0}, -float(t_obj.interval.earliest)))

        # Constraint: x_{i,u} + d_i <= latest(u) for u in E(M_i) with finite latest
        for u in enabled_i:
            t_u = tpn.transitions[u]
            if not t_u.interval.is_infinite():
                x_u_var = f"x_{i}_{u}"
                if x_u_var in var_idx:
                    ub_rows.append(({x_u_var: 1.0, d_var: 1.0}, float(t_u.interval.latest)))

        # Persistent clock: x_{i+1,u} = x_{i,u} + d_i for u in E_p[i]
        for u in E_p_list[i]:
            x_next = f"x_{i+1}_{u}"
            x_curr = f"x_{i}_{u}"
            if x_next in var_idx and x_curr in var_idx:
                # x_{i+1,u} - x_{i,u} - d_i = 0
                eq_rows.append(({x_next: 1.0, x_curr: -1.0, d_var: -1.0}, 0.0))

        # New clock: x_{i+1,u} = 0 for u in E_n[i]
        for u in E_n_list[i]:
            x_next = f"x_{i+1}_{u}"
            if x_next in var_idx:
                eq_rows.append(({x_next: 1.0}, 0.0))

    # Horizon constraint: sum(d_i) <= H
    h_coeffs = {f"d_{i}": 1.0 for i in range(n)}
    ub_rows.append((h_coeffs, float(H)))

    # Convert to numpy
    def to_matrix(rows, n_vars):
        if not rows:
            return np.zeros((0, n_vars)), np.zeros(0)
        A = np.zeros((len(rows), n_vars))
        b = np.zeros(len(rows))
        for r, (coeffs, rhs) in enumerate(rows):
            for vname, coeff in coeffs.items():
                if vname in var_idx:
                    A[r, var_idx[vname]] = coeff
            b[r] = rhs
        return A, b

    A_ub, b_ub = to_matrix(ub_rows, n_vars)
    A_eq, b_eq = to_matrix(eq_rows, n_vars)

    bounds_array = np.array(
        [(0.0, np.inf)] * n_vars, dtype=[("lb", float), ("ub", float)]
    )

    return A_ub, b_ub, A_eq, b_eq, bounds


def _solve_lp(
    tpn: TPN,
    sigma: Tuple[str, ...],
    H: float,
    minimize: bool,
    feasibility_tolerance: float = DEFAULT_FEASIBILITY_TOLERANCE,
    optimality_tolerance: float = DEFAULT_OPTIMALITY_TOLERANCE,
    profile: Profile | None = None,
) -> LPResult:
    """Solve path polytope LP. Returns LPResult with status and optimal value (or None)."""

    if not sigma:
        # Empty sequence: duration is 0
        return LPResult(status=LPStatus.OPTIMAL, value=0.0)

    with timed(profile, "runtime_lp_build"):
        var_names, var_idx, markings, E_p_list, E_n_list = _build_lp_variables(
            tpn, sigma
        )
        A_ub, b_ub, A_eq, b_eq, bounds = _build_constraints(
            tpn, sigma, H, var_names, var_idx, markings, E_p_list, E_n_list
        )
    bump(profile, "lp_builds")
    n = len(sigma)
    n_vars = len(var_names)

    # Objective: minimize or maximize sum(d_i)
    c = np.zeros(n_vars)
    for i in range(n):
        c[var_idx[f"d_{i}"]] = 1.0 if minimize else -1.0

    with timed(profile, "runtime_lp_solve"):
        result = linprog(
            c,
            A_ub=A_ub if A_ub.shape[0] > 0 else None,
            b_ub=b_ub if b_ub.shape[0] > 0 else None,
            A_eq=A_eq if A_eq.shape[0] > 0 else None,
            b_eq=b_eq if b_eq.shape[0] > 0 else None,
            bounds=bounds,
            method="highs",
            options={
                "presolve": True,
                "disp": False,
                "dual_feasibility_tolerance": feasibility_tolerance,
                "primal_feasibility_tolerance": feasibility_tolerance,
                "ipm_optimality_tolerance": optimality_tolerance,
            },
        )
    bump(profile, "lp_solves")

    lp_status = _scipy_status_to_lp_status(result.status)
    if lp_status == LPStatus.OPTIMAL:
        total = sum(result.x[var_idx[f"d_{i}"]] for i in range(n))
        return LPResult(status=LPStatus.OPTIMAL, value=total)
    return LPResult(status=lp_status, value=None)


def compute_completion_time_interval(
    tpn: TPN,
    sigma: Tuple[str, ...],
    H: float,
    feasibility_tolerance: float = DEFAULT_FEASIBILITY_TOLERANCE,
    optimality_tolerance: float = DEFAULT_OPTIMALITY_TOLERANCE,
    profile: Profile | None = None,
) -> Optional[Tuple[Fraction, object]]:
    """Compute [t_min, t_max] for firing sequence sigma.

    Returns (t_min, t_max) as Fractions, or None if infeasible.
    t_max may be H (as Fraction) if the maximum equals the horizon.
    """
    min_result = _solve_lp(
        tpn,
        sigma,
        H,
        minimize=True,
        feasibility_tolerance=feasibility_tolerance,
        optimality_tolerance=optimality_tolerance,
        profile=profile,
    )
    if min_result.status != LPStatus.OPTIMAL:
        return None

    max_result = _solve_lp(
        tpn,
        sigma,
        H,
        minimize=False,
        feasibility_tolerance=feasibility_tolerance,
        optimality_tolerance=optimality_tolerance,
        profile=profile,
    )
    if max_result.status == LPStatus.OPTIMAL:
        t_max_f = max_result.value
    else:
        # Non-optimal max: use H as conservative upper bound
        t_max_f = H

    # Convert to Fraction for exactness (rounded to reasonable precision)
    t_min = Fraction(min_result.value).limit_denominator(10**9)
    t_max = Fraction(t_max_f).limit_denominator(10**9)

    return (t_min, t_max)


def confirm_ambiguity(
    tpn: TPN,
    sigma_N: Tuple[str, ...],
    sigma_F: Tuple[str, ...],
    H: float,
    feasibility_tolerance: float = DEFAULT_FEASIBILITY_TOLERANCE,
    optimality_tolerance: float = DEFAULT_OPTIMALITY_TOLERANCE,
    profile: Profile | None = None,
) -> Optional[Dict]:
    """Solve coupled polytope to confirm ambiguity.

    Couples P_H(sigma_N) + P_H(sigma_F) with the constraint:
      sum(d_i^N) == sum(d_j^F)

    Returns {'overlap': (t_min, t_max)} if feasible, None if infeasible.
    """

    n_N = len(sigma_N)
    n_F = len(sigma_F)
    _build_started = time.perf_counter() if profile is not None else 0.0

    # Build variables for each path independently, with prefixes
    var_names_N, var_idx_N, markings_N, E_p_N, E_n_N = _build_lp_variables(
        tpn, sigma_N
    )
    var_names_F, var_idx_F, markings_F, E_p_F, E_n_F = _build_lp_variables(
        tpn, sigma_F
    )

    # Rename variables with prefix
    def prefix_vars(var_names, var_idx, prefix):
        new_names = [f"{prefix}{v}" for v in var_names]
        new_idx = {f"{prefix}{v}": i for v, i in var_idx.items()}
        return new_names, new_idx

    var_names_N_p, var_idx_N_p = prefix_vars(var_names_N, var_idx_N, "N_")
    var_names_F_p, var_idx_F_p = prefix_vars(var_names_F, var_idx_F, "F_")

    # Combined variable list
    all_var_names = var_names_N_p + var_names_F_p
    all_var_idx = {}
    for v, i in var_idx_N_p.items():
        all_var_idx[v] = i
    n_N_vars = len(var_names_N_p)
    for v, i in var_idx_F_p.items():
        all_var_idx[v] = i + n_N_vars

    n_total = len(all_var_names)

    # Build constraints for N path (with prefix)
    A_ub_N, b_ub_N, A_eq_N, b_eq_N, bounds_N = _build_constraints(
        tpn, sigma_N, H, var_names_N, var_idx_N, markings_N, E_p_N, E_n_N
    )
    # Build constraints for F path (with prefix)
    A_ub_F, b_ub_F, A_eq_F, b_eq_F, bounds_F = _build_constraints(
        tpn, sigma_F, H, var_names_F, var_idx_F, markings_F, E_p_F, E_n_F
    )

    # Embed into combined variable space
    def embed(A, var_names_local, var_names_local_p, all_var_idx, n_total):
        if A.shape[0] == 0:
            return np.zeros((0, n_total))
        A_new = np.zeros((A.shape[0], n_total))
        for col, vname in enumerate(var_names_local):
            pname = var_names_local_p[col]
            if pname in all_var_idx:
                A_new[:, all_var_idx[pname]] = A[:, col]
        return A_new

    A_ub_N_e = embed(A_ub_N, var_names_N, var_names_N_p, all_var_idx, n_total)
    A_ub_F_e = embed(A_ub_F, var_names_F, var_names_F_p, all_var_idx, n_total)
    A_eq_N_e = embed(A_eq_N, var_names_N, var_names_N_p, all_var_idx, n_total)
    A_eq_F_e = embed(A_eq_F, var_names_F, var_names_F_p, all_var_idx, n_total)

    A_ub_combined = np.vstack([A_ub_N_e, A_ub_F_e]) if (A_ub_N_e.shape[0] + A_ub_F_e.shape[0]) > 0 else np.zeros((0, n_total))
    b_ub_combined = np.concatenate([b_ub_N, b_ub_F])

    # Common duration constraint: sum(d_i^N) - sum(d_j^F) = 0
    duration_eq = np.zeros((1, n_total))
    for i in range(n_N):
        vname = f"N_d_{i}"
        if vname in all_var_idx:
            duration_eq[0, all_var_idx[vname]] = 1.0
    for j in range(n_F):
        vname = f"F_d_{j}"
        if vname in all_var_idx:
            duration_eq[0, all_var_idx[vname]] = -1.0

    A_eq_combined = np.vstack([A_eq_N_e, A_eq_F_e, duration_eq]) if (A_eq_N_e.shape[0] + A_eq_F_e.shape[0]) > 0 else duration_eq
    b_eq_combined = np.concatenate([b_eq_N, b_eq_F, [0.0]])

    # Combined bounds
    combined_bounds = list(bounds_N) + list(bounds_F)

    # Objective: minimize total duration (to find t_min of overlap)
    c_min = np.zeros(n_total)
    for i in range(n_N):
        vname = f"N_d_{i}"
        if vname in all_var_idx:
            c_min[all_var_idx[vname]] = 1.0

    if profile is not None:
        profile["runtime_lp_build"] = profile.get("runtime_lp_build", 0.0) + (
            time.perf_counter() - _build_started
        )
        bump(profile, "lp_builds")
    _solve_started = time.perf_counter() if profile is not None else 0.0
    result_min = linprog(
        c_min,
        A_ub=A_ub_combined if A_ub_combined.shape[0] > 0 else None,
        b_ub=b_ub_combined if b_ub_combined.shape[0] > 0 else None,
        A_eq=A_eq_combined if A_eq_combined.shape[0] > 0 else None,
        b_eq=b_eq_combined,
        bounds=combined_bounds,
        method="highs",
        options={
            "presolve": True,
            "disp": False,
            "dual_feasibility_tolerance": feasibility_tolerance,
            "primal_feasibility_tolerance": feasibility_tolerance,
            "ipm_optimality_tolerance": optimality_tolerance,
        },
    )

    if profile is not None:
        profile["runtime_lp_solve"] = profile.get("runtime_lp_solve", 0.0) + (
            time.perf_counter() - _solve_started
        )
        bump(profile, "lp_solves")
    min_status = _scipy_status_to_lp_status(result_min.status)

    if min_status == LPStatus.INFEASIBLE:
        return None
    elif min_status != LPStatus.OPTIMAL:
        # UNKNOWN, NUMERICAL_FAILURE, UNBOUNDED: cannot conclude
        return {"status": "SOLVER_UNKNOWN"}

    t_min_f = sum(result_min.x[all_var_idx[f"N_d_{i}"]] for i in range(n_N) if f"N_d_{i}" in all_var_idx)

    # Maximize total duration
    c_max = -c_min
    _solve_started = time.perf_counter() if profile is not None else 0.0
    result_max = linprog(
        c_max,
        A_ub=A_ub_combined if A_ub_combined.shape[0] > 0 else None,
        b_ub=b_ub_combined if b_ub_combined.shape[0] > 0 else None,
        A_eq=A_eq_combined if A_eq_combined.shape[0] > 0 else None,
        b_eq=b_eq_combined,
        bounds=combined_bounds,
        method="highs",
        options={
            "presolve": True,
            "disp": False,
            "dual_feasibility_tolerance": feasibility_tolerance,
            "primal_feasibility_tolerance": feasibility_tolerance,
            "ipm_optimality_tolerance": optimality_tolerance,
        },
    )

    if profile is not None:
        profile["runtime_lp_solve"] = profile.get("runtime_lp_solve", 0.0) + (
            time.perf_counter() - _solve_started
        )
        bump(profile, "lp_solves")
    max_status = _scipy_status_to_lp_status(result_max.status)
    if max_status == LPStatus.OPTIMAL:
        t_max_f = sum(result_max.x[all_var_idx[f"N_d_{i}"]] for i in range(n_N) if f"N_d_{i}" in all_var_idx)
    elif max_status in (LPStatus.NUMERICAL_FAILURE, LPStatus.UNKNOWN, LPStatus.UNBOUNDED):
        return {"status": "SOLVER_UNKNOWN"}
    else:
        # INFEASIBLE for max when min is feasible: use H as upper bound
        t_max_f = float(H)

    t_min = Fraction(t_min_f).limit_denominator(10**9)
    t_max = Fraction(t_max_f).limit_denominator(10**9)

    return {"overlap": (t_min, t_max), "status": "FEASIBLE"}
