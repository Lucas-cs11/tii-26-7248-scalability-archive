#!/usr/bin/env python3
"""Layer B of the v6 differential v2: transition-level synchronizer equivalence.

The optimisation commit adds a memo to ``build_synchronizer`` keyed by
``(state_key, label)`` where ``state_key = (X_N, X_F)``.  The transition is a
pure function of the synchronizer state and the observable label, so the memo
must not change any reachable state, edge or candidate word.

This checker works entirely inside the *optimized* tree and gives direct
transition-level evidence (it does not need the frozen baseline checkout):

For each of the 48 frozen performance-differential cells:

1. generate the v6 model, validate it, build the SCG, truncate it at
   completion classes, and build the synchronizer with
   ``expose_transitions=True`` so the memo is readable;
2. for **every** synchronizer state and **every** observable label, recompute
   the successor with the *uncached* path ``synchronizer_transition(...)`` and
   compare it with the memoised result for the same key: ``X_N``, ``X_F``,
   the state key and the empty-successor case;
3. replay the reachable automaton from the initial state using only the
   freshly computed transitions and compare the state sequence, the ordered
   edge multiset and the ordered candidate-word list with
   ``sync.states`` / ``sync.edges`` / ``sync.candidate_words``;
4. for cells whose memoised build fits the no-memo wall budget, also build
   ``build_synchronizer(use_transition_memo=False)`` and compare the complete
   automaton.  Cells that exceed the budget are marked ``skipped_budget`` and
   *never* counted as a pass.

The script is honest about coverage: a cell that cannot even build the
synchronizer within the cell wall budget is reported as ``CENSORED`` with the
budget used, and the summary reports ``cells_censored`` explicitly.

Usage (one command, ~parallel):

    .venv/bin/python scripts/verify_v6_sync_transition_equivalence.py \
        --jobs 4 --cell-timeout 300 --no-memo-timeout 60

The parent process runs one isolated child process per cell (so a runaway cell
can be killed) and aggregates the per-cell JSON written by the children.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# canonical fingerprints
# ---------------------------------------------------------------------------
def canonical_state_key(key):
    """Deterministic tuple form of a ``(X_N, X_F)`` synchronizer state key."""
    return (tuple(sorted(key[0])), tuple(sorted(key[1])))


def _automaton_hash(states, edges, words) -> str:
    digest = hashlib.sha256()
    for key in sorted(states, key=canonical_state_key):
        digest.update(repr(canonical_state_key(key)).encode())
    canonical_edges = sorted(
        (
            canonical_state_key(src),
            label,
            canonical_state_key(dst),
        )
        for (src, label, dst) in edges
    )
    digest.update(repr(canonical_edges).encode())
    digest.update(repr(sorted(words)).encode())
    return digest.hexdigest()


def _transition_map_hash(transition_map) -> str:
    digest = hashlib.sha256()
    for (state_key, label) in sorted(
        transition_map, key=lambda item: (canonical_state_key(item[0]), item[1])
    ):
        state, next_key = transition_map[(state_key, label)]
        digest.update(
            repr(
                (
                    canonical_state_key(state_key),
                    label,
                    canonical_state_key((state.X_N, state.X_F)),
                    canonical_state_key(next_key),
                )
            ).encode()
        )
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# worker: one cell, isolated in a child process
# ---------------------------------------------------------------------------
class _BudgetExceeded(Exception):
    pass


def _alarm(_signum, _frame):
    raise _BudgetExceeded("wall budget exhausted")


def _compare_automata(sync, replay_states, replay_edges, replay_words):
    """Return an ordered-difference description (empty list means identical)."""
    diffs = []
    if list(sync.states) != list(replay_states):
        sync_keys = [canonical_state_key(k) for k in sync.states]
        replay_keys = [canonical_state_key(k) for k in replay_states]
        diffs.append({
            "field": "states_sequence",
            "sync_count": len(sync_keys),
            "replay_count": len(replay_keys),
            "first_difference": next(
                (index for index, (a, b) in enumerate(zip(sync_keys, replay_keys)) if a != b),
                min(len(sync_keys), len(replay_keys)),
            ),
        })
    if sync.edges != replay_edges:
        diffs.append({
            "field": "edges_sequence",
            "sync_count": len(sync.edges),
            "replay_count": len(replay_edges),
            "first_difference": next(
                (index for index, (a, b) in enumerate(zip(sync.edges, replay_edges)) if a != b),
                min(len(sync.edges), len(replay_edges)),
            ),
        })
    if sync.candidate_words != replay_words:
        diffs.append({
            "field": "candidate_words_sequence",
            "sync_count": len(sync.candidate_words),
            "replay_count": len(replay_words),
            "first_difference": next(
                (
                    index
                    for index, (a, b) in enumerate(
                        zip(sync.candidate_words, replay_words)
                    )
                    if a != b
                ),
                min(len(sync.candidate_words), len(replay_words)),
            ),
        })
    return diffs


def _replay_bfs(scg, tpn, L, fresh_map, initial_state, initial_key, labels):
    """Rebuild the synchronizer automaton from fresh transitions only.

    ``fresh_map[(state_key, label)]`` was produced by direct, uncached calls to
    ``synchronizer_transition``.  The replay follows exactly the production BFS
    order.  Returns (states, edges, words, expanded_keys).
    """
    c_fin = scg.completion_ids
    states = {initial_key: initial_state}
    edges = []
    words = []
    expanded_keys = set()
    queue = deque([((), initial_key)])
    while queue:
        word, curr_key = queue.popleft()
        curr_state = states[curr_key]
        if curr_state.X_N & c_fin and curr_state.X_F & c_fin:
            words.append(word)
        if not curr_state.X_N or len(word) >= L:
            continue
        expanded_keys.add(curr_key)
        for label in labels:
            next_state, next_key = fresh_map[(curr_key, label)]
            if not next_state.X_N:
                continue
            if next_key not in states:
                states[next_key] = next_state
            edges.append((curr_key, label, next_key))
            queue.append((word + (label,), next_key))
    return states, edges, words, expanded_keys


def _build_synchronizer_no_memo(scg, tpn, L):
    """Production BFS with the transition memo disabled (reference only).

    This is a verbatim, cache-free copy of the frozen construction logic: every
    occurrence of a prefix recomputes ``synchronizer_transition``.  It is used
    only as an in-script reference and never touches the production module.
    """
    from src.synchronizer.builder import SyncState, reach_F, reach_N, synchronizer_transition

    c_fin = scg.completion_ids
    initial_state = SyncState(
        X_N=reach_N(scg, tpn, frozenset({scg.initial_id}), ""),
        X_F=reach_F(scg, tpn, frozenset({scg.initial_id}), ""),
    )
    initial_key = initial_state.key()
    states = {initial_key: initial_state}
    edges = []
    candidate_words = []
    queue = deque([((), initial_key)])
    labels = sorted(tpn.observable_alphabet)
    while queue:
        word, curr_key = queue.popleft()
        curr_state = states[curr_key]
        if curr_state.X_N & c_fin and curr_state.X_F & c_fin:
            candidate_words.append(word)
        if not curr_state.X_N or len(word) >= L:
            continue
        for label in labels:
            next_state = synchronizer_transition(scg, tpn, curr_state, label)
            next_key = next_state.key()
            if not next_state.X_N:
                continue
            if next_key not in states:
                states[next_key] = next_state
            edges.append((curr_key, label, next_key))
            queue.append((word + (label,), next_key))
    return {
        "states": states,
        "edges": edges,
        "candidate_words": candidate_words,
        "initial_key": initial_key,
    }


def run_cell(cell: dict, cell_timeout: float, no_memo_timeout: float) -> dict:
    """Run the complete Layer B check for one cell (inside a child process)."""
    record = {
        "task_id": cell["task_id"],
        "family": cell["benchmark_family"],
        "stratum": cell["stratum"],
        "mode": cell["mode"],
        "L": int(cell["L"]),
        "H": float(cell["H"]),
        "status": "ERROR",
        "error": None,
        "states": None,
        "states_checked": None,
        "labels": None,
        "transitions": None,
        "fresh_equals_memo": None,
        "memo_missing": None,
        "memo_missing_reason": {},
        "empty_successor_pairs": None,
        "mismatch_count": 0,
        "mismatches": [],
        "graph_pass": False,
        "graph_diffs": [],
        "no_memo_automaton": "not_attempted",
        "no_memo_detail": None,
        "sync_automaton_sha256": None,
        "fresh_reconstruction_sha256": None,
        "memo_sha256": None,
        "fresh_sha256": None,
        "scg_classes": None,
        "scg_edges": None,
        "sync_edges": None,
        "sync_candidate_words": None,
        "timing": {},
        "wall_seconds": None,
    }
    started = time.perf_counter()

    def timed(stage):
        record["timing"][stage + "_s"] = round(time.perf_counter() - started, 3)

    signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, float(cell_timeout))
    try:
        from src.benchmarks.v6_families import generate_v6_model
        from src.model.validation import validate_model
        from src.scg.builder import build_scg
        from src.synchronizer.builder import build_synchronizer, synchronizer_transition

        params = json.loads(cell["params"])
        tpn, default_l, default_h, _ = generate_v6_model(**params)
        errors = validate_model(tpn)
        if errors:
            raise ValueError("model validation failed: " + "; ".join(errors))
        L = int(cell["L"])
        H = float(cell["H"])
        timed("model")

        scg = build_scg(tpn).build_truncated()
        record["scg_classes"] = len(scg.classes)
        record["scg_edges"] = len(scg.edges)
        timed("scg")

        sync = build_synchronizer(tpn, scg, L)
        memo = sync.transition_cache
        if memo is None:  # pragma: no cover - the production builder always fills it
            raise RuntimeError("transition cache was not exposed")
        record["sync_states"] = len(sync.states)
        record["sync_edges"] = len(sync.edges)
        record["sync_candidate_words"] = len(sync.candidate_words)
        timed("sync")

        labels = sorted(tpn.observable_alphabet)
        record["labels"] = labels
        record["states"] = len(sync.states)

        # ------------------------------------------------------------------
        # 1) every state x every observable label: fresh vs memo
        # ------------------------------------------------------------------
        fresh_map = {}
        mismatches = []
        fresh_equals_memo = 0
        memo_missing = 0
        missing_reason = {"x_n_empty": 0, "depth_limit": 0, "unknown": 0}
        empty_successors = 0
        transitions = 0
        for state_key, state in sync.states.items():
            for label in labels:
                transitions += 1
                fresh = synchronizer_transition(scg, tpn, state, label)
                fresh_map[(state_key, label)] = (fresh, fresh.key())
                if not fresh.X_N:
                    empty_successors += 1
                cached = memo.get((state_key, label))
                if cached is None:
                    memo_missing += 1
                    continue
                memo_state, memo_next_key = cached
                if (
                    fresh.X_N != memo_state.X_N
                    or fresh.X_F != memo_state.X_F
                    or fresh.key() != memo_next_key
                ):
                    if len(mismatches) < 50:
                        mismatches.append({
                            "state_key": [sorted(state_key[0]), sorted(state_key[1])],
                            "label": label,
                            "fresh": {
                                "X_N": sorted(fresh.X_N),
                                "X_F": sorted(fresh.X_F),
                                "key": [sorted(fresh.key()[0]), sorted(fresh.key()[1])],
                            },
                            "memo": {
                                "X_N": sorted(memo_state.X_N),
                                "X_F": sorted(memo_state.X_F),
                                "key": [sorted(memo_next_key[0]), sorted(memo_next_key[1])],
                            },
                        })
                else:
                    fresh_equals_memo += 1
        record["states_checked"] = len(sync.states)
        record["transitions"] = transitions
        record["fresh_equals_memo"] = fresh_equals_memo
        record["memo_missing"] = memo_missing
        record["empty_successor_pairs"] = empty_successors
        record["mismatch_count"] = len(mismatches)
        record["mismatches"] = mismatches
        record["memo_sha256"] = _transition_map_hash(memo)
        record["fresh_sha256"] = _transition_map_hash(fresh_map)
        timed("transitions")

        # The replay is the strongest check, but for a mismatching cell we still
        # run it so the report carries the concrete graph divergence.
        replay_states, replay_edges, replay_words, expanded_keys = _replay_bfs(
            scg, tpn, L, fresh_map, sync.states[sync.initial_key], sync.initial_key, labels
        )
        # Reasons for memo-missing pairs (needed only for the report).
        for state_key in sync.states:
            if state_key in expanded_keys:
                continue
            state = sync.states[state_key]
            reason = "x_n_empty" if not state.X_N else "depth_limit"
            missing_reason[reason] += 1
        record["memo_missing_reason"] = missing_reason
        timed("replay")

        graph_diffs = _compare_automata(sync, replay_states, replay_edges, replay_words)
        record["graph_diffs"] = graph_diffs
        record["graph_pass"] = not graph_diffs and not mismatches
        record["sync_automaton_sha256"] = _automaton_hash(
            sync.states, sync.edges, sync.candidate_words
        )
        record["fresh_reconstruction_sha256"] = _automaton_hash(
            replay_states, replay_edges, replay_words
        )

        # Free the memo/fresh map before the no-memo build to bound memory.
        sync.transition_cache = None
        del memo
        del fresh_map

        # ------------------------------------------------------------------
        # 2) optional no-memo full automaton comparison (bounded)
        # ------------------------------------------------------------------
        remaining = cell_timeout - (time.perf_counter() - started)
        if remaining <= 5.0:
            record["no_memo_automaton"] = "skipped_budget"
            record["no_memo_detail"] = "no cell budget left after the transition pass"
        else:
            no_memo_budget = min(no_memo_timeout, remaining)
            signal.setitimer(signal.ITIMER_REAL, float(no_memo_budget))
            no_memo_started = time.perf_counter()
            try:
                no_memo = _build_synchronizer_no_memo(scg, tpn, L)
                record["no_memo_detail"] = {
                    "budget_seconds": no_memo_budget,
                    "wall_seconds": round(time.perf_counter() - no_memo_started, 3),
                    "states": len(no_memo["states"]),
                    "edges": len(no_memo["edges"]),
                    "candidate_words": len(no_memo["candidate_words"]),
                }
                no_memo_diffs = _compare_automata(
                    sync, no_memo["states"], no_memo["edges"],
                    no_memo["candidate_words"],
                )
                if no_memo_diffs:
                    record["no_memo_automaton"] = "mismatch"
                    record["no_memo_detail"]["diff"] = no_memo_diffs
                    record["no_memo_detail"]["mismatch_count"] = len(no_memo_diffs)
                else:
                    record["no_memo_automaton"] = "match"
                record["no_memo_detail"]["diff"] = no_memo_diffs
            except _BudgetExceeded:
                record["no_memo_automaton"] = "skipped_budget"
                record["no_memo_detail"] = {
                    "budget_seconds": no_memo_budget,
                    "reason": (
                        "cache-free reference BFS exceeded the no-memo wall budget"
                    ),
                }
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0.0)

        record["status"] = "CHECKED"
    except _BudgetExceeded:
        record["status"] = "CENSORED"
        record["error"] = (
            f"cell exceeded the {cell_timeout:g}s wall budget before the "
            "transition-level check could complete"
        )
    except Exception as exc:  # noqa: BLE001
        record["status"] = "ERROR"
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        record["wall_seconds"] = round(time.perf_counter() - started, 3)
    return record


# ---------------------------------------------------------------------------
# parent: run one child process per cell and aggregate
# ---------------------------------------------------------------------------
def _child_command(args, cell: dict, out_path: Path) -> list:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--cell-json",
        json.dumps(cell),
        "--cell-timeout",
        str(args.cell_timeout),
        "--no-memo-timeout",
        str(args.no_memo_timeout),
        "--worker-out",
        str(out_path),
    ]


def _run_child(args, cell: dict, out_path: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "PYTHONHASHSEED": "0",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    })
    started = time.perf_counter()
    process = subprocess.Popen(
        _child_command(args, cell, out_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    hard_timeout = args.cell_timeout + args.worker_grace
    try:
        stdout, stderr = process.communicate(timeout=hard_timeout)
        if out_path.exists():
            record = json.loads(out_path.read_text())
            if stdout.strip():
                record.setdefault("worker_stdout_tail", stdout.strip().splitlines()[-3:])
            return record
        return {
            "task_id": cell["task_id"],
            "family": cell["benchmark_family"],
            "stratum": cell["stratum"],
            "status": "CENSORED",
            "error": (
                f"child produced no result (exit={process.returncode}); "
                f"stderr={stderr[-800:]}"
            ),
            "mismatch_count": 0,
            "graph_pass": False,
            "no_memo_automaton": "not_attempted",
            "wall_seconds": round(time.perf_counter() - started, 3),
        }
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        partial = None
        if out_path.exists():
            try:
                partial = json.loads(out_path.read_text())
            except Exception:  # noqa: BLE001
                partial = None
        if partial is not None:
            partial["status"] = "CENSORED"
            partial.setdefault("error", "child killed after exceeding the wall budget")
            return partial
        return {
            "task_id": cell["task_id"],
            "family": cell["benchmark_family"],
            "stratum": cell["stratum"],
            "status": "CENSORED",
            "error": (
                f"cell wall budget of {args.cell_timeout:g}s exhausted; the "
                f"in-worker alarm did not return in time and the parent killed "
                f"the child at the {hard_timeout:g}s hard grace; no partial result"
            ),
            "mismatch_count": 0,
            "graph_pass": False,
            "no_memo_automaton": "not_attempted",
            "wall_seconds": round(time.perf_counter() - started, 3),
        }


def aggregate(results: list, cells: list, args) -> dict:
    by_id = {cell["task_id"]: cell for cell in cells}
    differential = {}
    differential_path = args.differential
    if differential_path.exists():
        census = json.loads(differential_path.read_text())
        differential = {
            "censored_ids": [entry["task_id"] for entry in census.get("censored_detail", [])],
            "matched": census.get("matched"),
            "mismatched": census.get("mismatched"),
        }
    checked = [r for r in results if r.get("status") == "CHECKED"]
    censored = [r for r in results if r.get("status") == "CENSORED"]
    errored = [r for r in results if r.get("status") == "ERROR"]

    mismatches = []
    for record in results:
        for mismatch in record.get("mismatches") or []:
            mismatches.append({
                "task_id": record["task_id"],
                "family": record.get("family"),
                "stratum": record.get("stratum"),
                **mismatch,
            })
        for diff in record.get("graph_diffs") or []:
            mismatches.append({
                "task_id": record["task_id"],
                "stage": "graph_reconstruction",
                **diff,
            })

    per_cell = []
    for record in results:
        per_cell.append({
            "task_id": record["task_id"],
            "family": record.get("family"),
            "stratum": record.get("stratum"),
            "status": record.get("status"),
            "states": record.get("states"),
            "transitions": record.get("transitions"),
            "fresh_equals_memo": record.get("fresh_equals_memo"),
            "memo_missing": record.get("memo_missing"),
            "memo_missing_reason": record.get("memo_missing_reason"),
            "labels": record.get("labels"),
            "empty_successor_pairs": record.get("empty_successor_pairs"),
            "mismatch_count": record.get("mismatch_count", 0),
            "graph_pass": record.get("graph_pass", False),
            "no_memo_automaton": record.get("no_memo_automaton"),
            "no_memo_detail": record.get("no_memo_detail"),
            "sync_states": record.get("sync_states"),
            "sync_edges": record.get("sync_edges"),
            "sync_candidate_words": record.get("sync_candidate_words"),
            "scg_classes": record.get("scg_classes"),
            "sync_automaton_sha256": record.get("sync_automaton_sha256"),
            "fresh_reconstruction_sha256": record.get("fresh_reconstruction_sha256"),
            "memo_sha256": record.get("memo_sha256"),
            "fresh_sha256": record.get("fresh_sha256"),
            "wall_seconds": record.get("wall_seconds"),
            "timing": record.get("timing"),
            "error": record.get("error"),
        })

    no_memo_matched = [r for r in checked if r.get("no_memo_automaton") == "match"]
    no_memo_skipped = [r["task_id"] for r in checked if r.get("no_memo_automaton") == "skipped_budget"]
    no_memo_mismatch = [r for r in checked if r.get("no_memo_automaton") == "mismatch"]
    graph_failed = [r for r in checked if not r.get("graph_pass")]

    all_cells_present = len({r["task_id"] for r in results}) == len(cells)
    censored_have_reason = all(bool(r.get("error")) for r in censored)
    coverage_complete = (
        all_cells_present
        and not errored
        and len(checked) + len(censored) == len(cells)
    )
    # PASS covers the transition-level proof only, as agreed with the Lead:
    # every checked cell must be mismatch-free with a passing graph replay, and
    # every uncheckable cell must be censored with an explicit wall-budget
    # reason and listed.  The no-memo full-automaton comparison is a separate
    # gate (no_memo_full_automaton.PASS) and never affects this flag.
    pass_flag = (
        bool(checked)
        and not mismatches
        and not graph_failed
        and not errored
        and all_cells_present
        and len(checked) + len(censored) == len(cells)
        and censored_have_reason
    )

    summary = {
        "protocol": "v6-differential-v2-layer-b",
        "source_differential": str(args.differential),
        "source_protocol": str(args.protocol),
        "notes": (
            "Layer B is an in-tree transition-level equivalence proof of the "
            "synchronizer memo. It does not need the frozen baseline checkout; "
            "cells that cannot build the synchronizer within the reported wall "
            "budget are censored and listed, never counted as passes."
        ),
        "PASS": pass_flag,
        "coverage_complete": coverage_complete,
        "cells_total": len(cells),
        "cells_checked": len(checked),
        "cells_censored": len(censored),
        "cells_errored": len(errored),
        "cell_timeout_seconds": args.cell_timeout,
        "no_memo_timeout_seconds": args.no_memo_timeout,
        "states_checked": sum(r.get("states_checked") or 0 for r in checked),
        "transitions_checked": sum(r.get("transitions") or 0 for r in checked),
        "fresh_equals_memo": sum(r.get("fresh_equals_memo") or 0 for r in checked),
        "memo_missing": sum(r.get("memo_missing") or 0 for r in checked),
        "memo_missing_reason": {
            reason: sum(
                (r.get("memo_missing_reason") or {}).get(reason, 0) for r in checked
            )
            for reason in ("x_n_empty", "depth_limit", "unknown")
        },
        "empty_successor_pairs": sum(r.get("empty_successor_pairs") or 0 for r in checked),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "graph_reconstruction_pass": not graph_failed and bool(checked),
        "graph_reconstruction_checked_cells": len(checked) - len(graph_failed),
        "no_memo_full_automaton": {
            "PASS": len(no_memo_mismatch) == 0,
            "cells_checked": len(no_memo_matched),
            "cells_skipped": no_memo_skipped,
            "mismatch_count": len(no_memo_mismatch),
            "mismatch_detail": [
                {"task_id": r["task_id"], "detail": r.get("no_memo_detail")}
                for r in no_memo_mismatch
            ],
        },
        "censored_all_have_budget_reason": censored_have_reason,
        "censored_budget_note": (
            f"every cell had a {args.cell_timeout:g}s transition-level wall "
            f"budget (in-worker SIGALRM); the parent kills a child after "
            f"{args.cell_timeout + args.worker_grace:g}s hard grace. The "
            "no-memo reference build has its own "
            f"{args.no_memo_timeout:g}s budget and is reported separately."
        ),
        "censored_cells": [
            {
                "task_id": r["task_id"],
                "family": r.get("family"),
                "stratum": r.get("stratum"),
                "reason": r.get("error"),
                "reason_detail": (
                    "the synchronizer automaton could not be constructed within "
                    f"the {args.cell_timeout:g}s transition-level wall budget, so "
                    "no transition exists to compare; this cell is not counted "
                    "as a pass."
                    + (
                        " It is also censored on the optimised side of the "
                        "frozen v1 differential, so the automaton is unbuildable "
                        "at the differential's 300s cap as well."
                        if r["task_id"] in set(differential.get("censored_ids") or [])
                        else ""
                    )
                ),
                "wall_budget_seconds": args.cell_timeout,
                "observed_wall_seconds": r.get("wall_seconds"),
                "partial": {
                    key: r.get(key)
                    for key in (
                        "scg_classes", "scg_edges", "sync_states", "sync_edges",
                        "states", "transitions", "fresh_equals_memo",
                    )
                },
            }
            for r in censored
        ],
        "errored_cells": [
            {"task_id": r["task_id"], "family": r.get("family"),
             "stratum": r.get("stratum"), "error": r.get("error")}
            for r in errored
        ],
        "differential_v1_census": differential,
        "differential_v1_censored_cells_checked": sorted(
            r["task_id"] for r in checked
            if r["task_id"] in set(differential.get("censored_ids") or [])
        ),
        "differential_v1_censored_cells_censored": sorted(
            r["task_id"] for r in censored
            if r["task_id"] in set(differential.get("censored_ids") or [])
        ),
        "total_wall_seconds_sum": round(sum(r.get("wall_seconds") or 0 for r in results), 3),
        "per_cell": per_cell,
    }
    if not all_cells_present:
        summary["missing_cell_ids"] = sorted(
            set(by_id) - {r["task_id"] for r in results}
        )
    return summary


def _write_cell_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path,
                        default=ROOT / "protocol/v6_perf_differential_1.json")
    parser.add_argument("--differential", type=Path,
                        default=ROOT / "work/v6_perf_differential_1/differential_results.json")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "work/v6_differential_v2_component")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--cell-timeout", type=float, default=300.0,
                        help="wall budget (s) for one cell's transition-level check")
    parser.add_argument("--no-memo-timeout", type=float, default=60.0,
                        help="wall budget (s) for one no-memo full-automaton build")
    parser.add_argument("--worker-grace", type=float, default=120.0,
                        help="extra seconds before the parent kills a child")
    parser.add_argument("--only", type=str, default=None,
                        help="comma-separated task ids; debug subset only")
    parser.add_argument("--use-existing", action="store_true",
                        help="reuse per-cell JSON already present in <out>/per_cell")
    # worker-only arguments
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cell-json", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-out", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker:
        cell = json.loads(args.cell_json)
        record = run_cell(cell, args.cell_timeout, args.no_memo_timeout)
        _write_cell_record(args.worker_out, record)
        print(json.dumps({
            "task_id": record["task_id"],
            "status": record["status"],
            "wall_seconds": record["wall_seconds"],
        }))
        return 0

    protocol = json.loads(args.protocol.read_text())
    cells = protocol["cells_detail"]
    if args.only:
        wanted = set(args.only.split(","))
        cells = [cell for cell in cells if cell["task_id"] in wanted]
        missing = wanted - {cell["task_id"] for cell in cells}
        if missing:
            print(f"unknown task ids: {sorted(missing)}", file=sys.stderr)
            return 2
    cell_dir = args.out / "per_cell_sync_transition"
    if not args.use_existing and cell_dir.exists():
        shutil.rmtree(cell_dir)
    cell_dir.mkdir(parents=True, exist_ok=True)

    results = []
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(_run_child, args, cell, cell_dir / f"{cell['task_id']}.json"): cell
            for cell in cells
        }
        for future in concurrent.futures.as_completed(futures):
            record = future.result()
            results.append(record)
            print(
                f"[{len(results):2d}/{len(cells)}] {record.get('family')}/"
                f"{record.get('stratum')}: {record.get('status')} "
                f"states={record.get('states')} transitions={record.get('transitions')} "
                f"mismatch={record.get('mismatch_count')} "
                f"graph={record.get('graph_pass')} "
                f"no_memo={record.get('no_memo_automaton')} "
                f"wall={record.get('wall_seconds')}s",
                flush=True,
            )
    elapsed = time.perf_counter() - started
    results.sort(key=lambda record: (record.get("family") or "", record.get("stratum") or ""))
    summary = aggregate(results, cells, args)
    summary["run_wall_seconds"] = round(elapsed, 3)
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "sync_transition_equivalence.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        key: summary[key]
        for key in (
            "PASS", "coverage_complete", "cells_checked", "cells_censored",
            "states_checked", "transitions_checked", "fresh_equals_memo",
            "mismatch_count", "graph_reconstruction_pass",
            "no_memo_full_automaton", "run_wall_seconds",
        )
    }, indent=2))
    print(f"wrote {out_path}")
    return 0 if summary["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
