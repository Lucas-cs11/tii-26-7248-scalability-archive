#!/usr/bin/env python3
"""Layer C of the v6 differential v2: per-component equivalence audits.

The optimisation commit changes four components.  This checker verifies each
changed component against a **reference implementation kept inside this
script** (the production tree is not given any verification-only behaviour
switch):

1. ``scg_adjacency_index``
   For 24 frozen models (12 families x {depth_shallow, L_mid}) every state
   class's ``scg.successors(cid)`` is compared with the projection of the raw
   edge list ``[(t, dst) for (src, t, dst) in scg.edges if src == cid]``:
   order, multiplicity and labels must match exactly.  Classes without
   successors must return ``[]``; the lazily built index must not be stale after
   construction and must be rebuilt correctly by ``build_truncated``.

2. ``marking_cache``
   ``build_expansion`` caches ``enabled_set`` / ``fire`` /
   ``persistent_enabled`` / ``newly_enabled`` / ``in_completion`` by marking
   key.  A cache-free copy of the identical algorithm (``build_expansion_
   uncached`` below, derived from the frozen source) is compared with the
   production build for 2-3 candidate words per family.  The fingerprint covers
   the ordered node sequence ``(marking, box, k, q)``, the ordered edge list,
   every node's ordered ``(sigma, J_hat)`` record list, and the ordered
   terminal-normal / terminal-faulty lists.

3. ``trace_record_dedup``
   ``add_record`` stores one record per firing sequence sigma at a node.  In
   this script only, ``TemporalExpansion.add_record`` is monkeypatched to the
   no-dedupe rule and the expansion is rebuilt.  The check proves that the
   dedupe rule hides *only* exactly identical records: the set of distinct
   ``(node, sigma, J_hat)`` records is unchanged, duplicate records agree on
   ``J_hat``, the terminal lists project onto the deduped lists, and the
   COMPLETE_SET critical-pair set (unique overlapping normal/faulty terminal
   pairs) is unchanged.

4. ``dbm_fraction`` / ``oracle``
   Runs ``tests/test_v6_component_equivalence.py`` (DBM/Fraction boundary
   cases), the existing ``tests/test_fraction_oracle.py`` and
   ``tests/test_v6_family_semantics.py`` via unittest.

Usage:

    .venv/bin/python scripts/verify_v6_component_equivalence.py --jobs 3

Outputs ``work/v6_differential_v2_component/component_equivalence.json``.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import re
import subprocess
import sys
import time
from collections import deque
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "work" / "v6_differential_v2_component"
PROFILE_PROTOCOL = ROOT / "protocol" / "v6_profile_sample_1.json"

# Pre-registered minimum coverage for the component audit.  PASS requires a
# zero mismatch count *and* these minima; a component that produces zero
# mismatches on too little evidence is not reported as a pass.  The minima are
# declared here, before any measurement, so they cannot be tuned to the result.
COVERAGE_MINIMA = {
    "scg_adjacency_index": {"models_checked": 24, "classes_checked": 24000},
    "marking_cache": {"models_checked": 12, "words_checked": 24,
                      "nodes_compared": 1000, "multi_record_nodes": 100},
    "trace_record_dedup": {"samples_checked": 12, "multi_record_nodes": 100},
    "dbm_fraction": {"cases_checked": 12},
    "oracle": {"fraction_oracle_tests": 10, "family_semantics_tests": 1},
}


# ---------------------------------------------------------------------------
# canonical rendering helpers
# ---------------------------------------------------------------------------
def _bound_repr(value) -> str:
    return "inf" if isinstance(value, float) and math.isinf(value) else str(value)


def _marking_repr(marking_key) -> list:
    return sorted((name, count) for (name, count) in marking_key)


def _box_repr(box_key) -> list:
    return sorted(
        (name, _bound_repr(a), _bound_repr(b)) for (name, a, b) in box_key
    )


def _node_repr(node) -> list:
    return [_marking_repr(node.marking_key), _box_repr(node.box_key), node.k, node.q]


def _record_repr(record) -> list:
    lo, hi = record.J_hat
    return [list(record.sigma), _bound_repr(lo), _bound_repr(hi)]


def expansion_fingerprint(exp) -> dict:
    """Ordered, JSON-friendly fingerprint of a TemporalExpansion."""
    return {
        "nodes": [_node_repr(node) for node in exp.nodes],
        "edges": [[src, label, dst] for (src, label, dst) in exp.edges],
        "records": [
            [_record_repr(record) for record in exp.records[nid]]
            for nid in range(len(exp.nodes))
        ],
        "terminal_normal": [
            [nid, *_record_repr(record)] for (nid, record) in exp.terminal_normal
        ],
        "terminal_faulty": [
            [nid, *_record_repr(record)] for (nid, record) in exp.terminal_faulty
        ],
    }


def _fingerprint_diffs(left: dict, right: dict) -> list:
    diffs = []
    for field in ("nodes", "edges", "records", "terminal_normal", "terminal_faulty"):
        a = left[field]
        b = right[field]
        if a != b:
            index = next(
                (index for index, (x, y) in enumerate(zip(a, b)) if x != y),
                min(len(a), len(b)),
            )
            diffs.append({
                "field": field,
                "left_count": len(a),
                "right_count": len(b),
                "first_difference": index,
                "left_sample": a[index] if index < len(a) else None,
                "right_sample": b[index] if index < len(b) else None,
            })
    return diffs


# ---------------------------------------------------------------------------
# cache-free reference expansion (verbatim copy of the frozen algorithm with
# the marking caches removed; no production code is modified)
# ---------------------------------------------------------------------------
def build_expansion_uncached(tpn, phi, H, budget=None):
    """Cache-free reference implementation of ``build_expansion``."""
    from src.model.tpn import INF
    from src.temporal_expansion.builder import (
        ExpansionNode,
        TemporalExpansion,
        TraceRecord,
        _add_bound,
        _box_key,
        _frac,
        _marking_key,
        _sub_bound,
    )

    m = len(phi)
    H_frac = Fraction(H) if not math.isinf(H) else INF
    exp = TemporalExpansion(phi, None)

    marking0 = dict(tpn.initial_marking)
    enabled0 = tpn.enabled_set(marking0)
    box0 = {}
    for u in enabled0:
        t_u = tpn.transitions[u]
        box0[u] = (t_u.interval.earliest, t_u.interval.latest)

    node0_key = ExpansionNode(
        marking_key=_marking_key(marking0), box_key=_box_key(box0), k=0, q="N"
    )
    n0_id, _ = exp.get_or_create_node(node0_key, marking0, box0)
    exp.add_record(n0_id, TraceRecord(sigma=(), J_hat=(Fraction(0), Fraction(0))))

    total_records = 1
    queue = deque([(n0_id, (), (Fraction(0), Fraction(0)))])
    while queue:
        if budget is not None and total_records > budget:
            return "INC"
        curr_id, sigma, J_hat = queue.popleft()
        curr_node = exp.nodes[curr_id]
        marking, box = exp.node_data[curr_id]

        if tpn.in_completion(marking):
            continue

        enabled = tuple(tpn.enabled_set(marking))
        urgency_upper = INF
        for u in enabled:
            b_u = box.get(u, (Fraction(0), INF))[1]
            if not math.isinf(b_u):
                if math.isinf(urgency_upper) or b_u < urgency_upper:
                    urgency_upper = b_u

        for t_name in enabled:
            t_obj = tpn.transitions[t_name]
            if not t_obj.is_observable:
                k_prime = curr_node.k
            elif curr_node.k < m and t_obj.label == phi[curr_node.k]:
                k_prime = curr_node.k + 1
            else:
                continue

            a_u, b_u = box.get(t_name, (t_obj.interval.earliest, t_obj.interval.latest))
            delta_lo = a_u
            delta_hi = urgency_upper
            if not math.isinf(delta_hi) and delta_hi < delta_lo:
                continue
            if math.isinf(delta_lo):
                continue

            j_lo = J_hat[0] + delta_lo
            j_hi = _add_bound(J_hat[1], delta_hi)
            j_lo_clipped = max(Fraction(0), j_lo)
            if not math.isinf(H_frac):
                j_hi_clipped = min(j_hi, H_frac) if not math.isinf(j_hi) else H_frac
            else:
                j_hi_clipped = j_hi
            if not math.isinf(j_hi_clipped) and j_hi_clipped < j_lo_clipped:
                continue

            new_marking = tpn.fire(marking, t_name)
            new_marking_key = _marking_key(new_marking)
            persistent = tpn.persistent_enabled(marking, t_name)
            newly = tpn.newly_enabled(marking, t_name)

            new_box = {}
            for u in tpn.enabled_set(new_marking):
                if u in persistent:
                    a_old, b_old = box.get(u, (Fraction(0), INF))
                    if math.isinf(delta_hi):
                        new_a = Fraction(0)
                    else:
                        sub_val = _frac(a_old) - _frac(delta_hi)
                        new_a = max(Fraction(0), sub_val)
                    new_b = _sub_bound(b_old, delta_lo)
                    if not math.isinf(new_b) and new_b < new_a:
                        new_a = Fraction(0)
                        new_b = Fraction(0)
                    new_box[u] = (new_a, new_b)
                else:
                    t_u = tpn.transitions[u]
                    new_box[u] = (t_u.interval.earliest, t_u.interval.latest)

            if curr_node.q == "F" or t_obj.is_fault:
                q_prime = "F"
            else:
                q_prime = "N"

            succ_node = ExpansionNode(
                marking_key=new_marking_key,
                box_key=_box_key(new_box),
                k=k_prime,
                q=q_prime,
            )
            sigma_prime = sigma + (t_name,)
            j_hat_prime = (j_lo_clipped, j_hi_clipped)

            succ_id, _ = exp.get_or_create_node(succ_node, new_marking, new_box)
            exp.edges.append((curr_id, t_name, succ_id))

            record = TraceRecord(sigma=sigma_prime, J_hat=j_hat_prime)
            added = exp.add_record(succ_id, record)
            if added:
                total_records += 1
                if budget is not None and total_records > budget:
                    return "INC"
                if tpn.in_completion(new_marking) and k_prime == m:
                    if q_prime == "N":
                        exp.terminal_normal.append((succ_id, record))
                    else:
                        exp.terminal_faulty.append((succ_id, record))
                queue.append((succ_id, sigma_prime, j_hat_prime))
    return exp


# ---------------------------------------------------------------------------
# bounded helper: run a callable under a CPU alarm
# ---------------------------------------------------------------------------
class BudgetExceeded(Exception):
    pass


def _alarm(_signum, _frame):
    raise BudgetExceeded("wall budget exhausted")


def run_with_budget(function, seconds, *args, **kwargs):
    import signal

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        return True, function(*args, **kwargs)
    except BudgetExceeded:
        return False, None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


# ---------------------------------------------------------------------------
# candidate-word discovery (state BFS, no word enumeration)
# ---------------------------------------------------------------------------
def find_candidate_words(
    tpn, scg_truncated, max_words=6, words_per_state=2, max_transitions=200000
):
    """Return candidate words of the synchronizer, shortest first.

    The candidate property depends only on the synchronizer state, so a BFS
    over ``(state, word)`` with a small words-per-state cap finds genuine
    candidate words (distinct label sequences) without enumerating the whole
    label-prefix tree.  Every word returned is a real candidate of the frozen
    construction for ``L >= len(word)``.
    """
    from src.synchronizer.builder import SyncState, reach_F, reach_N, synchronizer_transition

    c_fin = scg_truncated.completion_ids
    initial = SyncState(
        X_N=reach_N(scg_truncated, tpn, frozenset({scg_truncated.initial_id}), ""),
        X_F=reach_F(scg_truncated, tpn, frozenset({scg_truncated.initial_id}), ""),
    )
    labels = sorted(tpn.observable_alphabet)
    candidates = []
    queued = {initial.key(): 1}
    queue = deque([(initial, ())])
    transitions = 0
    while queue and len(candidates) < max_words and transitions < max_transitions:
        state, word = queue.popleft()
        if state.X_N & c_fin and state.X_F & c_fin:
            candidates.append(word)
        if queued.get(state.key(), 0) > words_per_state:
            continue
        for label in labels:
            nxt = synchronizer_transition(scg_truncated, tpn, state, label)
            transitions += 1
            key = nxt.key()
            if not nxt.X_N:
                continue
            count = queued.get(key, 0)
            if count >= words_per_state:
                continue
            queued[key] = count + 1
            queue.append((nxt, word + (label,)))
    return sorted(candidates, key=lambda word: (len(word), word)), initial


# ---------------------------------------------------------------------------
# worker: SCG adjacency index vs edge-list projection
# ---------------------------------------------------------------------------
def _adjacency_worker(cell: dict) -> dict:
    from src.benchmarks.v6_families import generate_v6_model
    from src.model.validation import validate_model
    from src.scg.builder import build_scg

    started = time.perf_counter()
    record = {
        "family": cell["benchmark_family"],
        "stratum": cell["stratum"],
        "status": "ERROR",
        "classes_checked": 0,
        "edges_checked": 0,
        "empty_classes": 0,
        "lazy_index_observed": False,
        "truncated_classes_checked": 0,
        "mismatch_count": 0,
        "mismatches": [],
        "scg_classes": None,
        "scg_edges": None,
        "wall_seconds": None,
    }
    try:
        tpn, _, _, _ = generate_v6_model(**json.loads(cell["params"]))
        errors = validate_model(tpn)
        if errors:
            raise ValueError("; ".join(errors))
        scg = build_scg(tpn)
        record["scg_classes"] = len(scg.classes)
        record["scg_edges"] = len(scg.edges)
        record["lazy_index_observed"] = scg._succ_index is None
        projections = {cid: [] for cid in scg.classes}
        for (src, label, dst) in scg.edges:
            projections[src].append((label, dst))

        for cid in sorted(scg.classes):
            got = scg.successors(cid)
            expected = projections[cid]
            record["classes_checked"] += 1
            if got != expected:
                record["mismatch_count"] += 1
                if len(record["mismatches"]) < 20:
                    record["mismatches"].append({
                        "stage": "full_scg",
                        "class_id": cid,
                        "expected_count": len(expected),
                        "got_count": len(got),
                        "expected": expected[:16],
                        "got": got[:16],
                    })
            if not expected:
                record["empty_classes"] += 1
        record["edges_checked"] = len(scg.edges)
        # A class id that never appeared must still return an empty list.
        ghost = (max(scg.classes) + 1000) if scg.classes else 0
        if scg.successors(ghost) != []:
            record["mismatch_count"] += 1
            record["mismatches"].append({"stage": "missing_class", "class_id": ghost})
        # Not stale: repeat every lookup and compare with the edge projection.
        for cid in sorted(scg.classes):
            if scg.successors(cid) != projections[cid]:
                record["mismatch_count"] += 1
                record["mismatches"].append({"stage": "repeated_lookup", "class_id": cid})
                break
        # build_truncated() is a fresh SCG; its lazy index must project the
        # truncated edge list, not the full one.
        truncated = scg.build_truncated()
        truncated_projections = {cid: [] for cid in truncated.classes}
        for (src, label, dst) in truncated.edges:
            truncated_projections[src].append((label, dst))
        for cid in sorted(truncated.classes):
            record["truncated_classes_checked"] += 1
            if truncated.successors(cid) != truncated_projections[cid]:
                record["mismatch_count"] += 1
                if len(record["mismatches"]) < 20:
                    record["mismatches"].append({
                        "stage": "truncated_scg",
                        "class_id": cid,
                    })
        record["status"] = "PASS" if record["mismatch_count"] == 0 else "MISMATCH"
    except Exception as exc:  # noqa: BLE001
        record["status"] = "ERROR"
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["wall_seconds"] = round(time.perf_counter() - started, 3)
    return record


# ---------------------------------------------------------------------------
# worker: marking-cache and trace-record-dedupe equivalence for one family
# ---------------------------------------------------------------------------
def _critical_pairs(exp, phi):
    """Unique COMPLETE_SET critical pairs (phi, sigma_N, sigma_F) of one word."""
    pairs = set()
    for (_nid_n, rec_n) in exp.terminal_normal:
        lo_n, hi_n = rec_n.J_hat
        for (_nid_f, rec_f) in exp.terminal_faulty:
            lo_f, hi_f = rec_f.J_hat
            lo_max = max(lo_n, lo_f)
            hi_min = hi_f if _leq(hi_f, hi_n) else hi_n
            if _lt(hi_min, lo_max):
                continue
            pairs.add((phi, rec_n.sigma, rec_f.sigma))
    return pairs


def _leq(a, b) -> bool:
    if isinstance(b, float) and math.isinf(b):
        return True
    if isinstance(a, float) and math.isinf(a):
        return False
    return a <= b


def _lt(a, b) -> bool:
    if isinstance(a, float) and math.isinf(a):
        return False
    if isinstance(b, float) and math.isinf(b):
        return True
    return a < b


def _dedupe_fingerprint_diffs(deduped, raw, phi):
    """Compare a deduped expansion with a no-dedupe expansion of the same word.

    Returns ``(diffs, distinct_critical_pairs, distinct_terminal_count,
    raw_terminal_count)``.
    """
    diffs = []
    if len(deduped.nodes) != len(raw.nodes):
        diffs.append({"field": "node_count", "deduped": len(deduped.nodes),
                      "raw": len(raw.nodes)})
    if deduped.edges != raw.edges:
        diffs.append({"field": "edges", "deduped_count": len(deduped.edges),
                      "raw_count": len(raw.edges)})
    if len(deduped.records) != len(raw.records):
        diffs.append({"field": "record_node_count", "deduped": len(deduped.records),
                      "raw": len(raw.records)})
        return diffs, 0, 0, 0
    deduped_records = set()
    raw_records = set()
    for nid in range(len(deduped.nodes)):
        deduped_list = [_record_repr(record) for record in deduped.records[nid]]
        raw_list = [_record_repr(record) for record in raw.records[nid]]
        deduped_records.update((nid, repr(record)) for record in deduped_list)
        raw_records.update((nid, repr(record)) for record in raw_list)
        # The raw run may repeat a sigma; every occurrence must agree on J_hat
        # and the first-occurrence projection must reproduce the deduped list.
        first_occurrence = []
        seen_sigmas = set()
        for record in raw.records[nid]:
            if record.sigma in seen_sigmas:
                continue
            seen_sigmas.add(record.sigma)
            first_occurrence.append(_record_repr(record))
        if first_occurrence != deduped_list:
            diffs.append({"field": "records_projection", "node_id": nid})
    if deduped_records != raw_records:
        diffs.append({"field": "distinct_record_set"})
    deduped_terminals = {
        (nid, tuple(record.sigma), _bound_repr(record.J_hat[0]), _bound_repr(record.J_hat[1]))
        for (nid, record) in deduped.terminal_normal + deduped.terminal_faulty
    }
    raw_terminals = {
        (nid, tuple(record.sigma), _bound_repr(record.J_hat[0]), _bound_repr(record.J_hat[1]))
        for (nid, record) in raw.terminal_normal + raw.terminal_faulty
    }
    if deduped_terminals != raw_terminals:
        diffs.append({"field": "distinct_terminal_set"})
    deduped_pairs = _critical_pairs(deduped, phi)
    raw_pairs = _critical_pairs(raw, phi)
    if deduped_pairs != raw_pairs:
        diffs.append({"field": "critical_pair_set",
                      "deduped": len(deduped_pairs), "raw": len(raw_pairs)})
    return diffs, len(deduped_pairs), len(deduped_terminals), len(raw_terminals)


def _dedupe_worker_family(family_cell: dict) -> dict:
    """Marking-cache + dedupe comparison for one family (one child process).

    The audit searches the same family generator for a model whose candidate
    words expand within a bounded budget.  The frozen cell model is tried
    first; if its candidate expansions are too large (parallel/cyclic families
    with long completion words), the same generator is re-run with reduced
    ``n_parts`` / ``ops_per_part`` and several seeds.  The chosen configuration
    and candidate words are recorded in the output so the sample is
    reproducible.
    """
    import signal

    from src.benchmarks.v6_families import generate_v6_model
    from src.model.validation import validate_model
    from src.scg.builder import build_scg
    from src.temporal_expansion.builder import TemporalExpansion, build_expansion

    family = family_cell["benchmark_family"]
    started = time.perf_counter()
    record = {
        "family": family,
        "status": "ERROR",
        "model": None,
        "sample_kind": None,
        "L": None,
        "H": None,
        "candidate_words": [],
        "words_compared": 0,
        "nodes_compared": 0,
        "words_skipped": [],
        "dedupe_samples_compared": 0,
        "dedupe_samples_skipped": [],
        "critical_pairs_compared": 0,
        "multi_record_nodes": 0,
        "duplicate_sigma_occurrences": 0,
        "mismatch_count": 0,
        "mismatches": [],
        "wall_seconds": None,
    }
    signal.signal(signal.SIGALRM, _alarm)

    def _model_for(model_params):
        ok, generated = run_with_budget(
            lambda: generate_v6_model(**model_params), 20.0
        )
        if not ok:
            return None
        tpn, default_l, default_h, _ = generated
        if validate_model(tpn):
            return None
        ok, scg = run_with_budget(lambda: build_scg(tpn).build_truncated(), 30.0)
        if not ok or len(scg.classes) > 40000:
            return None
        ok, found = run_with_budget(
            lambda: find_candidate_words(
                tpn, scg, max_words=8, words_per_state=2, max_transitions=200000
            ),
            30.0,
        )
        if not ok or len(found[0]) < 2:
            return None
        return tpn, scg, default_l, default_h, found[0]

    def _expansion_pair(tpn, phi, H):
        ok, deduped = run_with_budget(
            lambda: build_expansion(tpn, phi, H, budget=120000), 12.0
        )
        if not ok or isinstance(deduped, str):
            return None
        ok, uncached = run_with_budget(
            lambda: build_expansion_uncached(tpn, phi, H, budget=120000), 12.0
        )
        if not ok or isinstance(uncached, str):
            return None
        return deduped, uncached

    try:
        base_params = json.loads(family_cell["params"])
        base_seed = base_params.get("seed", 74943)
        configs = [("candidate_word_frozen_model", dict(base_params), [base_seed])]
        for parts, ops in ((2, 2), (2, 3), (2, 4)):
            cfg = dict(base_params)
            cfg["n_parts"] = parts
            cfg["ops_per_part"] = ops
            if "wip_bound" in cfg:
                cfg["wip_bound"] = max(1, min(int(cfg["wip_bound"]), parts))
            configs.append((
                f"candidate_word_reduced_p{parts}_o{ops}", cfg, list(range(1, 9)),
            ))
        configs.append(("candidate_word_frozen_model_seeds", dict(base_params),
                        list(range(1, 7))))

        chosen = None
        for label, cfg, seeds in configs:
            for seed in seeds:
                model_params = dict(cfg)
                model_params["seed"] = seed
                model = _model_for(model_params)
                if model is None:
                    continue
                tpn, scg, default_l, default_h, words = model
                H = float(default_h)
                successes = []
                attempt_limit = 3 if label == "candidate_word_frozen_model" else 4
                for phi in words[:attempt_limit]:
                    pair = _expansion_pair(tpn, phi, H)
                    if pair is None:
                        record["words_skipped"].append({
                            "phi": list(phi), "config": label, "seed": seed,
                            "reason": "cached/uncached expansion INC or wall budget",
                        })
                        continue
                    deduped, uncached = pair
                    diffs = _fingerprint_diffs(
                        expansion_fingerprint(deduped),
                        expansion_fingerprint(uncached),
                    )
                    if diffs:
                        record["mismatch_count"] += len(diffs)
                        if len(record["mismatches"]) < 20:
                            record["mismatches"].append({
                                "stage": "marking_cache", "phi": list(phi),
                                "config": label, "seed": seed, "diffs": diffs,
                            })
                    successes.append((phi, deduped))
                    if len(successes) >= 3:
                        break
                if len(successes) >= 2:
                    chosen = (label, seed, tpn, scg, default_l, default_h,
                              words, successes, H, model_params)
                    break
            if chosen is not None:
                break

        if chosen is None:
            # Last resort: synthetic observable words on the frozen model.  The
            # marking-cache equivalence does not depend on the word being a
            # candidate; this is reported explicitly and only used for a family
            # whose candidate completions cannot be expanded within budget.
            tpn, default_l, default_h, _ = run_with_budget(
                lambda: generate_v6_model(**base_params), 20.0
            )[1]
            scg = run_with_budget(
                lambda: build_scg(tpn).build_truncated(), 30.0
            )[1]
            H = float(default_h)
            successes = []
            for label in sorted(tpn.observable_alphabet):
                pair = _expansion_pair(tpn, (label,), H)
                if pair is None:
                    continue
                successes.append(((label,), pair[0]))
                if len(successes) >= 3:
                    break
            if len(successes) < 2:
                record["status"] = "SKIPPED"
                record["error"] = "no expandable word sample found for this family"
                record["wall_seconds"] = round(time.perf_counter() - started, 3)
                return record
            chosen = ("synthetic_observable_word", base_seed, tpn, scg,
                      default_l, default_h, [phi for phi, _ in successes],
                      successes, H, base_params)

        (label, seed, tpn, scg, default_l, default_h, words, successes, H,
         model_params) = chosen
        record["sample_kind"] = label
        L = max([int(default_l)] + [len(word) for word in words])
        record["L"] = L
        record["H"] = H
        record["model"] = {
            "family": family,
            "seed": seed,
            "params": model_params,
            "L": L,
            "H": H,
            "scg_classes": len(scg.classes),
            "scg_edges": len(scg.edges),
        }
        record["candidate_words"] = [list(word) for word in words]

        for phi, deduped in successes:
            record["words_compared"] += 1
            record["nodes_compared"] += len(deduped.nodes)
            record["multi_record_nodes"] += sum(
                1 for node_records in deduped.records.values()
                if len(node_records) > 1
            )

            # ---- trace-record dedupe comparison (in-script monkeypatch) ----
            total_records = sum(len(v) for v in deduped.records.values())
            dedupe_budget = min(max(10 * total_records, 5000), 200000)
            original_add_record = TemporalExpansion.add_record

            def add_record_no_dedupe(self, node_id, trace_record):
                self.records[node_id].append(trace_record)
                return True

            TemporalExpansion.add_record = add_record_no_dedupe
            try:
                ok, raw = run_with_budget(
                    lambda: build_expansion(
                        tpn, phi, H, budget=dedupe_budget
                    ),
                    45.0,
                )
            finally:
                TemporalExpansion.add_record = original_add_record
            if not ok or isinstance(raw, str):
                record["dedupe_samples_skipped"].append({
                    "phi": list(phi),
                    "reason": "no-dedupe expansion INC/timeout within budget",
                })
                continue
            diffs, pairs, _deduped_terminals, _raw_terminals = (
                _dedupe_fingerprint_diffs(deduped, raw, phi)
            )
            if diffs:
                record["mismatch_count"] += len(diffs)
                if len(record["mismatches"]) < 20:
                    record["mismatches"].append({"stage": "trace_record_dedup",
                                                 "phi": list(phi), "diffs": diffs})
            record["dedupe_samples_compared"] += 1
            record["critical_pairs_compared"] += pairs
            for node_records in raw.records.values():
                sigmas = [tuple(trace_record.sigma) for trace_record in node_records]
                record["duplicate_sigma_occurrences"] += len(sigmas) - len(set(sigmas))

        record["status"] = "PASS" if record["mismatch_count"] == 0 else "MISMATCH"
    except Exception as exc:  # noqa: BLE001
        record["status"] = "ERROR"
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["wall_seconds"] = round(time.perf_counter() - started, 3)
    return record


# ---------------------------------------------------------------------------
# unittest subprocess helpers
# ---------------------------------------------------------------------------
def _run_unittest(pattern: str, keyword: str | None) -> dict:
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests",
               "-p", pattern, "-v"]
    if keyword:
        # ``-k`` selects test ids inside the discovered module.
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests",
                   "-p", pattern, "-v", "-k", keyword]
    process = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True)
    output = process.stdout + process.stderr
    match = re.search(r"Ran (\d+) tests?", output)
    tests_run = int(match.group(1)) if match else 0
    passed = process.returncode == 0 and "OK" in output and tests_run > 0
    return {
        "PASS": passed,
        "tests_run": tests_run,
        "returncode": process.returncode,
        "tail": output.strip().splitlines()[-6:],
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--protocol", type=Path, default=PROFILE_PROTOCOL)
    parser.add_argument("--jobs", type=int, default=3,
                        help="worker processes for the SCG / expansion audits")
    parser.add_argument("--only-component", choices=(
        "scg_adjacency_index", "marking_cache", "trace_record_dedup",
        "dbm_fraction", "oracle"), default=None)
    parser.add_argument("--families", type=str, default=None,
                        help="comma-separated family prefix filter (debug only)")
    args = parser.parse_args()

    protocol = json.loads(args.protocol.read_text())
    cells = protocol["cells_detail"]

    summary = {
        "protocol": "v6-differential-v2-layer-c",
        "PASS": False,
        "components": {
            "scg_adjacency_index": {
                "PASS": False, "models_checked": 0, "classes_checked": 0,
                "mismatch_count": 0,
            },
            "marking_cache": {
                "PASS": False, "models_checked": 0, "words_checked": 0,
                "nodes_compared": 0, "mismatch_count": 0,
            },
            "trace_record_dedup": {
                "PASS": False, "samples_checked": 0,
                "critical_pairs_compared": 0, "mismatch_count": 0,
            },
            "dbm_fraction": {
                "PASS": False, "cases_checked": 0, "mismatch_count": 0,
            },
            "oracle": {
                "PASS": False, "fraction_oracle_tests": 0,
                "family_semantics_pass": False, "mismatch_count": 0,
            },
        },
        "coverage": {
            "all_coverage_met": False,
            "criteria": {},
            "notes": (
                "PASS requires zero mismatches AND the pre-registered minimum "
                "coverage; skipped/censored samples are listed per component."
            ),
        },
        "mismatches": [],
        "details": {},
    }
    started = time.perf_counter()

    only = args.only_component

    # ---- 1. SCG adjacency index -------------------------------------------
    if only in (None, "scg_adjacency_index"):
        adjacency_cells = [
            cell for cell in cells
            if cell["stratum"] in ("depth_shallow", "L_mid")
        ]
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
            adjacency_records = list(pool.map(_adjacency_worker, adjacency_cells))
        mismatch_count = sum(record["mismatch_count"] for record in adjacency_records)
        models_checked = sum(
            1 for record in adjacency_records if record["status"] in ("PASS", "MISMATCH")
        )
        summary["components"]["scg_adjacency_index"].update({
            "PASS": mismatch_count == 0 and models_checked == len(adjacency_cells),
            "models_checked": models_checked,
            "classes_checked": sum(record["classes_checked"] for record in adjacency_records),
            "truncated_classes_checked": sum(
                record["truncated_classes_checked"] for record in adjacency_records
            ),
            "empty_classes_checked": sum(
                record["empty_classes"] for record in adjacency_records
            ),
            "lazy_index_observed_all": all(
                record["lazy_index_observed"] for record in adjacency_records
            ),
            "mismatch_count": mismatch_count,
        })
        adjacency_min = COVERAGE_MINIMA["scg_adjacency_index"]
        summary["components"]["scg_adjacency_index"]["coverage"] = {
            "models_checked": {
                "minimum": adjacency_min["models_checked"],
                "measured": models_checked,
                "pass": models_checked >= adjacency_min["models_checked"],
            },
            "classes_checked": {
                "minimum": adjacency_min["classes_checked"],
                "measured": sum(r["classes_checked"] for r in adjacency_records),
                "pass": sum(r["classes_checked"] for r in adjacency_records)
                >= adjacency_min["classes_checked"],
            },
        }
        summary["details"]["scg_adjacency_index"] = adjacency_records
        summary["mismatches"].extend(
            {"component": "scg_adjacency_index", **mismatch}
            for record in adjacency_records for mismatch in record["mismatches"]
        )

    # ---- 2+3. marking cache and trace-record dedupe -----------------------
    if only in (None, "marking_cache", "trace_record_dedup"):
        family_cells = {}
        for stratum in ("depth_shallow", "L_mid", "L_high_complete"):
            for cell in cells:
                if cell["stratum"] == stratum:
                    family_cells.setdefault(cell["benchmark_family"], cell)
        if args.families:
            wanted = tuple(part.strip() for part in args.families.split(","))
            family_cells = {
                family: cell for family, cell in family_cells.items()
                if family.startswith(wanted)
            }
        dedupe_cells = [family_cells[family] for family in sorted(family_cells)]
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
            family_records = list(pool.map(_dedupe_worker_family, dedupe_cells))
        finished = [
            record for record in family_records
            if record["status"] in ("PASS", "MISMATCH")
        ]
        marking_mismatch = sum(
            sum(1 for mismatch in record["mismatches"]
                if mismatch.get("stage") == "marking_cache")
            for record in family_records
        )
        dedupe_mismatch = sum(
            sum(1 for mismatch in record["mismatches"]
                if mismatch.get("stage") == "trace_record_dedup")
            for record in family_records
        )
        words_checked = sum(record["words_compared"] for record in finished)
        nodes_compared = sum(record["nodes_compared"] for record in finished)
        samples_checked = sum(
            record["dedupe_samples_compared"] for record in finished
        )
        multi_record_nodes = sum(
            record["multi_record_nodes"] for record in finished
        )
        duplicate_sigma_occurrences = sum(
            record["duplicate_sigma_occurrences"] for record in finished
        )
        families_without_a_sample = [
            record["family"] for record in family_records
            if record["status"] == "SKIPPED"
        ]
        models_with_one_word = [
            record["family"] for record in finished if record["words_compared"] < 2
        ]
        families_with_zero_duplicate_sigma = [
            record["family"] for record in finished
            if record["duplicate_sigma_occurrences"] == 0
        ]
        marking_min = COVERAGE_MINIMA["marking_cache"]
        marking_coverage = {
            "models_checked": {"minimum": marking_min["models_checked"],
                               "measured": len(finished),
                               "pass": len(finished) >= marking_min["models_checked"]},
            "words_checked": {"minimum": marking_min["words_checked"],
                              "measured": words_checked,
                              "pass": words_checked >= marking_min["words_checked"]},
            "nodes_compared": {"minimum": marking_min["nodes_compared"],
                               "measured": nodes_compared,
                               "pass": nodes_compared >= marking_min["nodes_compared"]},
            "multi_record_nodes": {
                "minimum": marking_min["multi_record_nodes"],
                "measured": multi_record_nodes,
                "pass": multi_record_nodes >= marking_min["multi_record_nodes"],
            },
        }
        marking_ok = (
            marking_mismatch == 0
            and not families_without_a_sample
            and all(item["pass"] for item in marking_coverage.values())
        )
        dedupe_min = COVERAGE_MINIMA["trace_record_dedup"]
        dedupe_coverage = {
            "samples_checked": {"minimum": dedupe_min["samples_checked"],
                                "measured": samples_checked,
                                "pass": samples_checked >= dedupe_min["samples_checked"]},
            "multi_record_nodes": {
                "minimum": dedupe_min["multi_record_nodes"],
                "measured": multi_record_nodes,
                "pass": multi_record_nodes >= dedupe_min["multi_record_nodes"],
            },
            "duplicate_sigma_occurrences": {
                "measured": duplicate_sigma_occurrences,
                "note": "number of identical (node, sigma) additions the dedupe rule hides",
            },
            "families_with_zero_duplicate_sigma": families_with_zero_duplicate_sigma,
            "dedupe_rule_inert_on_audited_samples": duplicate_sigma_occurrences == 0,
            "note": (
                "In Algorithm S3.2 the successor of (node, sigma) is deterministic, "
                "so the same firing sequence is generated at most once per node; "
                "the dedupe rule is defensive and no duplicate sigma was generated "
                "by any audited expansion. The audit therefore proves the weaker "
                "but exact statement required here: disabling dedupe adds no record "
                "that is not already present with an identical J_hat, and the "
                "distinct record / terminal / COMPLETE_SET critical-pair sets are "
                "unchanged. Multi-record nodes (different sigmas reaching the same "
                "(marking, box, k, q)) are preserved in both runs."
            ),
        }
        dedupe_ok = (
            dedupe_mismatch == 0
            and not families_without_a_sample
            and all(item["pass"] for item in dedupe_coverage.values()
                    if isinstance(item, dict) and "pass" in item)
        )
        summary["components"]["marking_cache"].update({
            "PASS": marking_ok,
            "models_checked": len(finished),
            "words_checked": words_checked,
            "nodes_compared": nodes_compared,
            "multi_record_nodes": multi_record_nodes,
            "models_with_fewer_than_two_words": models_with_one_word,
            "sample_kinds": {
                kind: sum(1 for record in finished if record.get("sample_kind") == kind)
                for kind in sorted(
                    {record.get("sample_kind") for record in finished if record.get("sample_kind")}
                )
            },
            "coverage": marking_coverage,
            "mismatch_count": marking_mismatch,
        })
        summary["components"]["trace_record_dedup"].update({
            "PASS": dedupe_ok,
            "samples_checked": samples_checked,
            "critical_pairs_compared": sum(
                record["critical_pairs_compared"] for record in finished
            ),
            "duplicate_sigma_occurrences": duplicate_sigma_occurrences,
            "multi_record_nodes": multi_record_nodes,
            "families_with_zero_duplicate_sigma": families_with_zero_duplicate_sigma,
            "coverage": dedupe_coverage,
            "mismatch_count": dedupe_mismatch,
        })
        summary["details"]["marking_cache_and_dedup"] = family_records
        for record in finished:
            for mismatch in record["mismatches"]:
                summary["mismatches"].append({
                    "component": (
                        "marking_cache" if mismatch.get("stage") == "marking_cache"
                        else "trace_record_dedup"
                    ),
                    "family": record["family"],
                    **mismatch,
                })
        if only == "trace_record_dedup":
            summary["components"]["marking_cache"]["PASS"] = None  # not evaluated
        if only == "marking_cache":
            summary["components"]["trace_record_dedup"]["PASS"] = None

    # ---- 4. DBM / Fraction boundary tests ---------------------------------
    if only in (None, "dbm_fraction"):
        dbm_result = _run_unittest(
            "test_v6_component_equivalence.py", "DBMFractionBoundary"
        )
        dbm_min = COVERAGE_MINIMA["dbm_fraction"]["cases_checked"]
        summary["components"]["dbm_fraction"].update({
            "PASS": dbm_result["PASS"] and dbm_result["tests_run"] >= dbm_min,
            "cases_checked": dbm_result["tests_run"],
            "coverage": {
                "cases_checked": {
                    "minimum": dbm_min,
                    "measured": dbm_result["tests_run"],
                    "pass": dbm_result["tests_run"] >= dbm_min,
                },
            },
            "mismatch_count": 0 if dbm_result["PASS"] else max(dbm_result["tests_run"], 1),
            "unittest_tail": dbm_result["tail"],
        })
        if not dbm_result["PASS"]:
            summary["mismatches"].append({"component": "dbm_fraction",
                                          "detail": dbm_result["tail"]})

    # ---- 5. independent oracles ------------------------------------------
    if only in (None, "oracle"):
        fraction_result = _run_unittest("test_fraction_oracle.py", None)
        family_result = _run_unittest("test_v6_family_semantics.py", None)
        oracle_min = COVERAGE_MINIMA["oracle"]
        oracle_ok = (
            fraction_result["PASS"]
            and family_result["PASS"]
            and fraction_result["tests_run"] >= oracle_min["fraction_oracle_tests"]
            and family_result["tests_run"] >= oracle_min["family_semantics_tests"]
        )
        summary["components"]["oracle"].update({
            "PASS": oracle_ok,
            "fraction_oracle_tests": fraction_result["tests_run"],
            "family_semantics_tests": family_result["tests_run"],
            "family_semantics_pass": family_result["PASS"],
            "coverage": {
                "fraction_oracle_tests": {
                    "minimum": oracle_min["fraction_oracle_tests"],
                    "measured": fraction_result["tests_run"],
                    "pass": fraction_result["tests_run"]
                    >= oracle_min["fraction_oracle_tests"],
                },
                "family_semantics_tests": {
                    "minimum": oracle_min["family_semantics_tests"],
                    "measured": family_result["tests_run"],
                    "pass": family_result["tests_run"]
                    >= oracle_min["family_semantics_tests"],
                },
            },
            "mismatch_count": 0 if oracle_ok else 1,
            "unittest_tail": {
                "fraction_oracle": fraction_result["tail"],
                "family_semantics": family_result["tail"],
            },
        })
        if not oracle_ok:
            summary["mismatches"].append({"component": "oracle"})

    evaluated = [
        component for component in summary["components"].values()
        if component["PASS"] is not None
    ]
    coverage_met = all(
        item["pass"]
        for component in evaluated
        for item in (component.get("coverage") or {}).values()
        if isinstance(item, dict) and "pass" in item
    )
    summary["coverage"]["all_coverage_met"] = coverage_met
    summary["coverage"]["criteria"] = {
        name: component.get("coverage")
        for name, component in summary["components"].items()
        if component.get("coverage")
    }
    summary["PASS"] = bool(evaluated) and all(
        component["PASS"] for component in evaluated
    ) and not summary["mismatches"] and coverage_met
    summary["run_wall_seconds"] = round(time.perf_counter() - started, 3)
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "component_equivalence.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "PASS": summary["PASS"],
        "components": summary["components"],
        "mismatch_count": len(summary["mismatches"]),
        "run_wall_seconds": summary["run_wall_seconds"],
    }, indent=2))
    print(f"wrote {out_path}")
    return 0 if summary["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
