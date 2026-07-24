"""
vitals_generator.py
--------------------
Simulates what the ambulance app would normally get from BLE-paired vitals
hardware + LLM-parsed voice symptoms (FR-1.1 / FR-1.3 in the SRS). Standing in
for real hardware during the hackathon demo — swap `generate_case()` for the
real BLE client / Whisper pipeline later without touching anything downstream.

Each generated case is intentionally built around a clinical scenario so the
ESI engine's output is easy to sanity-check against what a human would expect.
"""

import random
import uuid
from datetime import datetime, timezone

SCENARIOS = {
    "cardiac_arrest": {
        "vitals_range": {
            "heart_rate_bpm": (150, 175),
            "spo2_percent": (78, 87),
            "bp_systolic_mmhg": (55, 75),
            "temperature_celsius": (36.5, 37.5),
        },
        "flags": {"cardiac_arrest_flag": True},
        "symptom_text": "Patient unresponsive, no pulse detected, EMT starting CPR.",
    },
    "chest_pain": {
        "vitals_range": {
            "heart_rate_bpm": (105, 125),
            "spo2_percent": (89, 93),
            "bp_systolic_mmhg": (82, 92),
            "temperature_celsius": (36.8, 37.4),
        },
        "flags": {"chest_pain_flag": True},
        "symptom_text": "Patient clutching chest, sweating heavily, says pain radiating to left arm.",
    },
    "stroke": {
        "vitals_range": {
            "heart_rate_bpm": (88, 105),
            "spo2_percent": (93, 96),
            "bp_systolic_mmhg": (165, 195),
            "temperature_celsius": (36.7, 37.2),
        },
        "flags": {"stroke_symptoms_flag": True},
        "symptom_text": "Sudden facial drooping on right side, slurred speech, left arm weakness.",
    },
    "road_trauma": {
        "vitals_range": {
            "heart_rate_bpm": (115, 140),
            "spo2_percent": (90, 94),
            "bp_systolic_mmhg": (85, 100),
            "temperature_celsius": (36.4, 37.0),
        },
        "flags": {"major_trauma_flag": True, "moderate_bleeding_flag": True},
        "symptom_text": "Motorbike accident, visible leg deformity, bleeding from scalp laceration.",
    },
    "fracture_stable": {
        "vitals_range": {
            "heart_rate_bpm": (85, 100),
            "spo2_percent": (96, 99),
            "bp_systolic_mmhg": (110, 125),
            "temperature_celsius": (36.6, 37.1),
        },
        "flags": {"fracture_flag": True},
        "symptom_text": "Fall from ladder, patient conscious, right wrist swollen and deformed, pain 7/10.",
    },
    "fever_general": {
        "vitals_range": {
            "heart_rate_bpm": (90, 105),
            "spo2_percent": (95, 98),
            "bp_systolic_mmhg": (105, 120),
            "temperature_celsius": (38.6, 39.6),
        },
        "flags": {"fever_infection_flag": True},
        "symptom_text": "High fever since morning, chills, mild dehydration, alert and oriented.",
    },
    "normal_checkup": {
        "vitals_range": {
            "heart_rate_bpm": (65, 85),
            "spo2_percent": (97, 100),
            "bp_systolic_mmhg": (108, 125),
            "temperature_celsius": (36.5, 37.2),
        },
        "flags": {},
        "symptom_text": "Patient reports mild dizziness, vitals stable, requesting precautionary checkup.",
    },
}

# Approximate live-ambulance coordinates around Coimbatore, for radius/ETA math
AMBULANCE_START_POINTS = [
    {"lat": 11.0050, "lng": 76.9610, "area": "RS Puram"},
    {"lat": 10.9950, "lng": 76.9500, "area": "Gandhipuram"},
    {"lat": 11.0250, "lng": 76.9850, "area": "Peelamedu"},
    {"lat": 10.9850, "lng": 76.9700, "area": "Singanallur"},
]


def _rand_in_range(bounds):
    lo, hi = bounds
    return round(random.uniform(lo, hi), 1)


def generate_case(scenario_name: str = None) -> dict:
    """Generate one synthetic ambulance case. Pass a scenario name for a
    deterministic clinical picture, or omit for a random one."""
    scenario_name = scenario_name or random.choice(list(SCENARIOS.keys()))
    scenario = SCENARIOS[scenario_name]
    start = random.choice(AMBULANCE_START_POINTS)

    vitals = {k: _rand_in_range(v) for k, v in scenario["vitals_range"].items()}

    return {
        "case_id": f"CASE-{uuid.uuid4().hex[:8].upper()}",
        "ambulance_id": f"AMB-{random.randint(100, 999)}",
        "scenario": scenario_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "location": start,
        "vitals": vitals,
        "capture_method": "simulated_ble",
        "symptom_text": scenario["symptom_text"],
        "symptom_flags": scenario["flags"],  # normally produced by LLM parser from symptom_text
    }


def generate_batch(n: int = 5) -> list:
    names = list(SCENARIOS.keys())
    random.shuffle(names)
    return [generate_case(names[i % len(names)]) for i in range(n)]


if __name__ == "__main__":
    import json
    for case in generate_batch(len(SCENARIOS)):
        print(json.dumps(case, indent=2))
        print("-" * 60)
