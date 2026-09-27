"""Unit tests for the wraparound timeline solver."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.solver import ValidationError, solve  # noqa: E402


def ticks_by_event(timeline):
    return {e["event"]: e["tick"] for e in timeline["entries"]}


class UniqueCase(unittest.TestCase):
    """Spec example: M=100, anchor A=95, B=3, A->B=[8,8] => B = 103."""

    PAYLOAD = {
        "modulus": 100,
        "anchor": {"id": "A", "tick": 95},
        "events": [{"id": "B", "counter": 3}],
        "constraints": [
            {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
        ],
    }

    def test_status_and_tick(self):
        res = solve(self.PAYLOAD)
        self.assertEqual(res["status"], "unique")
        ticks = ticks_by_event(res["timeline"])
        self.assertEqual(ticks["anchor"], 95)
        self.assertEqual(ticks["B"], 103)

    def test_wrap_count_and_evidence(self):
        res = solve(self.PAYLOAD)
        self.assertEqual(res["evidence"]["wrap_counts"], {"B": 1})
        self.assertIn("B", res["evidence"]["derivations"])

    def test_same_counter_is_not_same_time(self):
        # C reports the same counter value 3 as B; the constraints place them
        # one full wrap apart, so equal counters land on different ticks.
        payload = {
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 3}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
                {"id": "bc", "src": "B", "dst": "C", "window": [100, 100]},
            ],
        }
        res = solve(payload)
        self.assertEqual(res["status"], "unique")
        ticks = ticks_by_event(res["timeline"])
        self.assertEqual(ticks["B"], 103)
        self.assertEqual(ticks["C"], 203)  # same counter value, different time


class AmbiguousCase(unittest.TestCase):
    """M=100, A=95, B=3, A->B=[-92,8]: k_B in {0,1} => t_B in {3,103}.

    The same counter reading 3 is a real tick both before (3) and after (103)
    the anchor depending on wrap count; identical counter values must not be
    treated as one instant.
    """

    PAYLOAD = {
        "modulus": 100,
        "anchor": {"id": "A", "tick": 95},
        "events": [{"id": "B", "counter": 3}],
        "constraints": [
            {"id": "ab", "src": "anchor", "dst": "B", "window": [-92, 8]},
        ],
    }

    def test_two_canonical_timelines(self):
        res = solve(self.PAYLOAD)
        self.assertEqual(res["status"], "multiple")
        self.assertEqual(len(res["timelines"]), 2)
        t1 = ticks_by_event(res["timelines"][0])
        t2 = ticks_by_event(res["timelines"][1])
        self.assertEqual(t1["B"], 3)
        self.assertEqual(t2["B"], 103)
        # canonical order: anchor first, then events sorted by id
        for tl in res["timelines"]:
            ids = [e["event"] for e in tl["entries"]]
            self.assertEqual(
                ids, ["anchor"] + sorted(i for i in ids if i != "anchor"))

    def test_first_unstable_precedence(self):
        res = solve(self.PAYLOAD)
        unstable = res["first_unstable_precedence"]
        self.assertIsNotNone(unstable)
        self.assertEqual(unstable["pair"], ["anchor", "B"])
        # t_anchor - t_B ranges from 95-103=-8 to 95-3=92 (crosses zero)
        self.assertEqual(unstable["tick_difference_range"], [-8, 92])
        self.assertEqual(unstable["in_timeline_1"]["difference"], 92)
        self.assertEqual(unstable["in_timeline_2"]["difference"], -8)

    def test_multi_event_unstable_pair(self):
        # B free in {3,103}; C pinned at 104.  The anchor/B precedence flips
        # between the first two canonical timelines.
        payload = {
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 4}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [-92, 8]},
                {"id": "ac", "src": "anchor", "dst": "C", "window": [9, 9]},
            ],
        }
        res = solve(payload)
        self.assertEqual(res["status"], "multiple")
        t1 = ticks_by_event(res["timelines"][0])
        t2 = ticks_by_event(res["timelines"][1])
        self.assertEqual((t1["B"], t1["C"]), (3, 104))
        self.assertEqual((t2["B"], t2["C"]), (103, 104))
        unstable = res["first_unstable_precedence"]
        self.assertEqual(unstable["pair"], ["anchor", "B"])
        self.assertEqual(unstable["in_timeline_1"]["difference"], 92)
        self.assertEqual(unstable["in_timeline_2"]["difference"], -8)


class UnsatCase(unittest.TestCase):
    """Bidirectional contradiction chain: A->B=[8,8] forces t_B=103 while
    B->A=[92,92] forces t_B=3.  Each constraint is satisfiable on its own
    (both have non-empty integer k-windows); only the chain contradicts."""

    PAYLOAD = {
        "modulus": 100,
        "anchor": {"id": "A", "tick": 95},
        "events": [{"id": "B", "counter": 3}],
        "constraints": [
            {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
            {"id": "ba", "src": "B", "dst": "anchor", "window": [92, 92]},
        ],
    }

    def test_unsat_with_recomputable_chain(self):
        res = solve(self.PAYLOAD)
        self.assertEqual(res["status"], "unsat")
        conflict = res["conflict"]
        self.assertEqual(conflict["type"], "bound_contradiction")
        self.assertEqual(conflict["event"], "B")
        self.assertEqual(sorted(conflict["constraint_chain"]), ["ab", "ba"])
        lower = conflict["lower_bound_derivation"]
        upper = conflict["upper_bound_derivation"]
        # Recompute: ab forces k_B >= 1 (t_B >= 103); ba forces k_B <= 0
        # (t_B <= 3).  1 > 0 => contradiction.
        self.assertEqual(lower["derived_k_min"], 1)
        self.assertEqual(lower["derived_tick_min"], 103)
        self.assertEqual(upper["derived_k_max"], 0)
        self.assertEqual(upper["derived_tick_max"], 3)
        self.assertGreater(lower["derived_k_min"], upper["derived_k_max"])

    def test_modular_residue_conflict(self):
        # A->B=[8,8] with counter 4: t_B in {4,104,...}; 95+8=103 not among
        # them, so no integer wrap count exists.
        payload = {
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 4}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
            ],
        }
        res = solve(payload)
        self.assertEqual(res["status"], "unsat")
        conflict = res["conflict"]
        self.assertEqual(conflict["type"], "modular_residue_conflict")
        self.assertEqual(conflict["constraint_chain"], ["ab"])
        lo, hi = conflict["empty_k_window"]
        self.assertGreater(lo, hi)

    def test_cyclic_contradiction(self):
        # B->C and C->B both demand a full +100 ticks: impossible around the
        # cycle (k_C - k_B = 1 and k_B - k_C = 1 simultaneously).
        payload = {
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 3}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
                {"id": "bc", "src": "B", "dst": "C", "window": [100, 100]},
                {"id": "cb", "src": "C", "dst": "B", "window": [100, 100]},
            ],
        }
        res = solve(payload)
        self.assertEqual(res["status"], "unsat")
        conflict = res["conflict"]
        self.assertEqual(conflict["type"], "positive_cycle")
        self.assertEqual(sorted(conflict["constraint_chain"]), ["bc", "cb"])


class ValidationCase(unittest.TestCase):
    def test_disconnected_event_rejected(self):
        payload = {
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 3}, {"id": "Z", "counter": 1}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
            ],
        }
        with self.assertRaises(ValidationError):
            solve(payload)

    def test_too_many_events(self):
        payload = {
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": f"E{i:02d}", "counter": i % 100}
                       for i in range(13)],
            "constraints": [],
        }
        with self.assertRaises(ValidationError):
            solve(payload)

    def test_counter_out_of_range(self):
        payload = {
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 100}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
            ],
        }
        with self.assertRaises(ValidationError):
            solve(payload)

    def test_bad_window(self):
        payload = {
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [9, 8]},
            ],
        }
        with self.assertRaises(ValidationError):
            solve(payload)


class LargerAmbiguity(unittest.TestCase):
    def test_second_solution_is_lexicographic(self):
        # Two independent ambiguous events (each k in {0,1}); lex order
        # follows event ids: smallest vector (k_B, k_C) = (0,0), then (0,1).
        payload = {
            "modulus": 10,
            "anchor": {"id": "A", "tick": 5},
            "events": [{"id": "B", "counter": 1}, {"id": "C", "counter": 1}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [-5, 6]},
                {"id": "ac", "src": "anchor", "dst": "C", "window": [-5, 6]},
            ],
        }
        res = solve(payload)
        self.assertEqual(res["status"], "multiple")
        t1 = ticks_by_event(res["timelines"][0])
        t2 = ticks_by_event(res["timelines"][1])
        self.assertEqual((t1["B"], t1["C"]), (1, 1))
        self.assertEqual((t2["B"], t2["C"]), (1, 11))


if __name__ == "__main__":
    unittest.main(verbosity=2)
