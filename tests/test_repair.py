"""Unit tests for the minimal window-relaxation repair solver."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.repair import repair, SourceNotUnsat  # noqa: E402
from app.solver import solve, ValidationError  # noqa: E402


BIDIRECTIONAL = {
    "modulus": 100,
    "anchor": {"id": "A", "tick": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
        {"id": "ba", "src": "B", "dst": "anchor", "window": [92, 92]},
    ],
}

RESIDUE = {
    "modulus": 100,
    "anchor": {"id": "A", "tick": 95},
    "events": [{"id": "B", "counter": 4}],
    "constraints": [
        {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
    ],
}

CYCLE = {
    "modulus": 100,
    "anchor": {"id": "A", "tick": 95},
    "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 3}],
    "constraints": [
        {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
        {"id": "bc", "src": "B", "dst": "C", "window": [100, 100]},
        {"id": "cb", "src": "C", "dst": "B", "window": [100, 100]},
    ],
}


def unsat_record(payload, audit_no=1):
    result = solve(payload)
    assert result["status"] == "unsat"
    return {
        "audit_no": audit_no,
        "request_id": f"req-{audit_no}",
        "payload_hash": "hash",
        "input": payload,
        "result": result,
    }


def ticks(timeline):
    return {e["event"]: e["tick"] for e in timeline["entries"]}


class SingleWindowRepair(unittest.TestCase):
    def test_residue_conflict_fixed_by_one_tick(self):
        # B counter=4 cannot sit at distance 8 (grid points are 9 and -91);
        # one outward tick on the upper end admits t_B = 104.
        out = repair(unsat_record(RESIDUE), {})
        self.assertEqual(out["status"], "repaired")
        self.assertEqual(out["total_extension"], 1)
        self.assertEqual(ticks(out["timeline"]),
                         {"anchor": 95, "B": 104})
        (ab,) = out["constraints"]
        self.assertEqual(ab["original_window"], [8, 8])
        self.assertEqual(ab["relaxed_window"], [7, 9])
        self.assertEqual(ab["extension"], 1)
        self.assertEqual(ab["direction"], "upper")
        self.assertEqual(ab["outward_gap"], 1)
        self.assertEqual(ab["recomputed_difference"], 9)

    def test_recomputed_difference_lies_in_relaxed_window(self):
        out = repair(unsat_record(RESIDUE), {})
        for c in out["constraints"]:
            lo, hi = c["relaxed_window"]
            self.assertLessEqual(lo, c["recomputed_difference"])
            self.assertLessEqual(c["recomputed_difference"], hi)

    def test_extensions_sum_to_total(self):
        out = repair(unsat_record(CYCLE), {})
        self.assertEqual(
            sum(c["extension"] for c in out["constraints"]),
            out["total_extension"])


class CanonicalDecision(unittest.TestCase):
    def test_equal_cost_candidates_adjudicated_by_constraint_order(self):
        # Widening ab by 100 (timeline B=3) and widening ba by 100 (timeline
        # B=103) are both minimum; constraint order [ab, ba] picks the
        # lexicographically smallest relax vector [0, 100].
        out = repair(unsat_record(BIDIRECTIONAL), {})
        self.assertEqual(out["total_extension"], 100)
        decision = out["canonical_decision"]
        self.assertEqual(decision["constraint_order"], ["ab", "ba"])
        self.assertEqual(decision["relax_vector"], [0, 100])
        self.assertEqual(decision["wrap_vector"], [1])
        self.assertEqual(ticks(out["timeline"])["B"], 103)
        by_id = {c["id"]: c for c in out["constraints"]}
        self.assertEqual(by_id["ab"]["extension"], 0)
        self.assertEqual(by_id["ba"]["extension"], 100)
        self.assertEqual(by_id["ba"]["direction"], "lower")
        self.assertEqual(by_id["ba"]["recomputed_difference"], -8)

    def test_second_level_tie_adjudicated_by_event_order(self):
        # Window [5,5] with grid differences 0 and 10 (M=10) is unsat;
        # widening by exactly 5 admits BOTH timelines at the same minimum
        # cost with the same relax vector -- the canonical timeline takes
        # the smallest wrap count.
        payload = {
            "modulus": 10,
            "anchor": {"id": "A", "tick": 5},
            "events": [{"id": "B", "counter": 5}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [5, 5]}],
        }
        out = repair(unsat_record(payload), {})
        self.assertEqual(out["total_extension"], 5)
        self.assertEqual(out["canonical_decision"]["relax_vector"], [5])
        self.assertEqual(out["canonical_decision"]["wrap_vector"], [0])
        self.assertEqual(ticks(out["timeline"])["B"], 5)
        self.assertEqual(out["constraints"][0]["relaxed_window"], [0, 10])

    def test_cycle_repair(self):
        out = repair(unsat_record(CYCLE), {})
        self.assertEqual(out["total_extension"], 200)
        self.assertEqual(out["canonical_decision"]["relax_vector"],
                         [0, 0, 200])
        self.assertEqual(ticks(out["timeline"]),
                         {"anchor": 95, "B": 103, "C": 203})


class MinimalityByEnumeration(unittest.TestCase):
    """Cross-check the cut optimum against exhaustive wrap-count search on
    small random instances: minimum total, then relax vector, then wrap
    vector must all agree."""

    def test_random_instances_match_brute_force(self):
        import itertools
        import random
        from app.repair import optimize, _seed_amounts
        from app.solver import (
            _Propagator, _build_edges, _validate, Constraint)

        rng = random.Random(1234)
        checked = 0
        for _ in range(300):
            n = rng.randint(1, 3)
            m = rng.randint(2, 7)
            anchor_tick = rng.randint(0, 3 * m)
            events = [{"id": chr(65 + i), "counter": rng.randrange(m)}
                      for i in range(n)]
            ids = [chr(65 + i) for i in range(n)]
            constraints, ci = [], 0
            connected = set()
            for nid in sorted(ids, key=lambda _: rng.random()):
                parent = rng.choice(
                    ["anchor"] + sorted(connected) or ["anchor"])
                lo = rng.randint(-2 * m, 2 * m)
                hi = lo + rng.randint(0, 2 * m)
                if rng.random() < 0.5:
                    src, dst = parent, nid
                else:
                    src, dst = nid, parent
                constraints.append({"id": f"c{ci:02d}", "src": src,
                                    "dst": dst, "window": [lo, hi]})
                ci += 1
                connected.add(nid)
            for _ in range(rng.randint(0, 2)):
                a, b = rng.sample(["anchor"] + ids, 2)
                lo = rng.randint(-2 * m, 2 * m)
                hi = lo + rng.randint(0, 2 * m)
                constraints.append({"id": f"c{ci:02d}", "src": a,
                                    "dst": b, "window": [lo, hi]})
                ci += 1
            payload = {"modulus": m,
                       "anchor": {"id": "Z", "tick": anchor_tick},
                       "events": events, "constraints": constraints}
            if solve(payload)["status"] != "unsat":
                continue
            mm, aa, ee, raw = _validate(payload)
            cs = sorted(raw, key=lambda c: c.id)
            residues = {e["id"]: e["counter"] for e in ee}
            _, seed = _seed_amounts(cs, residues, aa)
            wide = [Constraint(c.id, c.src, c.dst, c.lo - seed, c.hi + seed)
                    for c in cs]
            nodes, edges, _ = _build_edges(mm, aa, ee, wide)
            p = _Propagator(nodes, edges, track=False)
            self.assertTrue(p.run())
            eids = sorted(residues)
            best = None
            spaces = [range(p.low[p.idx[e]], p.high[p.idx[e]] + 1)
                      for e in eids]
            for combo in itertools.product(*spaces):
                kvec = dict(zip(eids, combo))
                xs, total = [], 0
                for c in cs:
                    ts = aa if c.src == "anchor" else \
                        residues[c.src] + mm * kvec[c.src]
                    td = aa if c.dst == "anchor" else \
                        residues[c.dst] + mm * kvec[c.dst]
                    x = max(0, c.lo - (td - ts), (td - ts) - c.hi)
                    xs.append(x)
                    total += x
                key = (total, tuple(xs), tuple(kvec[e] for e in eids))
                if best is None or key < best:
                    best = key
            amounts, total, kvec, _, _ = optimize(mm, aa, ee, cs)
            got = (total,
                   tuple(amounts[c.id] for c in cs),
                   tuple(kvec[e] for e in eids))
            self.assertEqual(got, best, payload)
            checked += 1
        self.assertGreater(checked, 20)


class InvalidSource(unittest.TestCase):
    def test_non_unsat_source_rejected(self):
        unique = {
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]}],
        }
        rec = {"audit_no": 9, "input": unique, "result": solve(unique)}
        with self.assertRaises(SourceNotUnsat) as ctx:
            repair(rec, {})
        self.assertEqual(ctx.exception.audit_no, 9)

    def test_unknown_payload_fields_rejected(self):
        with self.assertRaises(ValidationError):
            repair(unsat_record(RESIDUE), {"candidates": ["ab"]})
        with self.assertRaises(ValidationError):
            repair(unsat_record(RESIDUE), {"unexpected": 1})


class FrozenSourceEvidence(unittest.TestCase):
    def test_result_carries_frozen_conflict_and_identity(self):
        rec = unsat_record(BIDIRECTIONAL, audit_no=42)
        out = repair(rec, {})
        self.assertEqual(out["source"]["audit_no"], 42)
        self.assertEqual(out["source"]["request_id"], "req-42")
        self.assertEqual(out["source"]["payload_hash"], "hash")
        self.assertEqual(out["source"]["original_status"], "unsat")
        self.assertEqual(out["source"]["conflict"]["type"],
                         "bound_contradiction")
        self.assertEqual(out["modulus"], 100)
        self.assertEqual(out["extension_unit"], "tick")


if __name__ == "__main__":
    unittest.main(verbosity=2)
