"""API tests for minimum window-relaxation repairs."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.service import make_server  # noqa: E402
from app.store import AuditStore  # noqa: E402

# Modular residue conflict: unique 1-tick upper repair.
SINGLE_UNSAT = {
    "request_id": "req-fix-single",
    "modulus": 100,
    "anchor": {"id": "A", "tick": 95},
    "events": [{"id": "B", "counter": 4}],
    "constraints": [
        {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
    ],
}

# Bidirectional contradiction: two equal-cost repairs, adjudicated by the
# relaxation vector in constraint-id order (ba wins), then timeline (t_B=103).
EQUAL_COST_UNSAT = {
    "request_id": "req-fix-equal",
    "modulus": 100,
    "anchor": {"id": "A", "tick": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
        {"id": "ba", "src": "B", "dst": "anchor", "window": [92, 92]},
    ],
}

# A satisfiable audit: repairs against it must be refused.
SATISFIABLE = {
    "request_id": "req-fix-sat",
    "modulus": 100,
    "anchor": {"id": "A", "tick": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
    ],
}


class RepairApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls._tmp.name, "test.db")
        cls.store = AuditStore(cls.db_path)
        cls.server = make_server(cls.store, 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()
        cls._tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.url(path), data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def create_audit(self, payload):
        code, body = self.request("POST", "/audits", payload)
        self.assertIn(code, (200, 201), body)
        return body["audit_no"]

    def ticks(self, timeline):
        return {e["event"]: e["tick"] for e in timeline["entries"]}


class TestSingleWindowRepair(RepairApiBase):
    def test_single_window_repair(self):
        no = self.create_audit(SINGLE_UNSAT)
        code, body = self.request(
            "POST", f"/audits/{no}/repairs", {"fix_id": "fix-single-1"})
        self.assertEqual(code, 201, body)
        self.assertEqual(body["status"], "repaired")
        self.assertEqual(body["total_expansion"], 1)
        self.assertFalse(body["replayed"])
        result = body["result"]
        w = {x["constraint_id"]: x for x in result["windows"]}["ab"]
        self.assertEqual(w["original_window"], [8, 8])
        self.assertEqual(w["relaxed_window"], [8, 9])
        self.assertEqual(w["direction"], "upper")
        self.assertEqual(w["recomputed_difference"], 9)
        self.assertEqual(self.ticks(result["canonical_timeline"]),
                         {"anchor": 95, "B": 104})
        self.assertTrue(
            result["optimality_evidence"]["strong_duality"])


class TestEqualCostAdjudication(RepairApiBase):
    def test_canonical_choice_among_equal_cost_candidates(self):
        no = self.create_audit(EQUAL_COST_UNSAT)
        code, body = self.request(
            "POST", f"/audits/{no}/repairs", {"fix_id": "fix-equal-1"})
        self.assertEqual(code, 201, body)
        self.assertEqual(body["total_expansion"], 100)
        result = body["result"]
        by_id = {x["constraint_id"]: x for x in result["windows"]}
        # vector adjudicated by constraint-id order: ba lower, ab untouched
        self.assertEqual(by_id["ab"]["relaxed_window"], [8, 8])
        self.assertEqual(by_id["ba"]["relaxed_window"], [-8, 92])
        self.assertEqual(result["relaxation_vector"],
                         [{"constraint_id": "ab", "extension": [0, 0]},
                          {"constraint_id": "ba", "extension": [100, 0]}])
        # then canonical timeline: t_B = 103
        self.assertEqual(self.ticks(result["canonical_timeline"]),
                         {"anchor": 95, "B": 103})
        self.assertEqual(result["wrap_counts"], {"B": 1})


class TestIllegalSources(RepairApiBase):
    def test_repair_against_satisfiable_audit_rejected(self):
        no = self.create_audit(SATISFIABLE)
        code, body = self.request(
            "POST", f"/audits/{no}/repairs", {"fix_id": "fix-bad-1"})
        self.assertEqual(code, 409, body)
        self.assertIn("error", body)
        # repeating the marker is refused again and never creates a result
        code3, body3 = self.request(
            "POST", f"/audits/{no}/repairs", {"fix_id": "fix-bad-1"})
        self.assertEqual(code3, 409)
        self.assertIn("unsat", body3["error"])

    def test_repair_against_unknown_audit_404(self):
        code, body = self.request(
            "POST", "/audits/999999/repairs", {"fix_id": "fix-bad-2"})
        self.assertEqual(code, 404, body)

    def test_fix_id_reuse_against_other_source_rejected(self):
        no_a = self.create_audit(SINGLE_UNSAT)
        other = json.loads(json.dumps(EQUAL_COST_UNSAT))
        other["request_id"] = "req-fix-equal-reuse"
        no_b = self.create_audit(other)
        self.assertNotEqual(no_a, no_b)
        code1, body1 = self.request(
            "POST", f"/audits/{no_a}/repairs", {"fix_id": "fix-reuse-1"})
        self.assertEqual(code1, 201, body1)
        original_no = body1["repair_no"]
        # the same marker against a different frozen source is a new request
        code2, body2 = self.request(
            "POST", f"/audits/{no_b}/repairs", {"fix_id": "fix-reuse-1"})
        self.assertEqual(code2, 409, body2)
        # the original repair is untouched and still replays
        code3, body3 = self.request(
            "POST", f"/audits/{no_a}/repairs", {"fix_id": "fix-reuse-1"})
        self.assertEqual(code3, 200)
        self.assertEqual(body3["repair_no"], original_no)

    def test_missing_fix_id_400(self):
        no = self.create_audit(SINGLE_UNSAT)
        code, _ = self.request("POST", f"/audits/{no}/repairs", {})
        self.assertEqual(code, 400)

    def test_changed_payload_marker_rejected_without_new_result(self):
        no = self.create_audit(SINGLE_UNSAT)
        tampered = json.loads(json.dumps(SINGLE_UNSAT))
        tampered["modulus"] = 99
        code1, body1 = self.request(
            "POST", f"/audits/{no}/repairs",
            {"fix_id": "fix-tamper-1", "payload": tampered})
        self.assertEqual(code1, 409, body1)
        # the same fix id with a proper request creates exactly once
        code2, body2 = self.request(
            "POST", f"/audits/{no}/repairs", {"fix_id": "fix-tamper-1"})
        self.assertEqual(code2, 201, body2)
        repair_no = body2["repair_no"]
        # echoing the frozen source input replays the same repair number
        code3, body3 = self.request(
            "POST", f"/audits/{no}/repairs",
            {"fix_id": "fix-tamper-1",
             "payload": _strip_request_id(SINGLE_UNSAT)})
        self.assertEqual(code3, 200)
        self.assertEqual(body3["repair_no"], repair_no)


def _strip_request_id(payload):
    return {k: v for k, v in payload.items() if k != "request_id"}


class TestRepairIdempotency(RepairApiBase):
    def test_same_fix_id_and_payload_replays_original_number(self):
        no = self.create_audit(SINGLE_UNSAT)
        code1, body1 = self.request(
            "POST", f"/audits/{no}/repairs", {"fix_id": "fix-idem-1"})
        self.assertEqual(code1, 201)
        code2, body2 = self.request(
            "POST", f"/audits/{no}/repairs", {"fix_id": "fix-idem-1"})
        self.assertEqual(code2, 200)
        self.assertTrue(body2["replayed"])
        self.assertEqual(body2["repair_no"], body1["repair_no"])
        self.assertEqual(body2["total_expansion"], 1)


class TestFrozenRepairRead(RepairApiBase):
    def test_get_repair_returns_frozen_evidence(self):
        no = self.create_audit(EQUAL_COST_UNSAT)
        code, created = self.request(
            "POST", f"/audits/{no}/repairs", {"fix_id": "fix-frozen-1"})
        self.assertEqual(code, 201)
        repair_no = created["repair_no"]
        code2, rec = self.request("GET", f"/repairs/{repair_no}")
        self.assertEqual(code2, 200, rec)
        self.assertEqual(rec["fix_id"], "fix-frozen-1")
        self.assertEqual(rec["source_audit_no"], no)
        self.assertEqual(rec["result"]["total_expansion"], 100)
        self.assertEqual(
            rec["request"]["payload"]["modulus"],
            EQUAL_COST_UNSAT["modulus"])
        # the source audit reading is still intact and untouched
        code3, audit = self.request("GET", f"/audits/{no}")
        self.assertEqual(code3, 200)
        self.assertEqual(audit["result"]["status"], "unsat")

    def test_get_missing_repair_404(self):
        code, _ = self.request("GET", "/repairs/999999")
        self.assertEqual(code, 404)


class TestRepairPersistenceAcrossRestart(unittest.TestCase):
    """A store reopened on the same SQLite file must still serve the frozen
    source, minimum cost and repair evidence by the original number."""

    def test_reopen_store_keeps_source_and_repair(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "persist.db")

        store = AuditStore(db_path)
        body = {k: v for k, v in SINGLE_UNSAT.items() if k != "request_id"}
        source_result = _solve(body)
        record, created = store.create("req-persist-1", body, source_result)
        self.assertTrue(created)
        audit_no = record["audit_no"]

        from app.repair import repair
        result = repair(record["input"], record["result"])
        fix, created = store.create_repair(
            "fix-persist-1", audit_no,
            {"source_audit_no": audit_no, "payload": record["input"]}, result)
        self.assertTrue(created)
        repair_no = fix["repair_no"]
        store.close()

        # Reopen: simulates a service restart against the same volume.
        store2 = AuditStore(db_path)
        self.addCleanup(store2.close)
        replayed_audit = store2.get(audit_no)
        self.assertIsNotNone(replayed_audit)
        self.assertEqual(replayed_audit["result"]["status"], "unsat")
        replayed_fix = store2.get_repair(repair_no)
        self.assertIsNotNone(replayed_fix)
        self.assertEqual(replayed_fix["fix_id"], "fix-persist-1")
        self.assertEqual(replayed_fix["source_audit_no"], audit_no)
        self.assertEqual(replayed_fix["result"]["total_expansion"], 1)
        self.assertEqual(
            replayed_fix["result"]["windows"][0]["relaxed_window"], [8, 9])
        # the fix marker still resolves to the same repair number
        self.assertEqual(store2.find_repair("fix-persist-1")["repair_no"],
                         repair_no)


def _solve(body):
    from app.solver import solve
    return solve(body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
