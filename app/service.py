"""HTTP API for the wraparound timestamp audit service.

Endpoints
---------
GET  /health           -> 200 {"status": "ok"}
POST /audits           -> create an audit (idempotent on request_id)
                          201 new record, 200 replayed record,
                          400 invalid payload, 409 request_id reuse with a
                          different payload
GET  /audits/{number}  -> frozen record: input, conclusion and evidence

Configuration via environment:
  PORT      listen port (default 8080)
  AUDIT_DB  SQLite file path (default /data/audits.db)
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .solver import ValidationError, solve
from .store import AuditStore, PayloadConflict

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
        self._error(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/audits":
            self._error(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "invalid Content-Length")
            return
        if length <= 0 or length > MAX_BODY:
            self._error(400, "missing or oversized request body")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._error(400, f"request body is not valid JSON: {exc}")
            return
        if not isinstance(payload, dict):
            self._error(400, "payload must be a JSON object")
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
