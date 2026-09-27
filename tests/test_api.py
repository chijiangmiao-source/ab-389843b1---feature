"""API tests: HTTP endpoints, idempotency, frozen records, health check."""

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

UNIQUE_PAYLOAD = {
    "request_id": "req-unique-1",
    "modulus": 100,
    "anchor": {"id": "A", "tick": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
    ],
}

AMBIGUOUS_PAYLOAD = {
    "request_id": "req-ambiguous-1",
    "modulus": 100,
    "anchor": {"id": "A", "tick": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "ab", "src": "anchor", "dst": "B", "window": [-92, 8]},
    ],
}

UNSAT_PAYLOAD = {
    "request_id": "req-unsat-1",
    "modulus": 100,
    "anchor": {"id": "A", "tick": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
        {"id": "ba", "src": "B", "dst": "anchor", "window": [92, 92]},
    ],
}


class ApiTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        store = AuditStore(os.path.join(cls._tmp.name, "test.db"))
        cls.server = make_server(store, 0)  # ephemeral port
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, payload, request_id=None):
        body = dict(payload)
        if request_id is not None:
            body["request_id"] = request_id
        data = json.dumps(body).encode()
        req = urllib.request.Request(self.url("/audits"), data=data,
                                     headers={"Content-Type":
                                              "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url(path)) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class TestHealth(ApiTestBase):
    def test_health(self):
        code, body = self.get("/health")
        self.assertEqual(code, 200)
        self.assertEqual(body, {"status": "ok"})


class TestUniqueExpansion(ApiTestBase):
    def test_spec_example_b_expands_to_103(self):
        code, body = self.post(UNIQUE_PAYLOAD)
        self.assertEqual(code, 201)
        self.assertEqual(body["status"], "unique")
        ticks = {e["event"]: e["tick"]
                 for e in body["result"]["timeline"]["entries"]}
        self.assertEqual(ticks["anchor"], 95)
        self.assertEqual(ticks["B"], 103)
        self.assertFalse(body["replayed"])


class TestAmbiguous(ApiTestBase):
    def test_two_timelines_and_unstable_pair(self):
        code, body = self.post(AMBIGUOUS_PAYLOAD)
        self.assertEqual(code, 201)
        self.assertEqual(body["status"], "multiple")
        timelines = body["result"]["timelines"]
        self.assertEqual(len(timelines), 2)
        t1 = {e["event"]: e["tick"] for e in timelines[0]["entries"]}
        t2 = {e["event"]: e["tick"] for e in timelines[1]["entries"]}
        self.assertEqual(t1["B"], 3)
        self.assertEqual(t2["B"], 103)
        unstable = body["result"]["first_unstable_precedence"]
        self.assertEqual(unstable["pair"], ["anchor", "B"])


class TestUnsat(ApiTestBase):
    def test_bidirectional_conflict_chain(self):
        code, body = self.post(UNSAT_PAYLOAD)
        self.assertEqual(code, 201)
        self.assertEqual(body["status"], "unsat")
        conflict = body["result"]["conflict"]
        self.assertEqual(sorted(conflict["constraint_chain"]), ["ab", "ba"])
        self.assertEqual(conflict["lower_bound_derivation"]
                         ["derived_tick_min"], 103)
        self.assertEqual(conflict["upper_bound_derivation"]
                         ["derived_tick_max"], 3)


class TestIdempotency(ApiTestBase):
    def test_replay_returns_same_audit_number(self):
        code1, body1 = self.post(UNIQUE_PAYLOAD, request_id="req-idem-1")
        code2, body2 = self.post(UNIQUE_PAYLOAD, request_id="req-idem-1")
        self.assertEqual((code1, code2), (201, 200))
        self.assertEqual(body1["audit_no"], body2["audit_no"])
        self.assertTrue(body2["replayed"])

    def test_changed_payload_rejected_without_new_record(self):
        changed = json.loads(json.dumps(UNSAT_PAYLOAD))
        changed["constraints"][0]["window"] = [8, 9]  # modify one constraint
        code1, _ = self.post(UNSAT_PAYLOAD, request_id="req-idem-2")
        code2, body2 = self.post(changed, request_id="req-idem-2")
        self.assertEqual(code1, 201)
        self.assertEqual(code2, 409)
        self.assertIn("error", body2)
        # the original record is still intact and no new record appeared
        code3, body3 = self.post(UNSAT_PAYLOAD, request_id="req-idem-2")
        self.assertEqual(code3, 200)
        self.assertEqual(body3["status"], "unsat")

    def test_changed_event_rejected(self):
        changed = json.loads(json.dumps(UNIQUE_PAYLOAD))
        changed["events"][0]["counter"] = 4
        code1, _ = self.post(UNIQUE_PAYLOAD, request_id="req-idem-3")
        code2, _ = self.post(changed, request_id="req-idem-3")
        self.assertEqual((code1, code2), (201, 409))


class TestFrozenRecord(ApiTestBase):
    def test_get_returns_frozen_input_conclusion_evidence(self):
        code1, created = self.post(AMBIGUOUS_PAYLOAD,
                                   request_id="req-frozen-1")
        self.assertEqual(code1, 201)
        code2, rec = self.get(f"/audits/{created['audit_no']}")
        self.assertEqual(code2, 200)
        self.assertEqual(rec["request_id"], "req-frozen-1")
        self.assertEqual(rec["input"]["modulus"], 100)
        self.assertEqual(rec["input"]["events"],
                         AMBIGUOUS_PAYLOAD["events"])
        self.assertEqual(rec["result"]["status"], "multiple")
        self.assertIn("timelines", rec["result"])
        self.assertIn("first_unstable_precedence", rec["result"])

    def test_get_missing_returns_404(self):
        code, _ = self.get("/audits/999999")
        self.assertEqual(code, 404)


class TestValidation(ApiTestBase):
    def test_disconnected_event_rejected_400(self):
        payload = {
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 3}, {"id": "Z", "counter": 1}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
            ],
        }
        code, body = self.post(payload, request_id="req-bad-1")
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_missing_request_id_400(self):
        payload = {k: v for k, v in UNIQUE_PAYLOAD.items()
                   if k != "request_id"}
        code, _ = self.post(payload)
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
