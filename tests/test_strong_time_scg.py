"""Permanent minimal regressions for strong-time SCG successor semantics."""
from fractions import Fraction
import unittest

from src.model.tpn import Arc, Interval, Place, TPN, Transition
from src.scg.builder import _build_initial_class, _compute_successor, build_scg


F = Fraction


def conflict_net(left_interval, right_interval, *, completion=True):
    places = {name: Place(name) for name in ("choice", "left_done", "right_done")}
    transitions = {
        "left": Transition("left", "left", False, False,
                           Interval(F(left_interval[0]), F(left_interval[1]))),
        "right": Transition("right", "", True, False,
                            Interval(F(right_interval[0]), F(right_interval[1]))),
    }
    arcs = [
        Arc("choice", "left"), Arc("left", "left_done"),
        Arc("choice", "right"), Arc("right", "right_done"),
    ]
    completions = [{"left_done": 1}, {"right_done": 1}] if completion else []
    return TPN(places, transitions, arcs, {"choice": 1}, completions)


class StrongTimeSCGRegressions(unittest.TestCase):
    def successor_names(self, tpn):
        scg = build_scg(tpn)
        return {name for source, name, _ in scg.edges if source == scg.initial_id}

    def test_A_urgent_fault_rejects_late_normal_successor(self):
        tpn = conflict_net((2, 3), (0, 1))
        self.assertEqual(self.successor_names(tpn), {"right"})

    def test_B_immediate_skip_rejects_delayed_inspection(self):
        tpn = conflict_net((1, 1), (0, 0))
        self.assertEqual(self.successor_names(tpn), {"right"})

    def test_C_immediate_continue_rejects_delayed_rework(self):
        tpn = conflict_net((1, 2), (0, 0))
        self.assertEqual(self.successor_names(tpn), {"right"})

    def test_D_overlapping_competition_keeps_both_legal_branches(self):
        tpn = conflict_net((2, 3), (1, 3))
        self.assertEqual(self.successor_names(tpn), {"left", "right"})

    def test_E_persistent_transition_keeps_remaining_firing_domain(self):
        places = {name: Place(name) for name in ("pa", "pb", "da", "db")}
        transitions = {
            "a": Transition("a", "a", False, False, Interval(F(2), F(2))),
            "b": Transition("b", "b", False, False, Interval(F(1), F(3))),
        }
        arcs = [
            Arc("pa", "a"), Arc("a", "da"),
            Arc("pb", "b"), Arc("b", "db"),
        ]
        tpn = TPN(places, transitions, arcs, {"pa": 1, "pb": 1}, [{"da": 1, "db": 1}])
        successor = _compute_successor(tpn, _build_initial_class(tpn), "a")
        self.assertIsNotNone(successor)
        self.assertEqual(successor.dbm.get("b", "0"), F(1))
        self.assertEqual(successor.dbm.get("0", "b"), F(0))

    def test_F_self_reenabling_transition_gets_fresh_firing_domain(self):
        places = {"p": Place("p")}
        transitions = {
            "loop": Transition("loop", "tick", False, False, Interval(F(1), F(2)))
        }
        tpn = TPN(
            places, transitions, [Arc("p", "loop"), Arc("loop", "p")],
            {"p": 1}, [],
        )
        successor = _compute_successor(tpn, _build_initial_class(tpn), "loop")
        self.assertIsNotNone(successor)
        self.assertEqual(successor.dbm.get("loop", "0"), F(2))
        self.assertEqual(successor.dbm.get("0", "loop"), F(-1))

    def test_first_entry_completion_has_no_outgoing_edges(self):
        tpn = conflict_net((2, 3), (1, 3))
        scg = build_scg(tpn)
        self.assertTrue(scg.completion_ids)
        self.assertFalse(any(src in scg.completion_ids for src, _, _ in scg.edges))


if __name__ == "__main__":
    unittest.main()
