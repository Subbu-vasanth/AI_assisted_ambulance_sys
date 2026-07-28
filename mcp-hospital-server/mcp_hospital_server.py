"""
mcp_hospital_server.py
-----------------------
Exposes the hospital network as MCP tools so an AI agent can orchestrate the
parallel-broadcast dispatch workflow (FR-3.3 through FR-3.7 in the SRS) by
calling real tools instead of hardcoded if/else logic.

Tools:
  - list_hospitals            : full directory (read-only)
  - find_matching_hospitals   : radius + specialty filtered candidates
  - send_dispatch_request     : notify a hospital of an incoming case
  - get_dispatch_status       : poll a request's current status
  - confirm_hospital_response : simulates a hospital accepting/declining (demo only)
  - broadcast_standdown       : notify losing hospitals once one is selected

Run: python mcp_hospital_server.py   (stdio transport, for local agent testing)
"""

import json
import math
import os
import random
import sys
import time
import uuid
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "app", "services"))
import dispatch_store as store  # noqa: E402 — persistent replacement for the old in-memory dict

mcp = FastMCP("hospital-network")

_HERE = os.path.dirname(__file__)
_HOSPITAL_DATA_PATH = os.path.join(_HERE, "..", "shared", "hospital_seed_data.json")

with open(_HOSPITAL_DATA_PATH) as f:
    HOSPITALS = {h["hospital_id"]: h for h in json.load(f)}


def _haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class NearbyQuery(BaseModel):
    lat: float = Field(..., description="Ambulance current latitude")
    lng: float = Field(..., description="Ambulance current longitude")
    specialty: str = Field(..., description="Required specialty, e.g. 'cardiac', 'neuro', 'orthopedic', 'trauma', 'pulmonology', 'general'")
    radius_km: float = Field(8.0, description="Search radius in kilometers")


@mcp.tool(
    name="find_matching_hospitals",
    description=(
        "Find hospitals within radius_km of the given coordinates that offer the required "
        "specialty and are not marked 'full'. Returns candidates sorted by distance, each "
        "with hospital_id, name, distance_km, capacity_status. Use this before "
        "send_dispatch_request to build your broadcast list."
    ),
)
def find_matching_hospitals(query: NearbyQuery) -> str:
    candidates = []
    for h in HOSPITALS.values():
        if query.specialty not in h["specialties"]:
            continue
        if h["capacity_status"] == "full":
            continue
        dist = _haversine_km(query.lat, query.lng, h["lat"], h["lng"])
        if dist <= query.radius_km:
            candidates.append({
                "hospital_id": h["hospital_id"],
                "name": h["name"],
                "distance_km": round(dist, 2),
                "capacity_status": h["capacity_status"],
                "specialties": h["specialties"],
            })
    candidates.sort(key=lambda c: c["distance_km"])
    if not candidates:
        return json.dumps({
            "candidates": [],
            "suggestion": f"No '{query.specialty}' hospitals within {query.radius_km}km. "
                           f"Retry with a larger radius_km (e.g. {query.radius_km * 2})."
        })
    return json.dumps({"candidates": candidates}, indent=2)


class DispatchInput(BaseModel):
    case_id: str = Field(..., description="Patient case identifier")
    hospital_id: str = Field(..., description="Target hospital_id from find_matching_hospitals")
    esi_level: int = Field(..., description="ESI urgency level 1-5 (1 = most critical)")
    specialty: str = Field(..., description="Required specialty for this case")
    vitals: dict = Field(..., description="heart_rate_bpm, spo2_percent, bp_systolic_mmhg, temperature_celsius")
    symptom_text: str = Field(..., description="Free-text symptom description from EMT")
    eta_minutes: float = Field(..., description="Estimated ambulance arrival time in minutes")
    distance_km: float = Field(0.0, description="Distance from ambulance to hospital, from find_matching_hospitals")


@mcp.tool(
    name="send_dispatch_request",
    description=(
        "Send a dispatch request with full patient data to one hospital. Call this once per "
        "candidate hospital to broadcast in parallel — do not wait for one response before "
        "calling the next. Returns a request_id to poll with get_dispatch_status."
    ),
)
def send_dispatch_request(input: DispatchInput) -> str:
    if input.hospital_id not in HOSPITALS:
        return json.dumps({"error": f"Unknown hospital_id '{input.hospital_id}'"})

    request_id = f"REQ-{uuid.uuid4().hex[:8].upper()}"
    store.save_request(
        request_id=request_id, case_id=input.case_id, hospital_id=input.hospital_id,
        hospital_name=HOSPITALS[input.hospital_id]["name"], esi_level=input.esi_level,
        specialty=input.specialty, vitals=input.vitals, symptom_text=input.symptom_text,
        eta_minutes=input.eta_minutes, distance_km=input.distance_km,
    )
    return json.dumps({"request_id": request_id, "status": "pending",
                        "sent_to": HOSPITALS[input.hospital_id]["name"]})


class StatusQuery(BaseModel):
    request_id: str = Field(..., description="Request ID returned by send_dispatch_request")


@mcp.tool(
    name="get_dispatch_status",
    description="Check the current status of a dispatch request: pending, accepted, or declined.",
)
def get_dispatch_status(query: StatusQuery) -> str:
    req = store.get_request(query.request_id)
    if not req:
        return json.dumps({"error": f"Unknown request_id '{query.request_id}'"})
    return json.dumps({"request_id": req["request_id"], "status": req["status"],
                        "hospital_name": req["hospital_name"]})


class SimulateResponseInput(BaseModel):
    request_id: str = Field(..., description="Request ID to simulate a hospital response for")
    force_status: Optional[str] = Field(
        None, description="Optional: force 'accepted' or 'declined' for demo control; omit for realistic random behavior"
    )


@mcp.tool(
    name="confirm_hospital_response",
    description=(
        "DEMO-ONLY tool: simulates a hospital's dashboard operator responding to a pending "
        "request, since no real hospital staff are in the loop during the hackathon demo. "
        "In production this is replaced by an actual dashboard button press (FR-4.3). "
        "Response likelihood is weighted by hospital capacity_status and avg_response_seconds."
    ),
)
def confirm_hospital_response(input: SimulateResponseInput) -> str:
    req = store.get_request(input.request_id)
    if not req:
        return json.dumps({"error": f"Unknown request_id '{input.request_id}'"})

    if input.force_status:
        status = input.force_status
    else:
        hospital = HOSPITALS[req["hospital_id"]]
        accept_prob = {"available": 0.85, "busy": 0.45, "full": 0.0}[hospital["capacity_status"]]
        status = "accepted" if random.random() < accept_prob else "declined"

    store.update_request_status(input.request_id, status)
    return json.dumps({"request_id": req["request_id"], "status": status,
                        "hospital_name": req["hospital_name"]})


class StanddownInput(BaseModel):
    case_id: str = Field(..., description="Patient case identifier")
    winning_hospital_id: str = Field(..., description="hospital_id of the confirmed/selected hospital")


@mcp.tool(
    name="broadcast_standdown",
    description=(
        "Notify all other hospitals that were sent a request for this case_id that the patient "
        "has been assigned elsewhere, so they can release the reserved bay. Call this immediately "
        "after selecting a winning hospital (FR-3.4)."
    ),
)
def broadcast_standdown(input: StanddownInput) -> str:
    notified = []
    for req in store.get_requests_by_case(input.case_id):
        if req["hospital_id"] != input.winning_hospital_id and req["status"] != "stood_down":
            store.update_request_status(req["request_id"], "stood_down")
            notified.append(req["hospital_name"])
    return json.dumps({"case_id": input.case_id, "stood_down": notified,
                        "winner": HOSPITALS[input.winning_hospital_id]["name"]})


@mcp.tool(
    name="list_hospitals",
    description="Return the full hospital directory with specialties and live capacity_status. Read-only.",
)
def list_hospitals() -> str:
    return json.dumps(list(HOSPITALS.values()), indent=2)


class UpdateCapacityInput(BaseModel):
    hospital_id: str = Field(..., description="The ID of the hospital (e.g. H001)")
    capacity_status: str = Field(..., description="The new capacity status: available, busy, or full")


@mcp.tool(
    name="update_capacity_status",
    description="Update the capacity status of a hospital. Valid statuses: available, busy, full.",
)
def update_capacity_status(input: UpdateCapacityInput) -> str:
    if input.hospital_id not in HOSPITALS:
        return json.dumps({"error": f"Unknown hospital_id '{input.hospital_id}'"})
    if input.capacity_status not in ("available", "busy", "full"):
        return json.dumps({"error": f"Invalid capacity_status '{input.capacity_status}'. Must be one of: available, busy, full."})
    
    update_hospital_capacity(input.hospital_id, input.capacity_status)
    return json.dumps({"status": "ok", "hospital_id": input.hospital_id, "capacity_status": input.capacity_status})


def update_hospital_capacity(hospital_id: str, capacity_status: str):
    if hospital_id in HOSPITALS:
        HOSPITALS[hospital_id]["capacity_status"] = capacity_status
        try:
            with open(_HOSPITAL_DATA_PATH, "w") as f:
                json.dump(list(HOSPITALS.values()), f, indent=2)
        except Exception as e:
            print(f"Error persisting capacity update: {e}")


if __name__ == "__main__":
    mcp.run(transport="stdio")

