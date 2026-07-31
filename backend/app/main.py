"""
main.py
-------
Backend service (SRS §9) that:
  - accepts a case from the ambulance app (or the simulator, for demo)
  - runs it through the ESI triage engine
  - uses the MCP hospital-network tools to broadcast dispatch requests in parallel
  - pushes live updates to connected hospital dashboards over WebSocket
  - accepts real accept/decline responses from a dashboard (replaces the
    demo-only confirm_hospital_response tool once staff are actually in the loop)

Run: uvicorn main:app --reload --port 8000   (from backend/app/)
"""

import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv
# Explicit path — backend/.env, one level up from this file (backend/app/main.py).
# Without this, load_dotenv() only finds .env if uvicorn happens to be launched
# from backend/ itself, which is exactly the kind of "works on my machine"
# bug that's easy to hit depending on which folder you cd into first.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

# --- wire up sibling modules (shared/, mcp-hospital-server/) ---
_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "..", "..", "shared"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "mcp-hospital-server"))
sys.path.insert(0, os.path.join(_HERE, "triage_engine"))

from esi_rules import score_case                     # noqa: E402
from symptom_parser import parse_symptoms, merge_flags  # noqa: E402
from vitals_generator import generate_case, SCENARIOS  # noqa: E402
import mcp_hospital_server as hosp                    # noqa: E402
from services.websocket_manager import manager        # noqa: E402
from services.whisper_service import transcribe_audio  # noqa: E402
from services import audit_service                     # noqa: E402
from services import dispatch_store as store            # noqa: E402
from services import routing_service                   # noqa: E402

app = FastAPI(title="Connected Ambulance System API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_FRONTEND_ROOT = os.path.join(_HERE, "..", "..")
app.mount("/ambulance", StaticFiles(directory=os.path.join(_FRONTEND_ROOT, "ambulance-app"), html=True), name="ambulance-app")
app.mount("/dashboard", StaticFiles(directory=os.path.join(_FRONTEND_ROOT, "hospital-dashboard"), html=True), name="hospital-dashboard")

# Create the ambulance-driver directory if it doesn't exist yet (avoids startup crash
# during the build phase when the file hasn't been created)
_DRIVER_DIR = os.path.join(_FRONTEND_ROOT, "ambulance-driver")
os.makedirs(_DRIVER_DIR, exist_ok=True)
app.mount("/driver", StaticFiles(directory=_DRIVER_DIR, html=True), name="ambulance-driver")


def haversine_eta_minutes(dist_km, avg_speed_kmh=35):
    return round((dist_km / avg_speed_kmh) * 60, 1)


def build_flags(symptom_text: str, manual_flags: dict) -> dict:
    """LLM-parses symptom_text (FR-2.2) and merges with any manually-tapped
    flags from the EMT app; manual always wins (see symptom_parser.merge_flags)."""
    llm_flags = parse_symptoms(symptom_text)
    return merge_flags(manual_flags, llm_flags)


# ---------------------------------------------------------------- schemas ---
class VitalsIn(BaseModel):
    heart_rate_bpm: float
    spo2_percent: float
    bp_systolic_mmhg: float
    temperature_celsius: float


class LocationIn(BaseModel):
    lat: float
    lng: float
    area: str = ""


class CaseIn(BaseModel):
    ambulance_id: str
    vitals: VitalsIn
    symptom_text: str
    symptom_flags: dict = {}
    location: LocationIn
    radius_km: float = 8.0


class RespondIn(BaseModel):
    status: str  # "accepted" | "declined"


# ---------------------------------------------------------------- routes ---
@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """Symptom voice capture via Whisper (FR-1.3) — chosen over browser
    speech recognition specifically for noise tolerance in a moving ambulance."""
    audio_bytes = await audio.read()
    result = transcribe_audio(audio_bytes, filename=audio.filename or "symptom.webm")
    if not result["success"]:
        raise HTTPException(422, result["error"])
    return {"text": result["text"]}


@app.get("/api/hospitals")
def list_hospitals():
    """Public directory — access_key is stripped before returning; it's a
    credential, not directory data (see /api/auth/login)."""
    hospitals = json.loads(hosp.list_hospitals())
    return [{k: v for k, v in h.items() if k != "access_key"} for h in hospitals]


class LoginIn(BaseModel):
    hospital_id: str
    access_key: str


@app.post("/api/auth/login")
def hospital_login(body: LoginIn):
    """Lightweight per-hospital credential check (SRS §5.4). This is a
    hackathon-scale gate — a per-hospital shared key, not full role-based
    auth/session tokens — but it stops the dashboard being wide open to
    anyone who finds the URL, which the SRS explicitly calls out as a gap."""
    hospital = hosp.HOSPITALS.get(body.hospital_id)
    if not hospital or hospital.get("access_key") != body.access_key:
        raise HTTPException(401, "Invalid hospital_id or access_key")
    return {"status": "ok", "hospital_id": body.hospital_id, "hospital_name": hospital["name"]}


class CapacityUpdateIn(BaseModel):
    capacity_status: str


@app.post("/api/hospitals/{hospital_id}/capacity")
def update_hospital_capacity_route(hospital_id: str, body: CapacityUpdateIn, x_hospital_key: str = Header(...)):
    """Update hospital capacity status (available, busy, or full). Requires authentication with X-Hospital-Key."""
    _verify_hospital_key(hospital_id, x_hospital_key)
    if body.capacity_status not in ("available", "busy", "full"):
        raise HTTPException(400, "capacity_status must be 'available', 'busy', or 'full'")
    hosp.update_hospital_capacity(hospital_id, body.capacity_status)
    return {"status": "ok", "hospital_id": hospital_id, "capacity_status": body.capacity_status}


def _verify_hospital_key(hospital_id: str, key: str):
    hospital = hosp.HOSPITALS.get(hospital_id)
    if not hospital or hospital.get("access_key") != key:
        raise HTTPException(401, "Invalid or missing hospital access key")


@app.get("/api/hospitals/{hospital_id}/active-requests")
def get_active_requests(hospital_id: str, x_hospital_key: str = Header(...)):
    """Retrieve all non-terminal requests for this hospital to reconcile UI state on reconnect."""
    _verify_hospital_key(hospital_id, x_hospital_key)
    requests = store.get_active_requests_by_hospital(hospital_id)
    hospital_data = hosp.HOSPITALS.get(hospital_id, {})
    for r in requests:
        r["on_duty_doctor_name"] = hospital_data.get("on_duty_doctor_name", "")
        r["on_duty_doctor_phone"] = hospital_data.get("on_duty_doctor_phone", "")
        r["emergency_team_phone"] = hospital_data.get("emergency_team_phone", "")
    return requests



@app.get("/api/scenarios")
def list_scenarios():
    return {"scenarios": list(SCENARIOS.keys())}


@app.post("/api/triage-preview")
def triage_preview(case_in: CaseIn):
    """Scores a case WITHOUT dispatching (FR-2.4) — the EMT app calls this
    first, shows the AI-suggested ESI level + rationale, and only calls
    /api/cases (which does dispatch) once the EMT explicitly confirms."""
    flags = build_flags(case_in.symptom_text, case_in.symptom_flags)
    triage = score_case(case_in.vitals.dict(), flags)
    return {"triage": triage, "flags_used": flags}


@app.post("/api/simulate/{scenario_name}")
async def simulate_case(scenario_name: str):
    """Demo helper: generates a realistic case for the given scenario and
    immediately dispatches it, exactly like a real ambulance submission would."""
    if scenario_name not in SCENARIOS:
        raise HTTPException(404, f"Unknown scenario '{scenario_name}'. Options: {list(SCENARIOS.keys())}")
    case = generate_case(scenario_name)
    case_in = CaseIn(
        ambulance_id=case["ambulance_id"],
        vitals=VitalsIn(**case["vitals"]),
        symptom_text=case["symptom_text"],
        symptom_flags=case["symptom_flags"],
        location=LocationIn(**case["location"]),
    )
    return await submit_case(case_in, case_id_override=case["case_id"])


@app.post("/api/cases")
async def submit_case(case_in: CaseIn, case_id_override: str = None):
    case_id = case_id_override or f"CASE-{int(time.time() * 1000) % 10_000_000}"

    # 1. Triage (deterministic ESI engine — see triage_engine/esi_rules.py).
    #    symptom_text is LLM-parsed into flags and merged with any manual taps (FR-2.2/2.3).
    flags = build_flags(case_in.symptom_text, case_in.symptom_flags)
    triage = score_case(case_in.vitals.dict(), flags)

    audit_service.log_event(case_id, "triage_scored", "esi_rules_engine", {
        "esi_level": triage["esi_level"], "label": triage["label"], "specialty": triage["specialty"],
        "rationale": triage["rationale"], "vitals": case_in.vitals.dict(), "flags_used": flags,
        "symptom_text": case_in.symptom_text,
    })

    # 2. Find matching hospitals; auto-widen radius once if nothing found (FR-3.5)
    query = hosp.NearbyQuery(lat=case_in.location.lat, lng=case_in.location.lng,
                              specialty=triage["specialty"], radius_km=case_in.radius_km)
    result = json.loads(hosp.find_matching_hospitals(query))
    if not result["candidates"]:
        query.radius_km *= 2
        result = json.loads(hosp.find_matching_hospitals(query))

    # 3. Persist the case BEFORE broadcasting, so dispatch_sent events always
    #    have a parent case row (crash-safety — see dispatch_store.py)
    store.save_case(case_id, triage)
    store.update_ambulance_position(case_id, case_in.location.lat, case_in.location.lng)

    # 4. Parallel broadcast (FR-3.3) — send to every candidate, then push a
    #    live WebSocket event to each hospital's connected dashboard
    sent = []
    for c in result["candidates"]:
        dispatch_input = hosp.DispatchInput(
            case_id=case_id, hospital_id=c["hospital_id"],
            esi_level=triage["esi_level"], specialty=triage["specialty"],
            vitals=case_in.vitals.dict(), symptom_text=case_in.symptom_text,
            eta_minutes=haversine_eta_minutes(c["distance_km"]), distance_km=c["distance_km"],
        )
        resp = json.loads(hosp.send_dispatch_request(dispatch_input))
        sent.append({**resp, "hospital_id": c["hospital_id"], "distance_km": c["distance_km"]})

        audit_service.log_event(case_id, "dispatch_sent", "dispatch_orchestrator", {
            "request_id": resp["request_id"], "hospital_id": c["hospital_id"],
            "hospital_name": resp["sent_to"], "distance_km": c["distance_km"],
            "eta_minutes": dispatch_input.eta_minutes,
        })

        hospital_data = hosp.HOSPITALS.get(c["hospital_id"], {})
        await manager.send_to_hospital(c["hospital_id"], {
            "type": "new_request",
            "request_id": resp["request_id"],
            "case_id": case_id,
            "esi_level": triage["esi_level"],
            "esi_label": triage["label"],
            "specialty": triage["specialty"],
            "rationale": triage["rationale"],
            "vitals": case_in.vitals.dict(),
            "symptom_text": case_in.symptom_text,
            "eta_minutes": dispatch_input.eta_minutes,
            "distance_km": c["distance_km"],
            "ambulance_lat": case_in.location.lat,
            "ambulance_lng": case_in.location.lng,
            "on_duty_doctor_name": hospital_data.get("on_duty_doctor_name", ""),
            "on_duty_doctor_phone": hospital_data.get("on_duty_doctor_phone", ""),
            "emergency_team_phone": hospital_data.get("emergency_team_phone", ""),
        })

    # 5. Schedule timeout for auto-widening if no hospital confirms (FR-3.5)
    asyncio.create_task(_check_and_widen_dispatch(
        case_id=case_id,
        lat=case_in.location.lat,
        lng=case_in.location.lng,
        specialty=triage["specialty"],
        current_radius=query.radius_km,
        symptom_text=case_in.symptom_text,
        vitals=case_in.vitals.dict(),
        initial_radius_km=case_in.radius_km
    ))

    return {
        "case_id": case_id,
        "triage": triage,
        "broadcast_to": [{"hospital_id": s["hospital_id"], "hospital_name": s["sent_to"],
                           "request_id": s["request_id"]} for s in sent],
    }


# How long to wait for ANY hospital to accept before auto-widening the search
# radius (FR-3.5). This is the PRE-ACCEPT timeout — distinct from
# ACCEPT_HOLD_WINDOW_SECONDS which is the POST-ACCEPT ranking window.
DISPATCH_TIMEOUT_SECONDS = float(os.environ.get("DISPATCH_TIMEOUT_SECONDS", "25.0"))
MAX_RADIUS_KM = 32.0


async def _check_and_widen_dispatch(case_id: str, lat: float, lng: float, specialty: str, current_radius: float, symptom_text: str, vitals: dict, initial_radius_km: float):
    """
    FR-3.5: If no hospital confirms within a timeout, auto-widen the radius and re-broadcast.
    Runs as a background task.
    """
    await asyncio.sleep(DISPATCH_TIMEOUT_SECONDS)

    # 1. Check if the case is already resolved or in the ranking phase
    case = store.get_case(case_id)
    if not case:
        return
    if case["winner"] is not None or case["ranking_started"] or case["candidate_accepts"]:
        return  # already accepted/resolved or ranking has started

    # 2. Widen the radius. Loop to widen immediately (zero-delay) if we find no candidates,
    # up to the MAX_RADIUS_KM limit.
    new_radius = current_radius
    new_candidates = []

    while new_radius < MAX_RADIUS_KM and not new_candidates:
        previous_radius = new_radius
        new_radius = new_radius * 2
        
        # Find matching hospitals in the new radius
        query = hosp.NearbyQuery(lat=lat, lng=lng, specialty=specialty, radius_km=new_radius)
        result = json.loads(hosp.find_matching_hospitals(query))
        candidates = result.get("candidates", [])

        # Filter out hospitals we already sent requests to
        existing_requests = store.get_requests_by_case(case_id)
        notified_hospital_ids = {r["hospital_id"] for r in existing_requests}
        new_candidates = [c for c in candidates if c["hospital_id"] not in notified_hospital_ids]

        if not new_candidates:
            audit_service.log_event(case_id, "radius_widened_no_candidates", "dispatch_orchestrator", {
                "previous_radius_km": previous_radius,
                "attempted_radius_km": new_radius,
                "message": "No new candidates found in this radius, expanding further."
            })

    if (new_radius > MAX_RADIUS_KM and not new_candidates) or not new_candidates:
        audit_service.log_event(case_id, "dispatch_timeout_failed", "dispatch_orchestrator", {
            "error": f"Reached maximum search radius of {MAX_RADIUS_KM}km without hospital confirmation.",
            "final_radius_km": min(new_radius, MAX_RADIUS_KM)
        })
        return

    # 3. Double-check case status immediately before logging/broadcasting (guards against race conditions)
    case = store.get_case(case_id)
    if not case or case["winner"] is not None or case["ranking_started"] or case["candidate_accepts"]:
        return

    # 4. Log the audit event for radius widening
    audit_service.log_event(case_id, "radius_widened", "dispatch_orchestrator", {
        "previous_radius_km": current_radius,
        "new_radius_km": new_radius,
        "new_candidates": [{"hospital_id": c["hospital_id"], "name": c["name"], "distance_km": c["distance_km"]} for c in new_candidates]
    })

    # 5. Save requests and broadcast to new candidates
    for c in new_candidates:
        # Double-check inside loop to handle race conditions during async socket transmits
        case = store.get_case(case_id)
        if not case or case["winner"] is not None or case["ranking_started"]:
            break

        dispatch_input = hosp.DispatchInput(
            case_id=case_id, hospital_id=c["hospital_id"],
            esi_level=case["triage"]["esi_level"], specialty=specialty,
            vitals=vitals, symptom_text=symptom_text,
            eta_minutes=haversine_eta_minutes(c["distance_km"]), distance_km=c["distance_km"],
        )
        resp = json.loads(hosp.send_dispatch_request(dispatch_input))

        audit_service.log_event(case_id, "dispatch_sent", "dispatch_orchestrator", {
            "request_id": resp["request_id"], "hospital_id": c["hospital_id"],
            "hospital_name": resp["sent_to"], "distance_km": c["distance_km"],
            "eta_minutes": dispatch_input.eta_minutes,
            "widen_stage": True
        })

        await manager.send_to_hospital(c["hospital_id"], {
            "type": "new_request",
            "request_id": resp["request_id"],
            "case_id": case_id,
            "esi_level": case["triage"]["esi_level"],
            "esi_label": case["triage"]["label"],
            "specialty": specialty,
            "rationale": case["triage"]["rationale"],
            "vitals": vitals,
            "symptom_text": symptom_text,
            "eta_minutes": dispatch_input.eta_minutes,
            "distance_km": c["distance_km"],
            "ambulance_lat": lat,
            "ambulance_lng": lng,
        })

    # 6. Only schedule the next timeout check if there's room to widen further.
    #    If we've already hit MAX_RADIUS_KM, the while loop above will immediately
    #    exhaust on the next cycle and log dispatch_timeout_failed for no reason.
    if new_radius < MAX_RADIUS_KM:
        asyncio.create_task(_check_and_widen_dispatch(
            case_id, lat, lng, specialty, new_radius, symptom_text, vitals, initial_radius_km
        ))



# How long to hold after the FIRST accept before ranking all accepts received
# during this window (FR-3.6). This is the POST-ACCEPT ranking window —
# distinct from DISPATCH_TIMEOUT_SECONDS which is the PRE-ACCEPT widen timeout.
ACCEPT_HOLD_WINDOW_SECONDS = 4.0


def _capacity_rank(status: str) -> int:
    """Lower is better. Shouldn't normally see 'full' here (find_matching_hospitals
    excludes it), but rank defensively in case capacity changed mid-flight."""
    return {"available": 0, "busy": 1, "full": 2}.get(status, 1)


async def _finalize_case_selection(case_id: str, skip_sleep: bool = False):
    """Waits out the hold window, then ranks every hospital that accepted
    during that window by distance and live capacity, and picks the best
    fit — not just whoever answered first (FR-3.6). Reads/writes through
    the persistent store so this survives a backend restart mid-window."""
    if not skip_sleep:
        await asyncio.sleep(ACCEPT_HOLD_WINDOW_SECONDS)

    case = store.get_case(case_id)
    if not case or case["winner"] is not None or not case["candidate_accepts"]:
        return  # already resolved some other way, or nobody actually accepted

    ranked = sorted(
        case["candidate_accepts"],
        key=lambda c: (_capacity_rank(c["capacity_status"]), c["distance_km"]),
    )
    winner = ranked[0]
    store.set_case_winner(case_id, winner["hospital_id"])

    standdown = json.loads(hosp.broadcast_standdown(
        hosp.StanddownInput(case_id=case_id, winning_hospital_id=winner["hospital_id"])
    ))

    audit_service.log_event(case_id, "standdown_broadcast", "dispatch_orchestrator", {
        "winner": standdown["winner"], "stood_down": standdown["stood_down"],
        "ranking_considered": [
            {"hospital_name": c["hospital_name"], "distance_km": c["distance_km"],
             "capacity_status": c["capacity_status"]} for c in ranked
        ],
    })

    all_requests = store.get_requests_by_case(case_id)
    for r in all_requests:
        if r["hospital_id"] != winner["hospital_id"]:
            await manager.send_to_hospital(r["hospital_id"], {
                "type": "stand_down", "case_id": case_id, "winner": standdown["winner"],
            })
    await manager.send_to_hospital(winner["hospital_id"], {
        "type": "confirmed",
        "case_id": case_id,
        "request_id": winner["request_id"],
        "ambulance_lat": case.get("ambulance_lat"),
        "ambulance_lng": case.get("ambulance_lng"),
    })


@app.post("/api/requests/{request_id}/respond")
async def respond_to_request(request_id: str, body: RespondIn, x_hospital_key: str = Header(...)):
    """Called by a hospital dashboard when staff click Accept/Decline (FR-4.3).
    Requires X-Hospital-Key matching the REQUEST'S OWNING hospital — a
    dashboard authenticated as H002 cannot respond on behalf of H001's
    request, even with a valid key for a different hospital.
    Accepts don't win instantly — they're held for ACCEPT_HOLD_WINDOW_SECONDS
    so other near-simultaneous accepts can be ranked together (FR-3.6), rather
    than the first hospital to click winning by pure luck of timing."""
    req = store.get_request(request_id)
    if not req:
        raise HTTPException(404, f"Unknown request_id '{request_id}'")
    if body.status not in ("accepted", "declined"):
        raise HTTPException(400, "status must be 'accepted' or 'declined'")

    _verify_hospital_key(req["hospital_id"], x_hospital_key)

    store.update_request_status(request_id, body.status)
    case_id = req["case_id"]
    case = store.get_case(case_id)

    audit_service.log_event(case_id, "hospital_responded", f"hospital:{req['hospital_id']}", {
        "request_id": request_id, "hospital_name": req["hospital_name"], "status": body.status,
    })

    if body.status != "accepted" or not case or case["winner"] is not None:
        return {"status": body.status, "case_id": case_id}

    hospital_meta = hosp.HOSPITALS.get(req["hospital_id"], {})
    store.add_candidate_accept(case_id, {
        "hospital_id": req["hospital_id"], "hospital_name": req["hospital_name"],
        "request_id": request_id, "distance_km": req.get("distance_km", 999),
        "capacity_status": hospital_meta.get("capacity_status", "unknown"),
    })

    all_requests = store.get_requests_by_case(case_id)

    # Single-candidate case (most demo scenarios): no point waiting out the
    # full window when there's nobody else to rank against.
    if len(all_requests) == 1:
        await _finalize_case_selection(case_id, skip_sleep=True)
        return {"status": "accepted", "case_id": case_id, "note": "only candidate — confirmed immediately"}

    if not case["ranking_started"]:
        store.set_ranking_started(case_id, True)
        asyncio.create_task(_finalize_case_selection(case_id))

    return {
        "status": "accepted", "case_id": case_id,
        "note": f"held for ranking against other accepts for {ACCEPT_HOLD_WINDOW_SECONDS}s (FR-3.6)",
    }


# -------------------------------------------------------- position tracking ---
class PositionIn(BaseModel):
    lat: float
    lng: float
    heart_rate_bpm: Optional[float] = None
    spo2_percent: Optional[float] = None
    bp_systolic_mmhg: Optional[float] = None
    temperature_celsius: Optional[float] = None


@app.post("/api/cases/{case_id}/position")
async def update_position(case_id: str, body: PositionIn):
    """Called every 30s by the ambulance driver app once a case is confirmed.
    Overwrites the case's latest position (single field, not history).
    If a winner exists, recomputes route/ETA and pushes to the hospital."""
    case = store.get_case(case_id)
    if not case:
        raise HTTPException(404, f"Unknown case_id '{case_id}'")

    store.update_ambulance_position(case_id, body.lat, body.lng)

    # Store latest vitals if provided (Feature 1: continuous vitals)
    live_vitals = {}
    if body.heart_rate_bpm is not None:
        live_vitals["heart_rate_bpm"] = body.heart_rate_bpm
    if body.spo2_percent is not None:
        live_vitals["spo2_percent"] = body.spo2_percent
    if body.bp_systolic_mmhg is not None:
        live_vitals["bp_systolic_mmhg"] = body.bp_systolic_mmhg
    if body.temperature_celsius is not None:
        live_vitals["temperature_celsius"] = body.temperature_celsius
    if live_vitals:
        store.update_latest_vitals(case_id, live_vitals)

    store.add_vitals_history(
        case_id,
        {
            "heart_rate_bpm": body.heart_rate_bpm,
            "spo2_percent": body.spo2_percent,
            "bp_systolic_mmhg": body.bp_systolic_mmhg,
            "temperature_celsius": body.temperature_celsius,
        },
    )

    result = {"status": "position_updated", "case_id": case_id}

    # If a winner exists, recompute route and push to hospital
    if case["winner"]:
        hospital = hosp.HOSPITALS.get(case["winner"])
        if hospital:
            # Wrap the blocking network call in asyncio.to_thread
            route = await asyncio.to_thread(
                routing_service.get_route, body.lat, body.lng, hospital["lat"], hospital["lng"]
            )
            result["distance_km"] = route["distance_km"]
            result["eta_minutes"] = route["eta_minutes"]

            # Push ambulance_position event to the winning hospital
            await manager.send_to_hospital(case["winner"], {
                "type": "ambulance_position",
                "case_id": case_id,
                "lat": body.lat,
                "lng": body.lng,
                "distance_km": route["distance_km"],
                "eta_minutes": route["eta_minutes"],
                **(live_vitals if live_vitals else {}),
            })

            # Fire arriving_soon exactly once when ETA first drops below 5 min
            if route["eta_minutes"] < 5 and not store.is_arriving_soon_sent(case_id):
                store.mark_arriving_soon_sent(case_id)
                await manager.send_to_hospital(case["winner"], {
                    "type": "arriving_soon",
                    "case_id": case_id,
                    "eta_minutes": route["eta_minutes"],
                })

            # Fire arrived exactly once when ETA is ~0
            if route["eta_minutes"] <= 0.2 and not store.is_arrived_sent(case_id):
                store.mark_arrived_sent(case_id)
                await manager.send_to_hospital(case["winner"], {
                    "type": "arrived",
                    "case_id": case_id,
                })

    return result


@app.get("/api/cases/{case_id}/route")
def get_case_route(case_id: str):
    """Returns the full traffic-annotated route from the ambulance's latest
    known position to the winning hospital's coordinates."""
    case = store.get_case(case_id)
    if not case:
        raise HTTPException(404, f"Unknown case_id '{case_id}'")
    if not case["winner"]:
        raise HTTPException(404, "No winner assigned yet — route not available")

    pos = store.get_ambulance_position(case_id)
    if not pos:
        raise HTTPException(404, "No ambulance position recorded yet")

    hospital = hosp.HOSPITALS.get(case["winner"])
    if not hospital:
        raise HTTPException(404, f"Winner hospital '{case['winner']}' not found")

    route = routing_service.get_route(pos[0], pos[1], hospital["lat"], hospital["lng"])
    return {
        "case_id": case_id,
        "ambulance": {"lat": pos[0], "lng": pos[1]},
        "hospital": {"lat": hospital["lat"], "lng": hospital["lng"], "name": hospital["name"]},
        **route,
    }


@app.get("/api/cases/{case_id}/status")
def get_case_status(case_id: str):
    """Returns case resolution status — the ambulance app polls this to know
    when to switch to the route/navigation screen."""
    case = store.get_case(case_id)
    if not case:
        raise HTTPException(404, f"Unknown case_id '{case_id}'")

    winner_hospital = hosp.HOSPITALS.get(case["winner"]) if case["winner"] else None
    return {
        "case_id": case_id,
        "status": "resolved" if case["winner"] else "pending",
        "winner_hospital_id": case["winner"],
        "winner_hospital_name": winner_hospital["name"] if winner_hospital else None,
        "winner_lat": winner_hospital["lat"] if winner_hospital else None,
        "winner_lng": winner_hospital["lng"] if winner_hospital else None,
    }


@app.get("/api/cases/{case_id}/vitals-history")
def get_case_vitals_history(case_id: str):
    """Returns the last 20 vitals history entries for a case, newest first."""
    case = store.get_case(case_id)
    if not case:
        raise HTTPException(404, f"Unknown case_id '{case_id}'")
    return {"case_id": case_id, "history": store.get_vitals_history(case_id)}


@app.websocket("/ws/hospital/{hospital_id}")
async def hospital_ws(websocket: WebSocket, hospital_id: str, key: str = ""):
    """Requires ?key=<access_key> matching this hospital_id. Rejected
    connections are closed with code 4401 before manager.connect() ever
    accepts them — an unauthenticated client never gets added to the
    broadcast list, so it can't see or act on any hospital's live requests."""
    hospital = hosp.HOSPITALS.get(hospital_id)
    if not hospital or hospital.get("access_key") != key:
        await websocket.close(code=4401, reason="Invalid or missing hospital access key")
        return

    await manager.connect(hospital_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # dashboard doesn't send anything meaningful; keep-alive
    except WebSocketDisconnect:
        manager.disconnect(hospital_id, websocket)


@app.get("/api/cases/{case_id}/audit")
def case_audit_trail(case_id: str):
    """Full chronological audit trail for one case (SRS §5.3, FR-2.5) — what
    a post-incident review would pull up: every rationale, dispatch, and
    response, in order, with full payload snapshots."""
    history = audit_service.get_case_history(case_id)
    if not history:
        raise HTTPException(404, f"No audit history for case_id '{case_id}'")
    return {"case_id": case_id, "events": history}


@app.get("/api/audit/recent")
def recent_audit_events(limit: int = 50):
    """Most recent events across all cases — live ops view."""
    return {"events": audit_service.get_recent_events(limit)}


@app.get("/")
def root():
    return {"status": "ok", "service": "Connected Ambulance System API"}
