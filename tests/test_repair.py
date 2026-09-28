"""Unit tests for minimum window-relaxation repairs."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.repair import RepairError, repair  # noqa: E402
from app.solver import solve  # noqa: E402


def ticks(timeline):
    return {e["event"]: e["tick"] for e in timeline["entries"]}


def rows_by_id(result):
    return {w["constraint_id"]: w for w in result["windows"]}


class SingleWindowRepair(unittest.TestCase):
    """Modular residue conflict: M=100, A=95, B counter 4, A->B=[8,8].

    t_B in {4,104,204,...}; at k=0 the window misses by 99 ticks on the lower
    side, at k=1 it overshoots by exactly 1 tick on the upper side.  The
    unique minimum repair widens [8,8] to [8,9] (one tick) and places B at
    104.
    """

    PAYLOAD = {
        "modulus": 100,
        "anchor": {"id": "A", "tick": 95},
        "events": [{"id": "B", "counter": 4}],
        "constraints": [
            {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
        ],
    }

    def setUp(self):
        self.source = solve(self.PAYLOAD)
        self.assertEqual(self.source["status"], "unsat")
        self.rep = repair(self.PAYLOAD, self.source)

    def test_minimum_single_tick_upper_relaxation(self):
        self.assertEqual(self.rep["status"], "repaired")
        self.assertEqual(self.rep["total_expansion"], 1)
        w = rows_by_id(self.rep)["ab"]
        self.assertEqual(w["original_window"], [8, 8])
        self.assertEqual(w["relaxed_window"], [8, 9])
        self.assertEqual((w["extend_lower"], w["extend_upper"]), (0, 1))
        self.assertEqual(w["direction"], "upper")

    def test_canonical_timeline_and_difference(self):
        tl = ticks(self.rep["canonical_timeline"])
        self.assertEqual(tl, {"anchor": 95, "B": 104})
        self.assertEqual(self.rep["wrap_counts"], {"B": 1})
        w = rows_by_id(self.rep)["ab"]
        self.assertEqual(w["recomputed_difference"], 9)
        self.assertEqual(w["relaxed_window"], [8, 9])

    def test_relaxed_instance_recomputes_feasible(self):
        v = self.rep["feasibility_verification"]
        self.assertTrue(v["feasible"])
        self.assertNotEqual(v["relaxed_status"], "unsat")
        self.assertTrue(self.rep["optimality_evidence"]["strong_duality"])


class EqualCostAdjudication(unittest.TestCase):
    """Spec bidirectional contradiction: A->B=[8,8] needs t_B=103 while
    B->A=[92,92] needs t_B=3.

    Repairs of total 100 exist in two ways:
      * k_B=0: widen ab lower by 100  ([8,8]   -> [-92,8])
      * k_B=1: widen ba lower by 100  ([92,92] -> [-8,92])
    Both cost 100; the relaxation vectors (constraint-id order: ab, ba) are
    ((100,0),(0,0)) vs ((0,0),(100,0)), so the second wins lex, and the
    canonical timeline is t_B=103.
    """

    PAYLOAD = {
        "modulus": 100,
        "anchor": {"id": "A", "tick": 95},
        "events": [{"id": "B", "counter": 3}],
        "constraints": [
            {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
            {"id": "ba", "src": "B", "dst": "anchor", "window": [92, 92]},
        ],
    }

    def setUp(self):
        self.source = solve(self.PAYLOAD)
        self.assertEqual(self.source["status"], "unsat")
        self.rep = repair(self.PAYLOAD, self.source)

    def test_total_and_canonical_vector(self):
        self.assertEqual(self.rep["total_expansion"], 100)
        by_id = rows_by_id(self.rep)
        self.assertEqual(by_id["ab"]["relaxed_window"], [8, 8])
        self.assertEqual(by_id["ba"]["relaxed_window"], [-8, 92])
        self.assertEqual(by_id["ba"]["direction"], "lower")
        vector = [(v["constraint_id"], v["extension"])
                  for v in self.rep["relaxation_vector"]]
        self.assertEqual(vector, [("ab", [0, 0]), ("ba", [100, 0])])

    def test_canonical_timeline_is_lex_winner(self):
        tl = ticks(self.rep["canonical_timeline"])
        self.assertEqual(tl, {"anchor": 95, "B": 103})
        self.assertEqual(self.rep["wrap_counts"], {"B": 1})

    def test_recomputed_differences_inside_relaxed_windows(self):
        by_id = rows_by_id(self.rep)
        self.assertEqual(by_id["ab"]["recomputed_difference"], 8)
        self.assertEqual(by_id["ba"]["recomputed_difference"], -8)


class IllegalSource(unittest.TestCase):
    def test_repair_requires_unsat_source(self):
        payload = {
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
            ],
        }
        unique = solve(payload)
        self.assertEqual(unique["status"], "unique")
        with self.assertRaises(RepairError):
            repair(payload, unique)
        with self.assertRaises(RepairError):
            repair(payload, {"status": "multiple"})
        with self.assertRaises(RepairError):
            repair(payload, None)


class BruteForceAgreement(unittest.TestCase):
    """Exhaustive check on a small hand-built instance."""

    def test_exhaustive_optimum_and_tie_break(self):
        # M=3, A=1, B=0, anchor->B window [7,7]: enumerate k_B.
        payload = {
            "modulus": 3,
            "anchor": {"id": "A", "tick": 1},
            "events": [{"id": "B", "counter": 0}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [7, 7]},
            ],
        }
        source = solve(payload)
        self.assertEqual(source["status"], "unsat")
        # t_B = 3k; difference t_B - 1 = 3k-1; target 7.
        #   k=0 -> diff -1, lower extension 8
        #   k=1 -> diff  2, lower extension 5
        #   k=2 -> diff  5, lower extension 2
        #   k=3 -> diff  8, upper extension 1   <- minimum
        best = None
        for k in range(8):
            diff = 3 * k - 1
            ext = (max(0, 7 - diff), max(0, diff - 7))
            key = (ext[0] + ext[1], [list(ext)], [k])
            if best is None or key < best[0]:
                best = (key, ext, k)
        (best_total, _best_vec, [best_k]), best_ext, _ = best
        rep = repair(payload, source)
        self.assertEqual(rep["total_expansion"], best_total)
        self.assertEqual(rep["total_expansion"], 1)
        self.assertEqual(rep["wrap_counts"], {"B": best_k})
        self.assertEqual(best_k, 3)
        w = rows_by_id(rep)["ab"]
        self.assertEqual((w["extend_lower"], w["extend_upper"]), (0, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
