"""HTTP API for the wraparound timestamp audit service.

Endpoints
---------
GET  /health                     -> 200 {"status": "ok"}
POST /audits                     -> create an audit (idempotent on request_id)
                                    201 new record, 200 replayed record,
                                    400 invalid payload, 409 request_id reuse
                                    with a different payload
GET  /audits/{number}            -> frozen record: input, conclusion & evidence
POST /audits/{number}/repairs    -> open a minimum window-relaxation repair
                                    against a frozen unsat audit (fix marker
                                    in the body); 201 new / 200 replayed,
                                    404 unknown source audit, 409 source is
                                    not unsat or fix_id reuse with a different
                                    payload
GET  /repairs/{number}           -> frozen repair: source reference, minimum
                                    cost, canonical timeline and evidence

Configuration via environment:
  PORT      listen port (default 8080)
  AUDIT_DB  SQLite file path (default /data/audits.db)
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .repair import RepairError, repair
from .solver import ValidationError, solve
from .store import AuditStore, PayloadConflict, RepairConflict

MAX_BODY = 1 << 20  # 1 MiB


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class AuditHandler(BaseHTTPRequestHandler):
    server_version = "WrapAudit/1.0"
    store: AuditStore = None  # injected by make_server

    # -- helpers --------------------------------------------------------------

    def _send(self, code, obj):
        body = _json_bytes(obj)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, {"error": message})

    def log_message(self, fmt, *args):  # keep container logs tidy but useful
        pass

    # -- routes ----------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, {"status": "ok"})
            return
        m = re.fullmatch(r"/audits/(\d+)", path)
        if m:
            rec = self.store.get(int(m.group(1)))
            if rec is None:
                self._error(404, f"audit {m.group(1)} not found")
            else:
                self._send(200, rec)
            return
        m = re.fullmatch(r"/repairs/(\d+)", path)
        if m:
            rec = self.store.get_repair(int(m.group(1)))
            if rec is None:
                self._error(404, f"repair {m.group(1)} not found")
            else:
                self._send(200, rec)
            return
        self._error(404, "not found")

    def _read_payload(self):
        """Parse one JSON request body; returns (payload, error_response).

        ``error_response`` is None on success; otherwise it is the
        (code, message) to send back.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, (400, "invalid Content-Length")
        if length <= 0 or length > MAX_BODY:
            return None, (400, "missing or oversized request body")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, (400, f"request body is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            return None, (400, "payload must be a JSON object")
        return payload, None

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        m = re.fullmatch(r"/audits/(\d+)/repairs", path)
        if m:
            self._post_repair(int(m.group(1)))
            return
        if path != "/audits":
            self._error(404, "not found")
            return
        payload, err = self._read_payload()
        if err is not None:
            self._error(*err)
            return

        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            self._error(400, "request_id must be a non-empty string")
            return

        body = {k: v for k, v in payload.items() if k != "request_id"}
        try:
            result = solve(body)
        except ValidationError as exc:
            self._error(400, str(exc))
            return

        try:
            record, created = self.store.create(request_id, body, result)
        except PayloadConflict as exc:
            self._error(409, str(exc))
            return

        self._send(201 if created else 200, {
            "audit_no": record["audit_no"],
            "request_id": record["request_id"],
            "status": record["result"]["status"],
            "result": record["result"],
            "replayed": not created,
        })

    def _post_repair(self, audit_no):
        payload, err = self._read_payload()
        if err is not None:
            self._error(*err)
            return
        source = self.store.get(audit_no)
        if source is None:
            self._error(404, f"audit {audit_no} not found; "
                             "cannot reference an unknown unsat record")
            return

        fix_id = payload.get("fix_id")
        if not isinstance(fix_id, str) or not fix_id:
            self._error(400, "fix_id must be a non-empty string")
            return

        # A fix marker may only carry a payload identical to the frozen source
        # (at most a benign echo); the source audit and its reading are never
        # rewritten.  The actual repair input is always the frozen input.
        echo = payload.get("payload")
        if echo is not None and echo != source["input"]:
            self._error(409, "repair payload must match the frozen source "
                             "audit input; source records cannot be rewritten")
            return

        request = {"source_audit_no": audit_no, "payload": source["input"]}

        # Idempotent replays are served without recomputation; an existing
        # fix_id with a different request is rejected and creates nothing.
        existing = self.store.find_repair(fix_id)
        if existing is not None:
            if existing["request"] != request:
                self._error(409, f"fix_id {fix_id!r} already used with a "
                                 f"different request (existing repair_no="
                                 f"{existing['repair_no']}); refusing to "
                                 f"create a new repair result")
                return
            self._send(200, self._repair_response(existing, replayed=True))
            return

        try:
            result = repair(source["input"], source["result"])
        except RepairError as exc:
            self._error(409, str(exc))
            return

        try:
            record, created = self.store.create_repair(
                fix_id, audit_no, request, result)
        except RepairConflict as exc:
            self._error(409, str(exc))
            return

        self._send(201 if created else 200,
                   self._repair_response(record, replayed=not created))

    @staticmethod
    def _repair_response(record, replayed):
        return {
            "repair_no": record["repair_no"],
            "fix_id": record["fix_id"],
            "source_audit_no": record["source_audit_no"],
            "status": record["result"]["status"],
            "total_expansion": record["result"]["total_expansion"],
            "result": record["result"],
            "replayed": replayed,
        }


def make_server(store: AuditStore, port: int) -> ThreadingHTTPServer:
    handler = type("BoundAuditHandler", (AuditHandler,), {"store": store})
    return ThreadingHTTPServer(("0.0.0.0", port), handler)


def main():
    port = int(os.environ.get("PORT", "8080"))
    db_path = os.environ.get("AUDIT_DB", "/data/audits.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    store = AuditStore(db_path)
    server = make_server(store, port)
    print(f"wrap-audit listening on 0.0.0.0:{port}, db={db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
