"""
RMT-FMS benchmark generator.
Synthetic diagnosis benchmark adapted from Huang et al. (IEEE TII, 2024).
Structure: reconfigurable machine tools, part types, sequential operations.

Purely acyclic (DAG) model. Each part executes its operations sequentially.
Each operation has a normal path and optionally a fault path.

Machine resource tokens: each machine m has p_machine_m_cap (machine_capacity
tokens). A job occupying machine m consumes one token at transfer time;
it is returned when processing ends.

Configuration-feature mapping: each machine has n_configs configurations.
Config is selected at job-arrival time via config-specific transfer transitions
t_trans_j{j}_op{k}_m{m}_cfg{c}. This avoids standalone reconfig cycles and
keeps the SCG acyclic and finite. Each (op, config) pair uses a dedicated
at-place p{j}_op{k}_at_m{m}_cfg{c} and processing transition
t_proc_j{j}_op{k}_m{m}_cfg{c} with a config-scaled interval.

When n_configs=1 (default), the model reduces to the original structure with
a single transfer/process pair per operation (no config suffix in logic,
though the naming still uses cfg1).
"""
from __future__ import annotations

import random
from fractions import Fraction
from typing import Dict, List, Optional, Set, Tuple

from ..model.tpn import TPN, Place, Transition, Arc, Interval, INF, AuthorizedRange

# R3-2b: explicit three-tier admissible-domain policies for the recovery
# transition t_recov.  Only the recovery domain varies across tiers; the
# t_proc domain (±50% of nominal) is held fixed so the controllability
# comparison isolates the recovery-time adjustment authority.
#
# IMPORTANT (R3-2b semantics): all three tiers are *synthetic exploratory*
# sensitivity levels.  'wide' reproduces the submitted 'extended' domain
# bit-identically.  The Moderate/Narrow tiers are NOT presented as physical
# or industrial constraints; they are a controllability ladder showing that
# the repair outcome (success rate, modification magnitude) is tunable by
# the system designer's choice of adjustment domain.  Success rates under
# any tier are exploratory analysis, not the paper's final experiment data.
#
# R3-2c: the tier ladder is frozen (no tuning).  Two mechanism ablations of
# 'wide' are added to isolate the fast-recovery [0,1] vs delayed-recovery
# [3,5] contributions to the wide->moderate success cliff:
#   'wide-no-fast'    : drop [0,1] AND tighten the authorized lower bound to 1
#                       (compressed recovery is disabled).
#   'wide-no-delayed' : drop [3,5] AND tighten the authorized upper bound to 4
#                       (delayed recovery beyond [2,4] is disabled).
# The authorized range is tightened together with the admissible set so the
# continuous LP search cannot silently re-introduce the removed mechanism.
DOMAIN_POLICIES = ("wide", "moderate", "narrow")
ABLATION_POLICIES = ("wide-no-fast", "wide-no-delayed")

# R3-5A: candidate-discretization densities over the *wide* semantic envelope
# (t_recov outer bound [0, 5J/2], boundary options [0,1] and [3,5] retained).
# Sets are strictly nested: coarse ⊂ base ⊂ fine (see protocol §3.2/§3.3).
# Default LP resolution aligns with density; lp_resolution can decouple it for
# the D1/D2 mechanism diagnostics.
DENSITIES = ("coarse", "base", "fine")
_DENSITY_DEFAULT_RES = {
    "coarse": Fraction(1, 1),
    "base": Fraction(1, 2),
    "fine": Fraction(1, 4),
}


def _frac(v: float) -> Fraction:
    return Fraction(v).limit_denominator(1000)


def generate_rmt_fms(
    n_machines: int,
    n_parts: int,
    ops_per_part: int,
    wip_bound: int,
    unobservable_ratio: float,
    fault_locations: int,
    interval_width_factor: float,
    adjustable_ratio: float,
    seed: int = 0,
    machine_capacity: int = 1,
    n_configs: int = 2,
    repair_domain: str = "standard",
    domain_policy: Optional[str] = None,
    candidate_density: Optional[str] = None,
    lp_resolution: Optional[Fraction] = None,
    inspection_locations: int = 0,
) -> Tuple[TPN, int, float]:
    """Generate a parameterized RMT-FMS TPN model.

    Per operation (j=part, k=op, m=machine), for each config c in 1..n_configs:

      Normal path (config c):
        p{j}_op{k}_start + p_machine_m_cap + p_machine_m_cfg_c
          -[t_trans_j{j}_op{k}_m{m}_cfg{c}: obs/unobs [1,2]]->
        p{j}_op{k}_at_m{m}_cfg{c}
          -[t_proc_j{j}_op{k}_m{m}_cfg{c}: obs, config-scaled interval]->
        p{j}_op{k}_end + p_machine_m_cap + p_machine_m_cfg_c

      Fault path (for fault-designated operations only, config c):
        p{j}_op{k}_start + p_machine_m_cap + p_machine_m_cfg_c
          -[t_fault_j{j}_op{k}_m{m}_cfg{c}: unobs fault [2,5]]->
        p{j}_op{k}_fault_m{m}_cfg{c}
          -[t_recov_j{j}_op{k}_m{m}_cfg{c}: unobs adjustable, domain_policy/repair_domain-dependent]->
        p{j}_op{k}_at_m{m}_cfg{c}   <- merges with normal path at at-place

    repair_domain controls the admissible interval set for adjustable transitions
    (t_recov and adjustable t_proc). This is a sensitivity parameter corresponding
    to the controller resolution and repair authority:

      "standard": 3 intervals around the nominal (coarse resolution ~1.0).
      "extended": 5 intervals including fast-recovery [0,1] and delayed-recovery
                  [3,5] options (controller resolution 0.5 per protocol).

    domain_policy (R3-2b) overrides repair_domain with an explicit three-tier
    ladder over the t_recov domain, for the Tadj-controllability study:

      "wide":     identical to repair_domain="extended" (bit-identical).
      "moderate": 3 intervals [1,2],[1,3],[2,4], authorized [1,4].
      "narrow":   2 intervals [1,2],[1,3], authorized [1,3].
    And two R3-2c mechanism ablations of wide:
      "wide-no-fast":    drop [0,1], authorized lower bound 0 -> 1.
      "wide-no-delayed": drop [3,5], authorized upper bound capped at 4.

    candidate_density (R3-5A) fixes the *wide* semantic envelope (t_recov
    [0, 5J/2], boundary options [0,1]/[3,5]) and only varies the candidate
    discretization density: 'coarse' = {[0,1],[1,3],[3,5]}, 'base' = current
    wide 5-set, 'fine' = base + intermediate candidates.  Strictly nested.
    Overrides domain_policy/repair_domain for the t_recov/t_proc candidate
    sets when provided (R3-5 runs in the wide domain).

    lp_resolution (R3-5A) overrides the LP search resolution in all
    authorized ranges (default: aligned with candidate_density — coarse=1,
    base=1/2, fine=1/4).  Decoupling it from candidate_density enables the
    D1/D2 mechanism diagnostics (protocol §10.3).

    Only the t_recov domain varies across tiers; the t_proc domain (±50% of
    nominal) is held fixed.  The Moderate/Narrow tiers are synthetic
    exploratory levels (NOT physical/industrial constraints) — see module
    docstring for the R3-2b semantics.

    inspection_locations selects a deterministic, seed-controlled subset of
    operations with an optional post-process inspection.  At each selected
    operation the part may either continue immediately or fire one additional
    observable inspection transition.  This keeps the model acyclic while
    creating complete histories at several consecutive observable lengths,
    which is required for a meaningful controlled L-sweep.  The default zero
    preserves the v3 generator exactly.

    Returns (tpn, L, H).
    """
    rng = random.Random(seed)

    places: Dict[str, Place] = {}
    transitions: Dict[str, Transition] = {}
    arcs: List[Arc] = []

    WIP_PLACE = "p_wip_capacity"

    def add_place(name: str) -> str:
        places[name] = Place(name=name)
        return name

    def add_transition(
        name: str,
        label: str,
        is_fault: bool,
        is_adjustable: bool,
        interval: Interval,
        admissible_intervals: Tuple[Interval, ...] = (),
        authorized_range: AuthorizedRange = None,
    ) -> str:
        transitions[name] = Transition(
            name=name,
            label=label,
            is_fault=is_fault,
            is_adjustable=is_adjustable,
            interval=interval,
            admissible_intervals=admissible_intervals,
            authorized_range=authorized_range,
        )
        return name

    def add_arc(from_node: str, to_node: str) -> None:
        arcs.append(Arc(from_node=from_node, to_node=to_node))

    # ---------------------------------------------------------------------------
    # Pre-compute operation-to-machine assignment (flat round-robin)
    # ---------------------------------------------------------------------------
    op_machine: Dict[Tuple[int, int], int] = {}
    _flat = 0
    for _j in range(1, n_parts + 1):
        for _k in range(1, ops_per_part + 1):
            op_machine[(_j, _k)] = _flat % n_machines + 1
            _flat += 1

    used_machines: Set[int] = set(op_machine.values())
    used_machines_list = sorted(used_machines)

    # Determine which (j,k) pairs have fault paths
    fault_ops: Set[Tuple[int, int]] = set()
    fault_machine_set: Set[int] = set(
        rng.sample(used_machines_list, min(fault_locations, len(used_machines_list)))
    )
    for (j, k), m in op_machine.items():
        if m in fault_machine_set:
            fault_ops.add((j, k))

    # Determine adjustable processing transitions
    total_proc = n_parts * ops_per_part
    if not 0 <= inspection_locations <= total_proc:
        raise ValueError(
            "inspection_locations must be between 0 and "
            f"n_parts*ops_per_part ({total_proc}), got {inspection_locations}"
        )
    adjustable_count = max(1, int(total_proc * adjustable_ratio))
    adjustable_indices: Set[int] = set(rng.sample(range(total_proc), adjustable_count))

    # Determine which transfer transitions are unobservable
    total_transfer = n_parts * ops_per_part
    unobs_count = int(total_transfer * unobservable_ratio)
    unobs_transfer_indices: Set[int] = set(rng.sample(range(total_transfer), unobs_count))

    # Sample after all legacy choices so inspection_locations=0 is bit-for-bit
    # compatible with the v3 generator's model topology and random choices.
    inspection_indices: Set[int] = set(
        rng.sample(range(total_proc), inspection_locations)
    )

    # ---------------------------------------------------------------------------
    # Build places and initial marking
    # ---------------------------------------------------------------------------
    initial_marking: Dict[str, int] = {}

    # WIP capacity place
    add_place(WIP_PLACE)
    initial_marking[WIP_PLACE] = wip_bound

    # Machine resource and config places
    for m in used_machines_list:
        cap_place = f"p_machine_{m}_cap"
        add_place(cap_place)
        initial_marking[cap_place] = machine_capacity
        for c in range(1, n_configs + 1):
            cfg_place = f"p_machine_{m}_cfg_{c}"
            add_place(cfg_place)
            initial_marking[cfg_place] = 1 if c == 1 else 0

    # ---------------------------------------------------------------------------
    # Build per-part sequential structure (acyclic)
    # ---------------------------------------------------------------------------
    proc_idx = 0
    transfer_idx = 0

    for j in range(1, n_parts + 1):
        for k in range(1, ops_per_part + 1):
            m = op_machine[(j, k)]

            start_place = f"p{j}_op{k}_start"
            end_place = f"p{j}_op{k}_end"
            cap_place = f"p_machine_{m}_cap"

            add_place(start_place)
            add_place(end_place)

            # First op of each part: WIP release transition
            if k == 1:
                release_place = f"p{j}_ready"
                add_place(release_place)
                initial_marking[release_place] = 1

                t_release = f"t_release_j{j}"
                add_transition(
                    t_release,
                    label="",
                    is_fault=False,
                    is_adjustable=False,
                    interval=Interval(Fraction(0), Fraction(0)),
                )
                add_arc(release_place, t_release)
                add_arc(WIP_PLACE, t_release)
                add_arc(t_release, start_place)

            # Processing interval base (config=1 reference)
            m_proc = Fraction(3)
            w_proc = Fraction(1)
            proc_lo_base = max(Fraction(0), m_proc - _frac(interval_width_factor) * w_proc)
            proc_hi_base = m_proc + _frac(interval_width_factor) * w_proc
            is_adj = proc_idx in adjustable_indices
            is_unobs_transfer = transfer_idx in unobs_transfer_indices

            # For each config: config-specific transfer -> at-place -> proc
            for c in range(1, n_configs + 1):
                cfg_place = f"p_machine_{m}_cfg_{c}"
                at_place_c = f"p{j}_op{k}_at_m{m}_cfg{c}"
                add_place(at_place_c)

                config_factor = Fraction(c, n_configs)

                # Transfer transition (config-selecting at arrival)
                t_trans = f"t_trans_j{j}_op{k}_m{m}_cfg{c}"
                trans_label = "" if is_unobs_transfer else f"transfer_j{j}_m{m}"
                add_transition(
                    t_trans,
                    label=trans_label,
                    is_fault=False,
                    is_adjustable=False,
                    interval=Interval(Fraction(1), Fraction(2)),
                )
                # Consumes: start token + machine cap + machine in config c
                add_arc(start_place, t_trans)
                add_arc(cap_place, t_trans)
                add_arc(cfg_place, t_trans)
                # Produces: part at machine in config c
                add_arc(t_trans, at_place_c)

                # Fault path (if this op has a fault)
                if (j, k) in fault_ops:
                    fault_place_c = f"p{j}_op{k}_fault_m{m}_cfg{c}"
                    add_place(fault_place_c)

                    t_fault = f"t_fault_j{j}_op{k}_m{m}_cfg{c}"
                    add_transition(
                        t_fault,
                        label="",
                        is_fault=True,
                        is_adjustable=False,
                        interval=Interval(Fraction(2), Fraction(5)),
                    )
                    # Fault also selects config at arrival
                    add_arc(start_place, t_fault)
                    add_arc(cap_place, t_fault)
                    add_arc(cfg_place, t_fault)
                    add_arc(t_fault, fault_place_c)

                    t_recov = f"t_recov_j{j}_op{k}_m{m}_cfg{c}"
                    recov_interval = Interval(Fraction(1), Fraction(3))
                    if candidate_density is not None:
                        # R3-5A: nested discretization over the wide semantic
                        # envelope (outer bound 5J/2, boundary options
                        # [0,1]/[3,5] retained).  Only candidate density
                        # varies; the envelope is identical to wide.
                        if candidate_density not in DENSITIES:
                            raise ValueError(
                                f"unknown candidate_density: {candidate_density!r} "
                                f"(expected {' | '.join(DENSITIES)})"
                            )
                        res = (
                            lp_resolution if lp_resolution is not None
                            else _DENSITY_DEFAULT_RES[candidate_density]
                        )
                        lp_l_max = Fraction(5 * n_parts * ops_per_part, 2)
                        if candidate_density == "coarse":
                            recov_admissible = (
                                Interval(Fraction(0), Fraction(1)),
                                recov_interval,
                                Interval(Fraction(3), Fraction(5)),
                            )
                        elif candidate_density == "base":
                            recov_admissible = (
                                Interval(Fraction(0), Fraction(1)),
                                Interval(Fraction(1), Fraction(2)),
                                recov_interval,
                                Interval(Fraction(2), Fraction(4)),
                                Interval(Fraction(3), Fraction(5)),
                            )
                        else:  # fine
                            recov_admissible = (
                                Interval(Fraction(0), Fraction(1)),
                                Interval(Fraction(0), Fraction(2)),
                                Interval(Fraction(1), Fraction(2)),
                                recov_interval,
                                Interval(Fraction(2), Fraction(3)),
                                Interval(Fraction(2), Fraction(4)),
                                Interval(Fraction(3), Fraction(4)),
                                Interval(Fraction(3), Fraction(5)),
                            )
                        recov_authorized = AuthorizedRange(
                            l_min=Fraction(0), l_max=lp_l_max,
                            u_min=Fraction(0), u_max=lp_l_max,
                            resolution=res,
                        )
                    elif domain_policy is not None:
                        # R3-2b explicit tier ladder (see module docstring).
                        # 'wide' reproduces the extended domain bit-identically.
                        if domain_policy == "wide":
                            recov_admissible = (
                                Interval(Fraction(0), Fraction(1)),    # fast recovery
                                Interval(Fraction(1), Fraction(2)),
                                recov_interval,                         # nominal [1,3]
                                Interval(Fraction(2), Fraction(4)),
                                Interval(Fraction(3), Fraction(5)),    # delayed recovery
                            )
                            lp_l_max = Fraction(5 * n_parts * ops_per_part, 2)
                            recov_authorized = AuthorizedRange(
                                l_min=Fraction(0), l_max=lp_l_max,
                                u_min=Fraction(0), u_max=lp_l_max,
                                resolution=Fraction(1, 2),
                            )
                        elif domain_policy == "moderate":
                            recov_admissible = (
                                Interval(Fraction(1), Fraction(2)),
                                recov_interval,
                                Interval(Fraction(2), Fraction(4)),
                            )
                            recov_authorized = AuthorizedRange(
                                l_min=Fraction(1), l_max=Fraction(4),
                                u_min=Fraction(1), u_max=Fraction(4),
                                resolution=Fraction(1, 1),
                            )
                        elif domain_policy == "narrow":
                            recov_admissible = (
                                Interval(Fraction(1), Fraction(2)),
                                recov_interval,
                            )
                            recov_authorized = AuthorizedRange(
                                l_min=Fraction(1), l_max=Fraction(3),
                                u_min=Fraction(1), u_max=Fraction(3),
                                resolution=Fraction(1, 1),
                            )
                        elif domain_policy == "wide-no-fast":
                            # Ablation: compressed recovery [0,1] is disabled.
                            # The authorized lower bound moves from 0 to 1 so
                            # the continuous LP search cannot reintroduce it.
                            recov_admissible = (
                                Interval(Fraction(1), Fraction(2)),
                                recov_interval,
                                Interval(Fraction(2), Fraction(4)),
                                Interval(Fraction(3), Fraction(5)),
                            )
                            lp_l_max = Fraction(5 * n_parts * ops_per_part, 2)
                            recov_authorized = AuthorizedRange(
                                l_min=Fraction(1), l_max=lp_l_max,
                                u_min=Fraction(1), u_max=lp_l_max,
                                resolution=Fraction(1, 2),
                            )
                        elif domain_policy == "wide-no-delayed":
                            # Ablation: delayed recovery beyond [2,4] is
                            # disabled.  The authorized upper bound is capped
                            # at 4 (recovery earliest time cannot exceed the
                            # moderate tier's range) so the LP search cannot
                            # reintroduce the delayed mechanism.
                            recov_admissible = (
                                Interval(Fraction(0), Fraction(1)),
                                Interval(Fraction(1), Fraction(2)),
                                recov_interval,
                                Interval(Fraction(2), Fraction(4)),
                            )
                            lp_l_max = Fraction(5 * n_parts * ops_per_part, 2)
                            cap = Fraction(4)
                            recov_authorized = AuthorizedRange(
                                l_min=Fraction(0), l_max=min(cap, lp_l_max),
                                u_min=Fraction(0), u_max=min(cap, lp_l_max),
                                resolution=Fraction(1, 2),
                            )
                        else:
                            raise ValueError(
                                f"unknown domain_policy: {domain_policy!r} "
                                "(expected 'wide' | 'moderate' | 'narrow' | "
                                "'wide-no-fast' | 'wide-no-delayed')"
                            )
                    elif repair_domain == "extended":
                        # Extended repair authority: 5 intervals at resolution 0.5
                        # Includes fast recovery [0,1] (compressed) and
                        # delayed recovery [3,5] (creates timing separation from
                        # normal path t_trans [1,2], enabling repair success).
                        # Justified by controller_time_grids=0.5 in protocol.
                        recov_admissible = (
                            Interval(Fraction(0), Fraction(1)),    # fast recovery
                            Interval(Fraction(1), Fraction(2)),
                            recov_interval,                         # nominal [1,3]
                            Interval(Fraction(2), Fraction(4)),
                            Interval(Fraction(3), Fraction(5)),    # delayed recovery
                        )
                        # LP-authorized continuous range for t_recov.
                        # The required lower bound scales with total operations
                        # (n_parts × ops_per_part) because multi-part instances
                        # accumulate shared-transition variance across all parts.
                        # Each operation contributes ~2 units of timing variance
                        # (t_trans width 1 + t_proc width ~1); the 2.5× factor
                        # includes a 25% margin for resource-contention effects.
                        lp_l_max = Fraction(5 * n_parts * ops_per_part, 2)
                        recov_authorized = AuthorizedRange(
                            l_min=Fraction(0), l_max=lp_l_max,
                            u_min=Fraction(0), u_max=lp_l_max,
                            resolution=Fraction(1, 2),
                        )
                    else:
                        # Standard: 3 intervals, coarse resolution ~1.0
                        recov_admissible = (
                            Interval(Fraction(1), Fraction(2)),
                            recov_interval,
                            Interval(Fraction(2), Fraction(4)),
                        )
                        recov_authorized = AuthorizedRange(
                            l_min=Fraction(1), l_max=Fraction(4),
                            u_min=Fraction(1), u_max=Fraction(4),
                            resolution=Fraction(1, 1),
                        )
                    add_transition(
                        t_recov,
                        label="",
                        is_fault=False,
                        is_adjustable=True,
                        interval=recov_interval,
                        admissible_intervals=recov_admissible,
                        authorized_range=recov_authorized,
                    )
                    add_arc(fault_place_c, t_recov)
                    add_arc(t_recov, at_place_c)

                # Processing transition (config-scaled interval)
                scaled_lo = max(Fraction(0), proc_lo_base * config_factor)
                scaled_hi = proc_hi_base * config_factor
                if scaled_hi < scaled_lo:
                    scaled_hi = scaled_lo
                # Ensure minimum positive interval (avoid degenerate [0,0])
                if scaled_hi == Fraction(0):
                    scaled_hi = Fraction(1, 10)
                proc_interval_c = Interval(scaled_lo, scaled_hi)

                t_proc = f"t_proc_j{j}_op{k}_m{m}_cfg{c}"
                if is_adj:
                    adm = (
                        Interval(
                            max(Fraction(0), _frac(2.0) * config_factor),
                            _frac(3.0) * config_factor,
                        ),
                        proc_interval_c,
                        Interval(
                            max(Fraction(0), _frac(2.0) * config_factor),
                            _frac(5.0) * config_factor,
                        ),
                    )
                    if proc_interval_c not in adm:
                        adm = (proc_interval_c,) + adm
                    if candidate_density == "fine":
                        # R3-5A §3.3: insert mid-u candidates between the
                        # sorted-by-latest neighbors (same earliest as the
                        # base candidates, same authorized envelope).
                        _sorted = sorted(adm, key=lambda iv: iv.latest)
                        new_adm = [_sorted[0]]
                        for _prev, _nxt in zip(_sorted, _sorted[1:]):
                            _mid_u = Fraction(
                                (_prev.latest + _nxt.latest) / 2
                            ).limit_denominator(1000)
                            new_adm.append(Interval(_prev.earliest, _mid_u))
                            new_adm.append(_nxt)
                        adm = tuple(new_adm)
                    # LP-authorized range: ±50% of nominal (extended domain semantics)
                    w_proc_half = _frac(interval_width_factor) * Fraction(1, 2) * config_factor
                    proc_res = (
                        lp_resolution if lp_resolution is not None
                        else _DENSITY_DEFAULT_RES.get(candidate_density, Fraction(1, 2))
                    )
                    proc_authorized = AuthorizedRange(
                        l_min=max(Fraction(0), proc_interval_c.earliest - w_proc_half),
                        l_max=proc_interval_c.earliest + w_proc_half,
                        u_min=max(Fraction(0), proc_interval_c.latest - w_proc_half),
                        u_max=proc_interval_c.latest + w_proc_half,
                        resolution=proc_res,
                    )
                    add_transition(
                        t_proc,
                        label=f"proc_j{j}_op{k}_m{m}",
                        is_fault=False,
                        is_adjustable=True,
                        interval=proc_interval_c,
                        admissible_intervals=adm,
                        authorized_range=proc_authorized,
                    )
                else:
                    add_transition(
                        t_proc,
                        label=f"proc_j{j}_op{k}_m{m}",
                        is_fault=False,
                        is_adjustable=False,
                        interval=proc_interval_c,
                    )
                # Preset: part at machine in config c
                add_arc(at_place_c, t_proc)
                # Postset: part done, machine cap returned, config returned.
                # Selected operations add an acyclic optional inspection fork,
                # yielding complete observable histories at multiple lengths.
                if proc_idx in inspection_indices:
                    post_place_c = f"p{j}_op{k}_post_m{m}_cfg{c}"
                    add_place(post_place_c)
                    add_arc(t_proc, post_place_c)

                    t_finish = f"t_finish_j{j}_op{k}_m{m}_cfg{c}"
                    add_transition(
                        t_finish,
                        label="",
                        is_fault=False,
                        is_adjustable=False,
                        interval=Interval(Fraction(0), Fraction(0)),
                    )
                    add_arc(post_place_c, t_finish)
                    add_arc(t_finish, end_place)

                    t_inspect = f"t_inspect_j{j}_op{k}_m{m}_cfg{c}"
                    add_transition(
                        t_inspect,
                        label=f"inspect_j{j}_op{k}",
                        is_fault=False,
                        is_adjustable=False,
                        interval=Interval(Fraction(1), Fraction(1)),
                    )
                    add_arc(post_place_c, t_inspect)
                    add_arc(t_inspect, end_place)
                else:
                    add_arc(t_proc, end_place)
                add_arc(t_proc, cap_place)
                add_arc(t_proc, cfg_place)

            proc_idx += 1
            transfer_idx += 1

            # Chain to next op
            if k < ops_per_part:
                next_start = f"p{j}_op{k+1}_start"
                t_chain = f"t_chain_j{j}_op{k}_to_op{k+1}"
                add_transition(
                    t_chain,
                    label="",
                    is_fault=False,
                    is_adjustable=False,
                    interval=Interval(Fraction(0), Fraction(0)),
                )
                add_arc(end_place, t_chain)
                add_arc(t_chain, next_start)
            else:
                # Last op: complete part and return WIP token
                p_done = f"p{j}_done"
                add_place(p_done)
                t_complete = f"t_complete_j{j}"
                add_transition(
                    t_complete,
                    label=f"complete_j{j}",
                    is_fault=False,
                    is_adjustable=False,
                    interval=Interval(Fraction(0), Fraction(0)),
                )
                add_arc(end_place, t_complete)
                add_arc(t_complete, p_done)
                add_arc(t_complete, WIP_PLACE)

    # ---------------------------------------------------------------------------
    # Task completion: all parts done -> task_done_place
    # ---------------------------------------------------------------------------
    add_place("task_done_place")
    add_transition(
        "t_task_done",
        label="task_done",
        is_fault=False,
        is_adjustable=False,
        interval=Interval(Fraction(0), Fraction(0)),
    )
    for j in range(1, n_parts + 1):
        add_arc(f"p{j}_done", "t_task_done")
    add_arc("t_task_done", "task_done_place")

    # ---------------------------------------------------------------------------
    # Completion marking
    # ---------------------------------------------------------------------------
    # At completion: task_done_place=1, wip returned, all machine caps returned,
    # and each machine's cfg token is back in whichever config the last job used.
    # Since configs are selected at job-arrival and returned unchanged by t_proc,
    # each machine m ends with its cfg token in whatever config was chosen for
    # the last job using that machine.  We must enumerate one completion marking
    # per possible final-config combination to allow the SCG to recognise
    # completion.  For n_configs configs and n_used_machines machines this is
    # n_configs^n_used_machines markings — small for pilot parameters.
    import itertools as _itertools
    completion_markings = []
    base = {"task_done_place": 1, "p_wip_capacity": wip_bound}
    # Machine cap tokens are always returned (t_proc returns them unconditionally)
    for m in used_machines_list:
        base[f"p_machine_{m}_cap"] = machine_capacity
    # Enumerate all possible final-config combinations
    config_choices = list(range(1, n_configs + 1))
    for combo in _itertools.product(config_choices, repeat=len(used_machines_list)):
        m_entry = dict(base)
        for m, c in zip(used_machines_list, combo):
            m_entry[f"p_machine_{m}_cfg_{c}"] = 1
        completion_markings.append(m_entry)

    # ---------------------------------------------------------------------------
    # Bounds
    # ---------------------------------------------------------------------------
    L = 2 * (ops_per_part * n_parts) + 2
    # H: based on config-1 (fastest) nominal makespan * 1.5 slack
    # proc_hi for cfg1 (config_factor=1/n_configs) with wf=1: (3+1)*(1/n_configs)
    # For n_configs=1: proc_hi=4; for n_configs=2: proc_hi=2 (cfg1 is faster)
    # Use config-n (slowest) for H to be safe: config_factor=1.0, proc_hi = 3+wf
    nominal_proc_hi = float(proc_hi_base)  # wf-dependent, cfg-n (factor=1.0)
    nominal_transfer = 2.0
    nominal_latest_per_op = nominal_proc_hi + nominal_transfer
    H = nominal_latest_per_op * ops_per_part * n_parts * 1.5

    tpn = TPN(
        places=places,
        transitions=transitions,
        arcs=arcs,
        initial_marking=initial_marking,
        completion_markings=completion_markings,
    )

    return tpn, L, H
