"""API tests for minimal window-relaxation repairs.

Covers: single-window repair, canonical adjudication of equal-cost
candidates, illegal-source rejection (non-unsat / unknown audit), idempotent
replay and changed-payload refusal, frozen repair retrieval and persistence
across a store restart, and unchanged compatibility of the original audit
reads.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.service import make_server  # noqa: E402
from app.store import AuditStore  # noqa: E402

UNSAT_PAYLOAD = {
    "request_id": "req-unsat-r",
    "modulus": 100,
    "anchor": {"id": "A", "tick": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
        {"id": "ba", "src": "B", "dst": "anchor", "window": [92, 92]},
    ],
}

# Widening the single [8,8] window by 5 ticks admits both grid points 0 and
# 10 (M=10) at the same minimum cost: exercises the timeline-level decision.
TIE_PAYLOAD = {
    "request_id": "req-tie-r",
    "modulus": 10,
    "anchor": {"id": "A", "tick": 5},
    "events": [{"id": "B", "counter": 5}],
    "constraints": [
        {"id": "ab", "src": "anchor", "dst": "B", "window": [5, 5]},
    ],
}

UNIQUE_PAYLOAD = {
    "request_id": "req-unique-r",
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
        # Each test method gets a fresh request id; the class-level store is
        # shared, so reusing the module-constant id would replay (200).
        body = json.loads(json.dumps(payload))
        body["request_id"] = f"{payload['request_id']}-{uuid.uuid4().hex[:10]}"
        code, resp = self.request("POST", "/audits", body)
        self.assertIn(code, (200, 201), resp)
        return resp["audit_no"]

    def create_repair(self, audit_no, repair_id, extra=None):
        body = {"repair_id": repair_id}
        if extra:
            body.update(extra)
        return self.request("POST", f"/audits/{audit_no}/repairs", body)


class TestSingleWindowRepair(RepairApiBase):
    def test_repair_created_with_minimum_cost_and_timeline(self):
        # Unsat due to a single modular-residue conflict (counter 4 vs the
        # required distance 8): one outward tick fixes it.
        payload = {
            "request_id": "req-residue-r",
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 4}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]}],
        }
        audit_no = self.create_audit(payload)
        code, body = self.create_repair(audit_no, "rid-single")
        self.assertEqual(code, 201, body)
        self.assertFalse(body["replayed"])
        self.assertEqual(body["status"], "repaired")
        self.assertEqual(body["source_audit_no"], audit_no)
        result = body["result"]
        self.assertEqual(result["total_extension"], 1)
        ticks = {e["event"]: e["tick"]
                 for e in result["timeline"]["entries"]}
        self.assertEqual(ticks, {"anchor": 95, "B": 104})
        (ab,) = result["constraints"]
        self.assertEqual(ab["original_window"], [8, 8])
        self.assertEqual(ab["relaxed_window"], [7, 9])
        self.assertEqual(ab["direction"], "upper")
        self.assertEqual(ab["recomputed_difference"], 9)
        self.assertEqual(result["source"]["audit_no"], audit_no)
        self.assertEqual(result["source"]["conflict"]["type"],
                         "modular_residue_conflict")


class TestCanonicalAdjudication(RepairApiBase):
    def test_equal_cost_candidates_adjudicated_by_constraint_order(self):
        audit_no = self.create_audit(UNSAT_PAYLOAD)
        code, body = self.create_repair(audit_no, "rid-tie")
        self.assertEqual(code, 201, body)
        result = body["result"]
        self.assertEqual(result["total_extension"], 100)
        decision = result["canonical_decision"]
        self.assertEqual(decision["constraint_order"], ["ab", "ba"])
        self.assertEqual(decision["relax_vector"], [0, 100])
        self.assertEqual(decision["wrap_vector"], [1])
        ticks = {e["event"]: e["tick"]
                 for e in result["timeline"]["entries"]}
        self.assertEqual(ticks["B"], 103)

    def test_second_level_tie_adjudicated_by_event_order(self):
        audit_no = self.create_audit(TIE_PAYLOAD)
        code, body = self.create_repair(audit_no, "rid-tie2")
        self.assertEqual(code, 201, body)
        result = body["result"]
        self.assertEqual(result["total_extension"], 5)
        self.assertEqual(result["canonical_decision"]["wrap_vector"], [0])
        ticks = {e["event"]: e["tick"]
                 for e in result["timeline"]["entries"]}
        self.assertEqual(ticks["B"], 5)


class TestIllegalSource(RepairApiBase):
    def test_repair_on_non_unsat_audit_rejected_409(self):
        audit_no = self.create_audit(UNIQUE_PAYLOAD)
        code, body = self.create_repair(audit_no, "rid-bad-status")
        self.assertEqual(code, 409)
        self.assertIn("error", body)

    def test_repair_on_unknown_audit_404(self):
        code, body = self.create_repair(999999, "rid-bad-audit")
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_missing_repair_id_400(self):
        audit_no = self.create_audit(UNSAT_PAYLOAD)
        code, body = self.request(
            "POST", f"/audits/{audit_no}/repairs", {})
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_unknown_payload_field_400(self):
        audit_no = self.create_audit(UNSAT_PAYLOAD)
        code, body = self.request(
            "POST", f"/audits/{audit_no}/repairs",
            {"repair_id": "rid-bad-body", "candidates": ["ab"]})
        self.assertEqual(code, 400)


class TestRepairIdempotency(RepairApiBase):
    def test_replay_returns_same_repair_number(self):
        audit_no = self.create_audit(UNSAT_PAYLOAD)
        code1, body1 = self.create_repair(audit_no, "rid-idem")
        code2, body2 = self.create_repair(audit_no, "rid-idem")
        self.assertEqual((code1, code2), (201, 200))
        self.assertEqual(body1["repair_no"], body2["repair_no"])
        self.assertTrue(body2["replayed"])
        self.assertEqual(body2["result"]["total_extension"], 100)

    def test_repair_id_reuse_with_changed_payload_refused(self):
        audit_no = self.create_audit(UNSAT_PAYLOAD)
        code1, body1 = self.create_repair(audit_no, "rid-conflict")
        self.assertEqual(code1, 201)
        code2, body2 = self.request(
            "POST", f"/audits/{audit_no}/repairs",
            {"repair_id": "rid-conflict", "unexpected": 1})
        self.assertEqual(code2, 400)
        # The same repair id + identical body still replays normally.
        code3, body3 = self.create_repair(audit_no, "rid-conflict")
        self.assertEqual(code3, 200)
        self.assertEqual(body3["repair_no"], body1["repair_no"])


class TestFrozenRepairAndCompatibility(RepairApiBase):
    def test_get_repair_returns_frozen_source_cost_evidence(self):
        audit_no = self.create_audit(UNSAT_PAYLOAD)
        code, created = self.create_repair(audit_no, "rid-frozen")
        self.assertEqual(code, 201)
        repair_no = created["repair_no"]
        code, rec = self.request("GET", f"/repairs/{repair_no}")
        self.assertEqual(code, 200)
        self.assertEqual(rec["repair_id"], "rid-frozen")
        self.assertEqual(rec["source_audit_no"], audit_no)
        self.assertEqual(rec["payload"], {})
        self.assertEqual(rec["result"]["total_extension"], 100)
        # The frozen source input and its original unsat evidence are kept.
        self.assertEqual(rec["source"]["input"]["modulus"], 100)
        self.assertEqual(rec["source"]["result"]["status"], "unsat")
        self.assertIn("conflict", rec["source"]["result"])

    def test_get_missing_repair_404(self):
        code, _ = self.request("GET", "/repairs/999999")
        self.assertEqual(code, 404)

    def test_original_audit_read_still_compatible(self):
        audit_no = self.create_audit(UNSAT_PAYLOAD)
        self.create_repair(audit_no, "rid-compat")
        code, rec = self.request("GET", f"/audits/{audit_no}")
        self.assertEqual(code, 200)
        self.assertEqual(rec["result"]["status"], "unsat")
        self.assertEqual(rec["input"]["events"],
                         UNSAT_PAYLOAD["events"])
        # The audit record carries no repair-specific fields.
        self.assertNotIn("repair", rec)

    def test_repair_survives_store_restart(self):
        audit_no = self.create_audit(UNSAT_PAYLOAD)
        _, created = self.create_repair(audit_no, "rid-restart")
        repair_no = created["repair_no"]

        reopened = AuditStore(self.db_path)
        rec = reopened.get_repair(repair_no)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["repair_id"], "rid-restart")
        self.assertEqual(rec["source_audit_no"], audit_no)
        self.assertEqual(rec["result"]["total_extension"], 100)
        self.assertEqual(rec["source"]["result"]["status"], "unsat")
        source = reopened.get(audit_no)
        self.assertEqual(source["result"]["status"], "unsat")
        reopened.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
