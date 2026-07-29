"""
dispatch_store.py
------------------
Persistent replacement for the in-memory DISPATCH_REQUESTS (mcp_hospital_server.py)
and ACTIVE_CASES (main.py) dicts. Backed by SQLite in the same spirit as
audit_service.py — a backend restart/crash mid-demo should not wipe out
whichever cases were actively in flight.

Two tables:
  - dispatch_requests: one row per hospital a case was broadcast to
  - cases: one row per patient case, tracking triage + winner + ranking state

JSON-serializable fields (vitals, sent list, candidate_accepts) are stored as
TEXT/JSON columns rather than normalized further — this is a hackathon-scale
store, not a production schema; see SRS §9 for the Postgres/Redis upgrade path.
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "dispatch_state.db")


def _init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatch_requests (
                request_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                hospital_id TEXT NOT NULL,
                hospital_name TEXT NOT NULL,
                esi_level INTEGER,
                specialty TEXT,
                vitals TEXT,
                symptom_text TEXT,
                eta_minutes REAL,
                distance_km REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                sent_at REAL NOT NULL,
                response_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                triage TEXT NOT NULL,
                winner_hospital_id TEXT,
                ranking_started INTEGER NOT NULL DEFAULT 0,
                candidate_accepts TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_req_case ON dispatch_requests(case_id)")
        # Safe migration: add ambulance position + arriving_soon columns if missing.
        # ALTER TABLE ... ADD COLUMN is idempotent-safe in SQLite (errors silently
        # if the column already exists, which we catch and ignore).
        for col_sql in [
            "ALTER TABLE cases ADD COLUMN ambulance_lat REAL",
            "ALTER TABLE cases ADD COLUMN ambulance_lng REAL",
            "ALTER TABLE cases ADD COLUMN arriving_soon_sent INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cases ADD COLUMN arrived_sent INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                conn.execute(col_sql)
            except Exception:
                pass  # column already exists
        conn.commit()


@contextmanager
def _connect():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- requests ---
def save_request(request_id: str, case_id: str, hospital_id: str, hospital_name: str,
                  esi_level: int, specialty: str, vitals: dict, symptom_text: str,
                  eta_minutes: float, distance_km: float):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO dispatch_requests "
            "(request_id, case_id, hospital_id, hospital_name, esi_level, specialty, "
            " vitals, symptom_text, eta_minutes, distance_km, status, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (request_id, case_id, hospital_id, hospital_name, esi_level, specialty,
             json.dumps(vitals), symptom_text, eta_minutes, distance_km, time.time()),
        )
        conn.commit()


def get_request(request_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM dispatch_requests WHERE request_id = ?", (request_id,)).fetchone()
    return _row_to_request(row) if row else None


def get_requests_by_case(case_id: str) -> list:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM dispatch_requests WHERE case_id = ?", (case_id,)).fetchall()
    return [_row_to_request(r) for r in rows]


def update_request_status(request_id: str, status: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE dispatch_requests SET status = ?, response_at = ? WHERE request_id = ?",
            (status, time.time(), request_id),
        )
        conn.commit()


def _row_to_request(row) -> dict:
    return {
        "request_id": row["request_id"], "case_id": row["case_id"],
        "hospital_id": row["hospital_id"], "hospital_name": row["hospital_name"],
        "esi_level": row["esi_level"], "specialty": row["specialty"],
        "vitals": json.loads(row["vitals"]), "symptom_text": row["symptom_text"],
        "eta_minutes": row["eta_minutes"], "distance_km": row["distance_km"],
        "status": row["status"], "sent_at": row["sent_at"], "response_at": row["response_at"],
    }


# -------------------------------------------------------------------- cases ---
def save_case(case_id: str, triage: dict):
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cases (case_id, triage, winner_hospital_id, ranking_started, candidate_accepts, created_at) "
            "VALUES (?, ?, NULL, 0, '[]', ?)",
            (case_id, json.dumps(triage), time.time()),
        )
        conn.commit()


def get_case(case_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    return _row_to_case(row) if row else None


def set_case_winner(case_id: str, hospital_id: str):
    with _connect() as conn:
        conn.execute("UPDATE cases SET winner_hospital_id = ? WHERE case_id = ?", (hospital_id, case_id))
        conn.commit()


def set_ranking_started(case_id: str, started: bool = True):
    with _connect() as conn:
        conn.execute("UPDATE cases SET ranking_started = ? WHERE case_id = ?", (1 if started else 0, case_id))
        conn.commit()


def add_candidate_accept(case_id: str, candidate: dict):
    """Appends to the candidate_accepts JSON list for FR-3.6 ranking."""
    with _connect() as conn:
        row = conn.execute("SELECT candidate_accepts FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        current = json.loads(row["candidate_accepts"]) if row else []
        current.append(candidate)
        conn.execute("UPDATE cases SET candidate_accepts = ? WHERE case_id = ?", (json.dumps(current), case_id))
        conn.commit()


def _row_to_case(row) -> dict:
    return {
        "case_id": row["case_id"], "triage": json.loads(row["triage"]),
        "winner": row["winner_hospital_id"], "ranking_started": bool(row["ranking_started"]),
        "candidate_accepts": json.loads(row["candidate_accepts"]), "created_at": row["created_at"],
        "ambulance_lat": row["ambulance_lat"] if "ambulance_lat" in row.keys() else None,
        "ambulance_lng": row["ambulance_lng"] if "ambulance_lng" in row.keys() else None,
        "arriving_soon_sent": bool(row["arriving_soon_sent"]) if "arriving_soon_sent" in row.keys() else False,
        "arrived_sent": bool(row["arrived_sent"]) if "arrived_sent" in row.keys() else False,
    }


# ---------------------------------------------------------- ambulance position ---
def update_ambulance_position(case_id: str, lat: float, lng: float):
    """Overwrites the case's latest known ambulance position (single field, not history)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE cases SET ambulance_lat = ?, ambulance_lng = ? WHERE case_id = ?",
            (lat, lng, case_id),
        )
        conn.commit()


def get_ambulance_position(case_id: str):
    """Returns (lat, lng) or None if no position has been recorded."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT ambulance_lat, ambulance_lng FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
    if row and row["ambulance_lat"] is not None and row["ambulance_lng"] is not None:
        return (row["ambulance_lat"], row["ambulance_lng"])
    return None


def mark_arriving_soon_sent(case_id: str):
    """Marks arriving_soon as sent so it fires exactly once per case."""
    with _connect() as conn:
        conn.execute(
            "UPDATE cases SET arriving_soon_sent = 1 WHERE case_id = ?", (case_id,)
        )
        conn.commit()


def is_arriving_soon_sent(case_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT arriving_soon_sent FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
    return bool(row["arriving_soon_sent"]) if row else False


def mark_arrived_sent(case_id: str):
    """Marks arrived as sent so it fires exactly once per case."""
    with _connect() as conn:
        conn.execute(
            "UPDATE cases SET arrived_sent = 1 WHERE case_id = ?", (case_id,)
        )
        conn.commit()


def is_arrived_sent(case_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT arrived_sent FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
    return bool(row["arrived_sent"]) if row else False


_init_db()


if __name__ == "__main__":
    # Self-test: save a case + two requests, update, retrieve, confirm survives "restart"
    # (re-opening the DB connection simulates a process restart since state isn't in-memory)
    test_case = "CASE-STORE-TEST"
    save_case(test_case, {"esi_level": 2, "label": "Emergent"})
    save_request("REQ-TEST-1", test_case, "H001", "Coimbatore General", 2, "cardiac",
                 {"heart_rate_bpm": 110}, "chest pain", 3.2, 1.8)
    save_request("REQ-TEST-2", test_case, "H002", "Kovai Heart", 2, "cardiac",
                 {"heart_rate_bpm": 110}, "chest pain", 4.1, 2.5)

    update_request_status("REQ-TEST-1", "accepted")
    add_candidate_accept(test_case, {"hospital_id": "H001", "distance_km": 1.8, "capacity_status": "available"})
    set_case_winner(test_case, "H001")

    case = get_case(test_case)
    reqs = get_requests_by_case(test_case)
    print("Case after 'restart' (fresh connection):", case)
    print(f"Requests for case ({len(reqs)}):")
    for r in reqs:
        print(f"  {r['hospital_name']}: {r['status']}")

    assert case["winner"] == "H001"
    assert len(reqs) == 2
    assert reqs[0]["status"] in ("accepted", "pending")
    print("\nSelf-test passed: state persisted and correctly retrievable.")
