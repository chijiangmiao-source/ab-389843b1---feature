"""Persistent audit record store with idempotent creation semantics.

Semantics required by the audit protocol:

  * ``create`` with a ``request_id`` never seen before solves the payload and
    stores input + conclusion + evidence frozen under a new audit number.
  * ``create`` with a known ``request_id`` and a byte-identical canonical
    payload replays the original record (same audit number, nothing new).
  * ``create`` with a known ``request_id`` but a different payload is
    rejected with :class:`PayloadConflict` and stores nothing.

Repairs (minimum window relaxations opened against frozen ``unsat`` audits)
follow the same rules under a distinct fix marker:

  * ``create_repair`` with a new ``fix_id`` freezes the marker, the source
    audit number and the recomputed minimum-cost repair evidence.
  * reusing a ``fix_id`` with an identical payload replays the original
    repair number; a changed payload is rejected with
    :class:`RepairConflict` and stores nothing.

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
CREATE TABLE IF NOT EXISTS repairs (
    repair_no        INTEGER PRIMARY KEY AUTOINCREMENT,
    fix_id           TEXT NOT NULL UNIQUE,
    source_audit_no  INTEGER NOT NULL,
    payload_hash     TEXT NOT NULL,
    request_json     TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL
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


class RepairConflict(Exception):
    """fix_id already exists with a different payload."""

    def __init__(self, fix_id, repair_no):
        super().__init__(
            f"fix_id {fix_id!r} already used with a different payload "
            f"(existing repair_no={repair_no}); refusing to create a new "
            f"repair result")
        self.fix_id = fix_id
        self.repair_no = repair_no


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

    # -- repairs -------------------------------------------------------------

    def find_repair(self, fix_id):
        row = self._conn.execute(
            "SELECT * FROM repairs WHERE fix_id = ?", (fix_id,)).fetchone()
        return self._repair_row_to_record(row) if row else None

    def create_repair(self, fix_id, source_audit_no, request, result):
        """Idempotent repair creation.  Returns (record, created_new: bool)."""
        fingerprint = canonical_fingerprint(request)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM repairs WHERE fix_id = ?", (fix_id,)
            ).fetchone()
            if row is not None:
                if row["payload_hash"] != fingerprint:
                    raise RepairConflict(fix_id, row["repair_no"])
                return self._repair_row_to_record(row), False
            now = datetime.now(timezone.utc).isoformat()
            try:
                cur = self._conn.execute(
                    "INSERT INTO repairs (fix_id, source_audit_no,"
                    " payload_hash, request_json, result_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (fix_id, source_audit_no, fingerprint,
                     json.dumps(request, sort_keys=True, ensure_ascii=False),
                     json.dumps(result, sort_keys=True, ensure_ascii=False), now),
                )
            except sqlite3.IntegrityError:
                # Concurrent creation with the same fix_id lost the race.
                row = self._conn.execute(
                    "SELECT * FROM repairs WHERE fix_id = ?", (fix_id,)
                ).fetchone()
                if row is not None and row["payload_hash"] == fingerprint:
                    return self._repair_row_to_record(row), False
                raise RepairConflict(
                    fix_id, row["repair_no"] if row else -1)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM repairs WHERE repair_no = ?",
                (cur.lastrowid,)).fetchone()
            return self._repair_row_to_record(row), True

    def get_repair(self, repair_no):
        row = self._conn.execute(
            "SELECT * FROM repairs WHERE repair_no = ?",
            (repair_no,)).fetchone()
        return self._repair_row_to_record(row) if row else None

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

    @staticmethod
    def _repair_row_to_record(row):
        return {
            "repair_no": row["repair_no"],
            "fix_id": row["fix_id"],
            "source_audit_no": row["source_audit_no"],
            "payload_hash": row["payload_hash"],
            "request": json.loads(row["request_json"]),
            "result": json.loads(row["result_json"]),
            "created_at": row["created_at"],
        }
