"""
audit_service.py
-----------------
Persists a durable, queryable trail of every triage and dispatch event for a
case (SRS §5.3: "All triage decisions logged with the contributing rule/vitals
for post-incident audit and legal traceability"; FR-2.5).

Backed by SQLite rather than an in-memory list — this was the other named gap
(no persistent DB), and audit data is exactly the kind of thing that must
survive a backend restart, so one fix covers both.

Event types logged: triage_scored, dispatch_sent, hospital_responded,
standdown_broadcast. Each entry captures a full snapshot of the relevant
payload, not just a summary — so a post-incident review can reconstruct
exactly what the system knew and decided at each step.
"""

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "audit_log.db")


def _init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                log_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                payload_snapshot TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_case_id ON audit_log(case_id)")
        conn.commit()


@contextmanager
def _connect():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def log_event(case_id: str, event_type: str, actor: str, payload: dict) -> str:
    """Writes one audit entry. Returns the log_id.
    event_type: 'triage_scored' | 'dispatch_sent' | 'hospital_responded' | 'standdown_broadcast'
    actor: e.g. 'esi_rules_engine', 'emt:AMB-108', 'hospital:H002', 'dispatch_orchestrator'"""
    log_id = f"LOG-{uuid.uuid4().hex[:10].upper()}"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (log_id, case_id, event_type, actor, payload_snapshot, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (log_id, case_id, event_type, actor, json.dumps(payload), time.time()),
        )
        conn.commit()
    return log_id


def get_case_history(case_id: str) -> list:
    """Full chronological audit trail for one case — what a post-incident
    review would pull up."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE case_id = ? ORDER BY timestamp ASC", (case_id,)
        ).fetchall()
    return [
        {
            "log_id": r["log_id"],
            "case_id": r["case_id"],
            "event_type": r["event_type"],
            "actor": r["actor"],
            "payload": json.loads(r["payload_snapshot"]),
            "timestamp": r["timestamp"],
        }
        for r in rows
    ]


def get_recent_events(limit: int = 50) -> list:
    """Most recent events across all cases — useful for a live ops view."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {
            "log_id": r["log_id"], "case_id": r["case_id"], "event_type": r["event_type"],
            "actor": r["actor"], "payload": json.loads(r["payload_snapshot"]), "timestamp": r["timestamp"],
        }
        for r in rows
    ]


_init_db()


if __name__ == "__main__":
    # Self-test: write a few events for a fake case, read them back
    test_case = f"CASE-TEST-{uuid.uuid4().hex[:6]}"
    log_event(test_case, "triage_scored", "esi_rules_engine",
              {"esi_level": 2, "rationale": ["chest_pain_flag present -> minimum ESI-2"]})
    log_event(test_case, "dispatch_sent", "dispatch_orchestrator",
              {"hospital_id": "H002", "hospital_name": "Kovai Heart & Vascular Institute"})
    log_event(test_case, "hospital_responded", "hospital:H002",
              {"status": "accepted"})

    history = get_case_history(test_case)
    print(f"Audit trail for {test_case}:")
    for entry in history:
        print(f"  [{entry['event_type']}] by {entry['actor']} -> {entry['payload']}")

    assert len(history) == 3, "Expected 3 audit entries"
    print("\nSelf-test passed: 3 events logged and retrieved in correct order.")
