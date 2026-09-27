"""Persistent audit record store with idempotent creation semantics.

Semantics required by the audit protocol:

  * ``create`` with a ``request_id`` never seen before solves the payload and
    stores input + conclusion + evidence frozen under a new audit number.
  * ``create`` with a known ``request_id`` and a byte-identical canonical
    payload replays the original record (same audit number, nothing new).
  * ``create`` with a known ``request_id`` but a different payload is
    rejected with :class:`PayloadConflict` and stores nothing.

The payload fingerprint is the SHA-256 of its canonical JSON encoding
(sorted keys, compact separators), so any change to any event or constraint
changes the fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    audit_no      INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id    TEXT NOT NULL UNIQUE,
    payload_hash  TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    result_json   TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


class PayloadConflict(Exception):
    """request_id already exists with a different payload."""

    def __init__(self, request_id, audit_no):
        super().__init__(
            f"request_id {request_id!r} already used with a different payload "
            f"(existing audit_no={audit_no}); refusing to create a new record")
        self.request_id = request_id
        self.audit_no = audit_no


def canonical_fingerprint(payload) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AuditStore:
    def __init__(self, db_path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        self._conn.close()

    # -- writes --------------------------------------------------------------

    def find_by_request_id(self, request_id):
        row = self._conn.execute(
            "SELECT * FROM audits WHERE request_id = ?", (request_id,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def create(self, request_id, payload, result):
        """Idempotent create.  Returns (record, created_new: bool)."""
        fingerprint = canonical_fingerprint(payload)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM audits WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is not None:
                if row["payload_hash"] != fingerprint:
                    raise PayloadConflict(request_id, row["audit_no"])
                return self._row_to_record(row), False
            now = datetime.now(timezone.utc).isoformat()
            cur = self._conn.execute(
                "INSERT INTO audits (request_id, payload_hash, payload_json,"
                " result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (request_id, fingerprint,
                 json.dumps(payload, sort_keys=True, ensure_ascii=False),
                 json.dumps(result, sort_keys=True, ensure_ascii=False),
                 now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM audits WHERE audit_no = ?",
                (cur.lastrowid,)).fetchone()
            return self._row_to_record(row), True

    # -- reads ---------------------------------------------------------------

    def get(self, audit_no):
        row = self._conn.execute(
            "SELECT * FROM audits WHERE audit_no = ?", (audit_no,)).fetchone()
        return self._row_to_record(row) if row else None

    @staticmethod
    def _row_to_record(row):
        return {
            "audit_no": row["audit_no"],
            "request_id": row["request_id"],
            "payload_hash": row["payload_hash"],
            "input": json.loads(row["payload_json"]),
            "result": json.loads(row["result_json"]),
            "created_at": row["created_at"],
        }
