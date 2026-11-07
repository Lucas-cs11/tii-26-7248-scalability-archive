"""Independent exact-oracle versus SCG soundness/completeness checks."""
from fractions import Fraction
import math
import unittest

from src.model.tpn import Arc, INF, Interval, Place, TPN, Transition
from src.scg.builder import build_scg
from src.semantics.fraction_oracle import check_firing_sequence, enumerate_oracle_traces


F = Fraction


def make_tpn(place_names, transition_specs, arcs, initial, completions=()):
    places = {name: Place(name) for name in place_names}
    transitions = {
        name: Transition(name, label, fault, False, Interval(F(lo), INF if hi == INF else F(hi)))
        for name, label, fault, lo, hi in transition_specs
    }
    return TPN(places, transitions, [Arc(a, b) for a, b in arcs], initial,
               [dict(marking) for marking in completions])


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


def conflict(left, right):
    return make_tpn(
        ("p", "l", "r"),
        (("left", "l", False, *left), ("right", "", True, *right)),
        (("p", "left"), ("left", "l"), ("p", "right"), ("right", "r")),
        {"p": 1}, ({"l": 1}, {"r": 1}),
    )


class FractionOracleTests(unittest.TestCase):
    def assert_scg_exact(self, tpn, depth):
        oracle = enumerate_oracle_traces(tpn, depth)
        graph = scg_traces(tpn, depth)
        self.assertEqual(graph - oracle, frozenset(), f"unsound traces: {graph - oracle}")
        self.assertEqual(oracle - graph, frozenset(), f"missing traces: {oracle - graph}")

    def test_urgent_conflict_is_sound_and_complete(self):
        self.assert_scg_exact(conflict((2, 3), (0, 1)), 1)

    def test_legal_nondeterministic_conflict_is_complete(self):
        self.assert_scg_exact(conflict((2, 3), (1, 3)), 1)

    def test_optional_inspection_conflict(self):
        self.assertFalse(check_firing_sequence(conflict((1, 1), (0, 0)), ("left",)).feasible)

    def test_rework_conflict(self):
        self.assertFalse(check_firing_sequence(conflict((1, 2), (0, 0)), ("left",)).feasible)

    def test_latest_boundary_equality_is_legal(self):
        self.assertTrue(check_firing_sequence(conflict((1, 1), (0, 1)), ("left",)).feasible)

    def test_zero_time_transition(self):
        self.assert_scg_exact(conflict((0, 0), (1, 2)), 1)

    def test_infinite_latest_transition(self):
        self.assert_scg_exact(conflict((4, INF), (0, 5)), 1)

    def test_persistent_clock_inheritance(self):
        tpn = make_tpn(
            ("pa", "pb", "da", "db"),
            (("a", "a", False, 2, 2), ("b", "b", False, 1, 3)),
            (("pa", "a"), ("a", "da"), ("pb", "b"), ("b", "db")),
            {"pa": 1, "pb": 1}, ({"da": 1, "db": 1},),
        )
        self.assert_scg_exact(tpn, 2)
        self.assertTrue(check_firing_sequence(tpn, ("a", "b")).feasible)

    def test_newly_enabled_clock_reset(self):
        tpn = make_tpn(
            ("p0", "p1", "p2"),
            (("a", "a", False, 2, 2), ("b", "b", False, 1, 1)),
            (("p0", "a"), ("a", "p1"), ("p1", "b"), ("b", "p2")),
            {"p0": 1}, ({"p2": 1},),
        )
        result = check_firing_sequence(tpn, ("a", "b"))
        self.assertTrue(result.feasible)
        self.assertEqual(result.event_times, (F(2), F(3)))
        self.assert_scg_exact(tpn, 2)

    def test_self_reenabling_clock_reset(self):
        tpn = make_tpn(
            ("p",), (("loop", "x", False, 1, 1),),
            (("p", "loop"), ("loop", "p")), {"p": 1}, (),
        )
        result = check_firing_sequence(tpn, ("loop", "loop"))
        self.assertTrue(result.feasible)
        self.assertEqual(result.event_times, (F(1), F(2)))
        self.assert_scg_exact(tpn, 3)

    def test_untimed_reachable_but_time_infeasible_path(self):
        self.assertFalse(check_firing_sequence(conflict((3, 4), (0, 2)), ("left",)).feasible)

    def test_independent_concurrent_orders(self):
        tpn = make_tpn(
            ("pa", "pb", "da", "db"),
            (("a", "a", False, 0, 2), ("b", "b", False, 0, 2)),
            (("pa", "a"), ("a", "da"), ("pb", "b"), ("b", "db")),
            {"pa": 1, "pb": 1}, ({"da": 1, "db": 1},),
        )
        self.assert_scg_exact(tpn, 2)
        self.assertTrue(check_firing_sequence(tpn, ("a", "b")).feasible)
        self.assertTrue(check_firing_sequence(tpn, ("b", "a")).feasible)

    def test_fault_recovery_cycle(self):
        tpn = make_tpn(
            ("ready", "failed", "done"),
            (("fault", "", True, 0, 1), ("recover", "r", False, 1, 2),
             ("finish", "f", False, 2, 3)),
            (("ready", "fault"), ("fault", "failed"),
             ("failed", "recover"), ("recover", "ready"),
             ("ready", "finish"), ("finish", "done")),
            {"ready": 1}, ({"done": 1},),
        )
        self.assert_scg_exact(tpn, 4)

    def test_first_completion_entry_terminates_trace(self):
        tpn = make_tpn(
            ("p", "done", "after"),
            (("finish", "f", False, 0, 1), ("extra", "x", False, 0, 1)),
            (("p", "finish"), ("finish", "done"),
             ("done", "extra"), ("extra", "after")),
            {"p": 1}, ({"done": 1},),
        )
        self.assertFalse(check_firing_sequence(tpn, ("finish", "extra")).feasible)
        self.assert_scg_exact(tpn, 2)

    def test_normalization_preserves_trace_language(self):
        tpn = make_tpn(
            ("p", "q", "r", "done"),
            (("choose", "", False, 1, 2), ("parallel", "p", False, 0, 3),
             ("finish", "f", False, 2, 4)),
            (("p", "choose"), ("choose", "q"),
             ("r", "parallel"), ("parallel", "r"),
             ("q", "finish"), ("r", "finish"), ("finish", "done")),
            {"p": 1, "r": 1}, ({"done": 1},),
        )
        self.assert_scg_exact(tpn, 3)


if __name__ == "__main__":
    unittest.main()
