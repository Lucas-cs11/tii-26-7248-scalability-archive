"""Layer C regression tests: DBM/Fraction boundaries and component equivalence.

These tests are permanent regressions for the semantics-preserving optimisation
of the SCG, synchronizer and temporal-expansion builders.  They cover:

* exact rational timing boundaries (half seconds, arbitrary denominators,
  zero, negatives, infinity, tight equality, empty zones, canonical
  normalisation);
* persistent-clock projection, newly-enabled reset and self-reenabling under
  exact rational timing;
* the transition memo of ``build_synchronizer`` against a fresh recomputation;
* the lazy SCG adjacency index against the raw edge-list projection;
* the marking caches of ``build_expansion`` against a cache-free reference;
* the trace-record dedupe rule against a no-dedupe expansion.

They only add assertions; no existing test is changed or weakened.
"""
from __future__ import annotations

import math
import sys
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.benchmarks.v6_families import generate_v6_model  # noqa: E402
from src.model.tpn import INF, Arc, Interval, Place, TPN, Transition  # noqa: E402
from src.scg.builder import (  # noqa: E402
    DBM,
    SCG,
    _build_initial_class,
    _compute_successor,
    build_scg,
)
from src.semantics.fraction_oracle import (  # noqa: E402
    check_firing_sequence,
    enumerate_oracle_traces,
)
from src.synchronizer.builder import (  # noqa: E402
    build_synchronizer,
    synchronizer_transition,
)
from src.temporal_expansion.builder import (  # noqa: E402
    TemporalExpansion,
    build_expansion,
)

from scripts.verify_v6_component_equivalence import (  # noqa: E402
    _dedupe_fingerprint_diffs,
    build_expansion_uncached,
    expansion_fingerprint,
    find_candidate_words,
)

F = Fraction


def make_tpn(place_names, transition_specs, arcs, initial, completions=()):
    places = {name: Place(name) for name in place_names}
    transitions = {}
    for name, label, fault, lo, hi in transition_specs:
        latest = INF if hi == INF else F(hi)
        transitions[name] = Transition(
            name, label, fault, False, Interval(F(lo), latest)
        )
    return TPN(
        places,
        transitions,
        [Arc(a, b) for a, b in arcs],
        initial,
        [dict(marking) for marking in completions],
    )


def conflict(left, right, completion=True):
    return make_tpn(
        ("p", "l", "r"),
        (("left", "l", False, *left), ("right", "", True, *right)),
        (("p", "left"), ("left", "l"), ("p", "right"), ("right", "r")),
        {"p": 1},
        ({"l": 1}, {"r": 1}) if completion else (),
    )


def replay_synchronizer(tpn, scg, L, fresh_map, initial_state, initial_key):
    """BFS replay of the synchronizer using only freshly computed transitions."""
    from collections import deque

    c_fin = scg.completion_ids
    labels = sorted(tpn.observable_alphabet)
    states = {initial_key: initial_state}
    edges = []
    words = []
    queue = deque([((), initial_key)])
    while queue:
        word, curr_key = queue.popleft()
        curr_state = states[curr_key]
        if curr_state.X_N & c_fin and curr_state.X_F & c_fin:
            words.append(word)
        if not curr_state.X_N or len(word) >= L:
            continue
        for label in labels:
            next_state, next_key = fresh_map[(curr_key, label)]
            if not next_state.X_N:
                continue
            if next_key not in states:
                states[next_key] = next_state
            edges.append((curr_key, label, next_key))
            queue.append((word + (label,), next_key))
    return states, edges, words


def scg_traces(tpn, max_depth):
    scg = build_scg(tpn)
    accepted = {()}
    frontier = [(scg.initial_id, tuple())]
    for _ in range(max_depth):
        following = []
        for class_id, prefix in frontier:
            if class_id in scg.completion_ids:
                continue
            for name, successor in scg.successors(class_id):
                trace = prefix + (name,)
                accepted.add(trace)
                following.append((successor, trace))
        frontier = following
    return frozenset(accepted)


class DBMFractionBoundaryTests(unittest.TestCase):
    """Exact rational DBM boundaries for the optimised SCG builder."""

    def test_half_second_initial_zone(self):
        tpn = make_tpn(
            ("p", "a_done", "b_done"),
            (("a", "a", False, 0.5, 1.5), ("b", "", True, 0.5, 1.5)),
            (("p", "a"), ("a", "a_done"), ("p", "b"), ("b", "b_done")),
            {"p": 1},
            ({"a_done": 1}, {"b_done": 1}),
        )
        initial = _build_initial_class(tpn)
        self.assertEqual(initial.dbm.get("a", "0"), F(3, 2))
        self.assertEqual(initial.dbm.get("0", "a"), F(-1, 2))
        self.assertEqual(initial.dbm.get("b", "0"), F(3, 2))
        self.assertEqual(initial.dbm.get("a", "b"), F(3, 2) - F(1, 2))
        self.assertFalse(initial.dbm.is_empty())

    def test_arbitrary_denominator_interval(self):
        tpn = make_tpn(
            ("p", "done"),
            (("a", "a", False, F(1, 3), F(5, 7)),),
            (("p", "a"), ("a", "done")),
            {"p": 1},
            ({"done": 1},),
        )
        initial = _build_initial_class(tpn)
        self.assertEqual(initial.dbm.get("a", "0"), F(5, 7))
        self.assertEqual(initial.dbm.get("0", "a"), F(-1, 3))
        successor = _compute_successor(tpn, initial, "a")
        self.assertIsNotNone(successor)
        self.assertTrue(successor.dbm.is_empty() is False)

    def test_negative_and_zero_bounds(self):
        tpn = make_tpn(
            ("p", "done"),
            (("a", "a", False, 2, 3), ("b", "b", False, 0, 1)),
            (("p", "a"), ("a", "done"), ("p", "b"), ("b", "done")),
            {"p": 1},
            ({"done": 1},),
        )
        initial = _build_initial_class(tpn)
        # x_a - x_b <= latest(a) - earliest(b) = 3
        self.assertEqual(initial.dbm.get("a", "b"), F(3))
        # x_b - x_a <= latest(b) - earliest(a) = -1
        self.assertEqual(initial.dbm.get("b", "a"), F(-1))
        self.assertLess(initial.dbm.get("b", "a"), 0)
        # zero earliest => DBM[0][u] = 0
        zero_net = make_tpn(
            ("p", "done"),
            (("a", "a", False, 0, 2),),
            (("p", "a"), ("a", "done")),
            {"p": 1},
            ({"done": 1},),
        )
        zero_initial = _build_initial_class(zero_net)
        self.assertEqual(zero_initial.dbm.get("0", "a"), F(0))

    def test_infinite_latest_bound(self):
        tpn = make_tpn(
            ("p", "done"),
            (("a", "a", False, F(1, 2), INF),),
            (("p", "a"), ("a", "done")),
            {"p": 1},
            ({"done": 1},),
        )
        initial = _build_initial_class(tpn)
        self.assertTrue(math.isinf(initial.dbm.get("a", "0")))
        self.assertEqual(initial.dbm.get("0", "a"), F(-1, 2))
        normalized = initial.dbm.normalize()
        self.assertFalse(normalized.is_empty())
        self.assertTrue(math.isinf(normalized.get("a", "0")))
        self.assertEqual(hash(normalized), hash(normalized.normalize()))

    def test_tight_boundary_equality_legal(self):
        # left fires exactly at its latest time and the fault at the same time
        tpn = conflict((F(1, 2), F(1, 2)), (F(0), F(1, 2)))
        self.assertTrue(check_firing_sequence(tpn, ("left",)).feasible)

    def test_tight_boundary_equality_urgent_conflict(self):
        # right must fire by 1/2, so left cannot fire at 3/2
        tpn = conflict((F(3, 2), F(2)), (F(0), F(1, 2)))
        self.assertFalse(check_firing_sequence(tpn, ("left",)).feasible)
        self.assertTrue(check_firing_sequence(tpn, ("right",)).feasible)

    def test_empty_zone_normalization(self):
        dbm = DBM(frozenset({"0", "a"}))
        dbm.set("a", "0", F(-2))  # x_a <= -2
        dbm.set("0", "a", F(1))   # x_a >= -1
        normalized = dbm.normalize()
        self.assertTrue(normalized.is_empty())
        self.assertLess(normalized.get("a", "a"), 0)

    def test_normalize_is_canonical(self):
        left = DBM(frozenset({"0", "a", "b"}))
        left.set("a", "0", F(1, 2))
        left.set("0", "a", F(-1, 2))
        left.set("b", "0", F(3, 2))
        left.set("0", "b", F(-1, 2))
        left.set("a", "b", F(1))       # redundant: 1/2 - 1/2 = 0 <= 1
        right = DBM(frozenset({"0", "a", "b"}))
        right.set("a", "0", F(1, 2))
        right.set("0", "a", F(-1, 2))
        right.set("b", "0", F(3, 2))
        right.set("0", "b", F(-1, 2))
        right.set("a", "b", F(2))      # looser redundant bound
        normalized_left = left.normalize()
        normalized_right = right.normalize()
        self.assertEqual(normalized_left, normalized_right)
        self.assertEqual(hash(normalized_left), hash(normalized_right))
        self.assertEqual(normalized_left._mat, normalized_left.normalize()._mat)
        # the hash cache returns the same value on repeated calls
        self.assertEqual(hash(normalized_left), hash(normalized_left))

    def test_persistent_clock_projection_half_seconds(self):
        tpn = make_tpn(
            ("pa", "pb", "da", "db"),
            (("a", "a", False, F(1, 2), F(1, 2)), ("b", "b", False, F(1, 3), F(2, 3))),
            (("pa", "a"), ("a", "da"), ("pb", "b"), ("b", "db")),
            {"pa": 1, "pb": 1},
            ({"da": 1, "db": 1},),
        )
        initial = _build_initial_class(tpn)
        successor = _compute_successor(tpn, initial, "a")
        self.assertIsNotNone(successor)
        # b persists: a fires at 1/2 and b is still enabled afterwards, so the
        # residual firing window of b in the new coordinates is [0, 1/6]
        # (b's latest 2/3 minus a's firing time 1/2), and the cross bound keeps
        # x_b - x_a <= 1/6 in the old coordinates.
        self.assertEqual(initial.dbm.get("b", "a"), F(1, 6))
        self.assertEqual(successor.dbm.get("b", "0"), F(1, 6))
        self.assertEqual(successor.dbm.get("0", "b"), F(0))
        self.assertFalse(successor.dbm.is_empty())

    def test_newly_enabled_clock_reset_half_seconds(self):
        tpn = make_tpn(
            ("p0", "p1", "p2"),
            (("a", "a", False, F(1, 2), F(3, 2)), ("b", "b", False, F(1, 4), F(1, 4))),
            (("p0", "a"), ("a", "p1"), ("p1", "b"), ("b", "p2")),
            {"p0": 1},
            ({"p2": 1},),
        )
        initial = _build_initial_class(tpn)
        successor = _compute_successor(tpn, initial, "a")
        self.assertIsNotNone(successor)
        self.assertEqual(successor.dbm.get("b", "0"), F(1, 4))
        self.assertEqual(successor.dbm.get("0", "b"), F(-1, 4))
        self.assertTrue(check_firing_sequence(tpn, ("a", "b")).feasible)
        self.assertEqual(
            check_firing_sequence(tpn, ("a", "b")).event_times, (F(1, 2), F(3, 4))
        )

    def test_self_reenabling_clock_reset_half_seconds(self):
        tpn = make_tpn(
            ("p",),
            (("loop", "x", False, F(1, 2), F(3, 2)),),
            (("p", "loop"), ("loop", "p")),
            {"p": 1},
            (),
        )
        initial = _build_initial_class(tpn)
        successor = _compute_successor(tpn, initial, "loop")
        self.assertIsNotNone(successor)
        self.assertEqual(successor.dbm.get("loop", "0"), F(3, 2))
        self.assertEqual(successor.dbm.get("0", "loop"), F(-1, 2))
        self.assertTrue(check_firing_sequence(tpn, ("loop", "loop")).feasible)

    def test_fraction_oracle_half_second_traces(self):
        nets = [
            conflict((F(1, 2), F(3, 2)), (F(0), F(1, 2))),
            conflict((F(1, 2), F(1, 2)), (F(0), F(1, 2))),
            make_tpn(
                ("pa", "pb", "da", "db"),
                (("a", "a", False, F(1, 2), F(1, 2)), ("b", "b", False, F(1, 3), F(2, 3))),
                (("pa", "a"), ("a", "da"), ("pb", "b"), ("b", "db")),
                {"pa": 1, "pb": 1},
                ({"da": 1, "db": 1},),
            ),
        ]
        for tpn in nets:
            oracle = enumerate_oracle_traces(tpn, 2)
            graph = scg_traces(tpn, 2)
            self.assertEqual(graph - oracle, frozenset(), "SCG has unsound traces")
            self.assertEqual(oracle - graph, frozenset(), "SCG misses traces")

    def test_empty_transition_window_is_infeasible(self):
        tpn = make_tpn(
            ("p", "done"),
            (("a", "a", False, F(3, 2), F(2)), ("b", "", True, F(0), F(1, 2))),
            (("p", "a"), ("a", "done"), ("p", "b"), ("b", "done")),
            {"p": 1},
            ({"done": 1},),
        )
        self.assertFalse(check_firing_sequence(tpn, ("a",)).feasible)
        self.assertTrue(check_firing_sequence(tpn, ("b",)).feasible)


class ComponentCacheEquivalenceTests(unittest.TestCase):
    """Cache/memo equivalence regressions on a small frozen family model."""

    @classmethod
    def setUpClass(cls):
        tpn, default_l, default_h, _ = generate_v6_model(
            "F01_sequential_sparse", 2
        )
        cls.tpn = tpn
        cls.H = float(default_h)
        full_scg = build_scg(tpn)
        cls.scg_full = full_scg
        cls.scg = full_scg.build_truncated()
        words, _ = find_candidate_words(tpn, cls.scg, max_words=4)
        cls.words = words
        cls.L = max([int(default_l)] + [len(word) for word in words])
        cls.sync = build_synchronizer(tpn, cls.scg, cls.L)

    def test_transition_cache_is_exposed_read_only(self):
        cache = self.sync.transition_cache
        self.assertIsNotNone(cache)
        self.assertGreater(len(cache), 0)
        for (state_key, label), (next_state, next_key) in cache.items():
            self.assertIn(state_key, self.sync.states)
            self.assertEqual(next_key, next_state.key())
            self.assertIn(label, self.tpn.observable_alphabet)
            # the cache must never be part of state identity
            self.assertNotIn("transition_cache", repr(self.sync.states[state_key]))

    def test_transition_cache_equals_fresh_recomputation(self):
        cache = self.sync.transition_cache
        checked = 0
        for state_key, state in self.sync.states.items():
            for label in sorted(self.tpn.observable_alphabet):
                fresh = synchronizer_transition(self.scg, self.tpn, state, label)
                cached = cache.get((state_key, label))
                if cached is None:
                    continue
                memo_state, memo_key = cached
                self.assertEqual(fresh.X_N, memo_state.X_N)
                self.assertEqual(fresh.X_F, memo_state.X_F)
                self.assertEqual(fresh.key(), memo_key)
                checked += 1
        self.assertGreater(checked, 0)
        self.assertEqual(checked, len(cache))

    def test_fresh_transition_replay_reproduces_automaton(self):
        fresh_map = {}
        for state_key, state in self.sync.states.items():
            for label in sorted(self.tpn.observable_alphabet):
                fresh = synchronizer_transition(self.scg, self.tpn, state, label)
                fresh_map[(state_key, label)] = (fresh, fresh.key())
        states, edges, words = replay_synchronizer(
            self.tpn,
            self.scg,
            self.L,
            fresh_map,
            self.sync.states[self.sync.initial_key],
            self.sync.initial_key,
        )
        self.assertEqual(list(states), list(self.sync.states))
        self.assertEqual(edges, self.sync.edges)
        self.assertEqual(words, self.sync.candidate_words)

    def test_successors_lazy_index_matches_edge_projection(self):
        scg = self.scg_full
        self.assertIsNone(scg._succ_index)  # not built during construction
        projection = {cid: [] for cid in scg.classes}
        for (src, label, dst) in scg.edges:
            projection[src].append((label, dst))
        for cid in scg.classes:
            self.assertEqual(scg.successors(cid), projection[cid])
        self.assertIsNotNone(scg._succ_index)
        # unknown class id -> empty list, not KeyError
        self.assertEqual(scg.successors(max(scg.classes) + 1000), [])
        # the truncated graph gets its own fresh index
        truncated = scg.build_truncated()
        truncated_projection = {cid: [] for cid in truncated.classes}
        for (src, label, dst) in truncated.edges:
            truncated_projection[src].append((label, dst))
        for cid in truncated.classes:
            self.assertEqual(truncated.successors(cid), truncated_projection[cid])

    def test_marking_cache_matches_cache_free_reference(self):
        self.assertTrue(self.words, "expected at least one candidate word")
        for phi in self.words[:3]:
            cached = build_expansion(self.tpn, phi, self.H)
            uncached = build_expansion_uncached(self.tpn, phi, self.H)
            self.assertNotIsInstance(cached, str)
            self.assertNotIsInstance(uncached, str)
            self.assertEqual(
                expansion_fingerprint(cached), expansion_fingerprint(uncached)
            )
            # node/edge/record counts are part of the fingerprint
            self.assertEqual(len(cached.nodes), len(uncached.nodes))
            self.assertEqual(cached.edges, uncached.edges)

    def test_trace_record_dedupe_only_hides_identical_records(self):
        phi = self.words[0]
        deduped = build_expansion(self.tpn, phi, self.H)
        original_add_record = TemporalExpansion.add_record

        def add_record_no_dedupe(expansion, node_id, record):
            expansion.records[node_id].append(record)
            return True

        TemporalExpansion.add_record = add_record_no_dedupe
        try:
            raw = build_expansion(self.tpn, phi, self.H, budget=200000)
        finally:
            TemporalExpansion.add_record = original_add_record
        self.assertNotIsInstance(raw, str)
        diffs, _pairs, _deduped_terminals, _raw_terminals = (
            _dedupe_fingerprint_diffs(deduped, raw, phi)
        )
        self.assertEqual(diffs, [])
        # no-dedupe stores at least as many records and the distinct set agrees
        distinct = {
            (nid, repr((record.sigma, record.J_hat)))
            for nid, records in deduped.records.items()
            for record in records
        }
        raw_distinct = {
            (nid, repr((record.sigma, record.J_hat)))
            for nid, records in raw.records.items()
            for record in records
        }
        self.assertEqual(distinct, raw_distinct)


if __name__ == "__main__":
    unittest.main()
