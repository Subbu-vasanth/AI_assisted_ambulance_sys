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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vitals_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                heart_rate_bpm REAL,
                spo2_percent REAL,
                bp_systolic_mmhg REAL,
                temperature_celsius REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_req_case ON dispatch_requests(case_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vitals_hist ON vitals_history(case_id, timestamp DESC)")
        # Safe migration: add ambulance position + arriving_soon columns if missing.
        # ALTER TABLE ... ADD COLUMN is idempotent-safe in SQLite (errors silently
        # if the column already exists, which we catch and ignore).
        for col_sql in [
            "ALTER TABLE cases ADD COLUMN ambulance_lat REAL",
            "ALTER TABLE cases ADD COLUMN ambulance_lng REAL",
            "ALTER TABLE cases ADD COLUMN arriving_soon_sent INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cases ADD COLUMN arrived_sent INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cases ADD COLUMN latest_vitals TEXT",
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
            "INSERT OR REPLACE INTO dispatch_requests "
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
        "latest_vitals": json.loads(row["latest_vitals"]) if ("latest_vitals" in row.keys() and row["latest_vitals"]) else None,
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


def update_latest_vitals(case_id: str, vitals: dict):
    """Overwrites the case's latest vitals snapshot (single field, not history)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE cases SET latest_vitals = ? WHERE case_id = ?",
            (json.dumps(vitals), case_id),
        )
        conn.commit()


def get_latest_vitals(case_id: str):
    """Returns the latest vitals dict or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT latest_vitals FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
    if row and row["latest_vitals"]:
        return json.loads(row["latest_vitals"])
    return None


def add_vitals_history(case_id: str, vitals: dict):
    """Appends a vitals log entry and prunes older entries beyond the last 20."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO vitals_history (case_id, timestamp, heart_rate_bpm, spo2_percent, bp_systolic_mmhg, temperature_celsius) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                case_id,
                time.time(),
                vitals.get("heart_rate_bpm"),
                vitals.get("spo2_percent"),
                vitals.get("bp_systolic_mmhg"),
                vitals.get("temperature_celsius"),
            ),
        )
        # Cap at last 20 rows per case, drop oldest beyond that
        conn.execute(
            "DELETE FROM vitals_history WHERE case_id = ? AND id NOT IN ("
            "  SELECT id FROM vitals_history WHERE case_id = ? ORDER BY timestamp DESC, id DESC LIMIT 20"
            ")",
            (case_id, case_id),
        )
        conn.commit()


def get_vitals_history(case_id: str) -> list:
    """Returns vitals history list for a case, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT timestamp, heart_rate_bpm, spo2_percent, bp_systolic_mmhg, temperature_celsius "
            "FROM vitals_history WHERE case_id = ? ORDER BY timestamp DESC, id DESC",
            (case_id,),
        ).fetchall()
    return [
        {
            "timestamp": r["timestamp"],
            "heart_rate_bpm": r["heart_rate_bpm"],
            "spo2_percent": r["spo2_percent"],
            "bp_systolic_mmhg": r["bp_systolic_mmhg"],
            "temperature_celsius": r["temperature_celsius"],
        }
        for r in rows
    ]


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


def get_active_requests_by_hospital(hospital_id: str) -> list:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT r.*, c.triage, c.winner_hospital_id, c.ranking_started, c.ambulance_lat, c.ambulance_lng, c.latest_vitals, c.arrived_sent
            FROM dispatch_requests r
            JOIN cases c ON r.case_id = c.case_id
            WHERE r.hospital_id = ? AND r.status != 'declined' AND IFNULL(c.arrived_sent, 0) = 0
            ORDER BY r.sent_at DESC
            """,
            (hospital_id,)
        ).fetchall()
        
    results = []
    for r in rows:
        db_status = r["status"]
        winner = r["winner_hospital_id"]
        
        if db_status == "accepted":
            if winner == hospital_id:
                status = "confirmed"
            elif winner is not None:
                status = "stood_down"
            else:
                status = "accepted"
        else:
            status = db_status
            
        results.append({
            "request_id": r["request_id"],
            "case_id": r["case_id"],
            "hospital_id": r["hospital_id"],
            "hospital_name": r["hospital_name"],
            "esi_level": r["esi_level"],
            "specialty": r["specialty"],
            "vitals": json.loads(r["latest_vitals"]) if (r["latest_vitals"]) else json.loads(r["vitals"]),
            "symptom_text": r["symptom_text"],
            "eta_minutes": r["eta_minutes"],
            "distance_km": r["distance_km"],
            "status": status,
            "sent_at": r["sent_at"],
            "response_at": r["response_at"],
            "triage": json.loads(r["triage"]) if r["triage"] else {},
            "ambulance_lat": r["ambulance_lat"],
            "ambulance_lng": r["ambulance_lng"],
        })
    return results


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

    for i in range(25):
        add_vitals_history(test_case, {"heart_rate_bpm": 80 + i, "spo2_percent": 98, "bp_systolic_mmhg": 120, "temperature_celsius": 37.0})
    hist = get_vitals_history(test_case)
    assert len(hist) == 20, f"Expected 20 history entries, got {len(hist)}"
    assert hist[0]["heart_rate_bpm"] == 104, f"Expected newest HR=104, got {hist[0]['heart_rate_bpm']}"

    assert case["winner"] == "H001"
    assert len(reqs) == 2
    assert reqs[0]["status"] in ("accepted", "pending")
    print("\nSelf-test passed: state persisted and correctly retrievable.")
