"""
run_demo.py
-----------
Wires together everything built so far into the actual end-to-end flow you'll
show judges: generated vitals -> ESI triage -> parallel MCP dispatch broadcast
-> first-accept wins -> stand-down to the rest.

This calls the MCP tool functions directly (in-process) rather than over
stdio/HTTP transport, so it's fast to iterate on. Swapping to a real AI agent
driving these same tools over MCP transport is a transport-layer change only —
the tool contracts don't change.

Run: python run_demo.py [scenario_name]
"""

import json
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "app", "triage_engine"))
sys.path.insert(0, os.path.dirname(__file__))

from vitals_generator import generate_case
from esi_rules import score_case
import mcp_hospital_server as srv


def haversine_eta_minutes(dist_km, avg_speed_kmh=35):
    return round((dist_km / avg_speed_kmh) * 60, 1)


def run_case(scenario_name=None, radius_km=8.0):
    print("=" * 70)
    case = generate_case(scenario_name)
    print(f"CASE GENERATED: {case['case_id']}  |  scenario: {case['scenario']}  |  area: {case['location']['area']}")
    print(f"Vitals: {case['vitals']}")
    print(f"Symptoms: {case['symptom_text']}")

    # 1. Triage scoring (deterministic ESI engine; symptom_flags stand in for LLM output)
    triage = score_case(case["vitals"], case["symptom_flags"])
    print(f"\nTRIAGE: ESI-{triage['esi_level']} ({triage['label']}) | specialty needed: {triage['specialty']}")
    for r in triage["rationale"]:
        print(f"   - {r}")
    print(f"   [EMT confirmation required before dispatch: {triage['requires_emt_confirmation']}]")

    # 2. Find matching hospitals via MCP tool
    query = srv.NearbyQuery(lat=case["location"]["lat"], lng=case["location"]["lng"],
                             specialty=triage["specialty"], radius_km=radius_km)
    result = json.loads(srv.find_matching_hospitals(query))

    if not result["candidates"]:
        print(f"\nNo hospitals found for specialty '{triage['specialty']}' within {radius_km}km.")
        print(f"   Auto-widening radius per FR-3.5 -> retrying at {radius_km * 2}km")
        query.radius_km = radius_km * 2
        result = json.loads(srv.find_matching_hospitals(query))
        if not result["candidates"]:
            print("   Still no match. Escalate to general-purpose nearest hospital.")
            return

    print(f"\nMATCHED HOSPITALS ({len(result['candidates'])}) within {query.radius_km}km:")
    for c in result["candidates"]:
        print(f"   - {c['name']} | {c['distance_km']}km | status: {c['capacity_status']}")

    # 3. Parallel broadcast (FR-3.3) — send to ALL candidates, not sequentially
    print(f"\nBROADCASTING to all {len(result['candidates'])} candidates in parallel...")
    sent_requests = []
    for c in result["candidates"]:
        dispatch_input = srv.DispatchInput(
            case_id=case["case_id"], hospital_id=c["hospital_id"],
            esi_level=triage["esi_level"], specialty=triage["specialty"],
            vitals=case["vitals"], symptom_text=case["symptom_text"],
            eta_minutes=haversine_eta_minutes(c["distance_km"]),
        )
        resp = json.loads(srv.send_dispatch_request(dispatch_input))
        sent_requests.append(resp["request_id"])
        print(f"   -> sent to {resp['sent_to']}  (request_id={resp['request_id']})")

    # 4. Collect responses (simulated hospital-side confirmation, FR-4.3 stand-in)
    print("\nCOLLECTING RESPONSES...")
    winner = None
    for rid in sent_requests:
        resp = json.loads(srv.confirm_hospital_response(srv.SimulateResponseInput(request_id=rid)))
        print(f"   {resp['hospital_name']}: {resp['status']}")
        if resp["status"] == "accepted" and winner is None:
            winner = resp

    # 5. Select winner + stand down the rest (FR-3.4, FR-3.6)
    if winner:
        winning_hospital_id = next(c["hospital_id"] for c in result["candidates"] if c["name"] == winner["hospital_name"])
        standdown = json.loads(srv.broadcast_standdown(
            srv.StanddownInput(case_id=case["case_id"], winning_hospital_id=winning_hospital_id)
        ))
        print(f"\nWINNER: {standdown['winner']}")
        print(f"STAND-DOWN sent to: {standdown['stood_down'] or 'none (only one candidate responded)'}")
    else:
        print("\nNo hospital accepted. Per FR-3.5, this triggers auto radius-widen and re-broadcast.")

    print("=" * 70)


if __name__ == "__main__":
    scenario = sys.argv[1] if len(sys.argv) > 1 else None
    run_case(scenario)
