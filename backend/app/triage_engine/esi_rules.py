"""
esi_rules.py
------------
Deterministic, explainable triage scoring engine (FR-2.1, FR-2.3, FR-2.5 in the SRS).

Design principle: this is the SOLE authority on urgency level. The LLM symptom
parser (symptom_parser.py) only produces `symptom_flags` — it never touches the
final ESI number directly. That keeps the liability story clean: every score
this function returns has a fully traceable, human-readable rationale.

Usage:
    from esi_rules import score_case
    result = score_case(vitals, symptom_flags)
    # result = {
    #   "esi_level": int (1-5, 1 = most critical),
    #   "label": str,
    #   "specialty": str,
    #   "rationale": list[str],
    #   "requires_emt_confirmation": True
    # }
"""

import json
import os

_REF_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared", "esi_reference_table.json")

with open(_REF_PATH) as f:
    REF = json.load(f)

VITALS = REF["vitals_thresholds"]
FLAGS = REF["symptom_flags"]
LEVELS = REF["esi_levels"]
DEFAULT_SPECIALTY = REF["specialty_default"]


def _check_vital(name, value, rationale):
    """Returns the worst (lowest) ESI level implied by a single vital, or None if normal."""
    if value is None:
        return None
    t = VITALS.get(name)
    if not t:
        return None

    worst = None

    for esi_key, esi_num in [("esi1_critical", 1), ("esi2_high_risk", 2), ("esi3_abnormal", 3)]:
        bounds = t.get(esi_key)
        if not bounds:
            continue
        below = bounds.get("below")
        above = bounds.get("above")
        if (below is not None and value < below) or (above is not None and value > above):
            rationale.append(f"{name}={value} falls in ESI-{esi_num} range ({esi_key})")
            worst = esi_num
            break  # most severe match for this vital wins, stop checking looser bands

    return worst


def score_case(vitals: dict, symptom_flags: dict = None) -> dict:
    """
    vitals: {
        "heart_rate_bpm": float, "spo2_percent": float,
        "bp_systolic_mmhg": float, "temperature_celsius": float
    }
    symptom_flags: dict of bool, e.g. {"chest_pain_flag": True, "stroke_symptoms_flag": False}
        (produced by the LLM symptom parser from voice/text input)
    """
    symptom_flags = symptom_flags or {}
    rationale = []
    candidate_levels = []  # (esi_level, specialty_or_None)

    # 1. Check vitals against thresholds
    for vital_name, value in vitals.items():
        level = _check_vital(vital_name, value, rationale)
        if level:
            candidate_levels.append((level, None))

    # 2. Check symptom flags (these can force ESI-1 or set a minimum floor)
    triggered_specialty = None
    for flag_name, is_present in symptom_flags.items():
        if not is_present:
            continue
        rule = FLAGS.get(flag_name)
        if not rule:
            continue
        if "forces_esi" in rule:
            candidate_levels.append((rule["forces_esi"], rule.get("specialty")))
            rationale.append(f"{flag_name} present -> forces ESI-{rule['forces_esi']}")
        elif "min_esi" in rule:
            candidate_levels.append((rule["min_esi"], rule.get("specialty")))
            rationale.append(f"{flag_name} present -> minimum ESI-{rule['min_esi']}")
        if rule.get("specialty") and triggered_specialty is None:
            triggered_specialty = rule["specialty"]

    # 3. Final score = most severe (lowest number) of all candidates; default ESI-5 if nothing abnormal
    if candidate_levels:
        esi_level = min(lvl for lvl, _ in candidate_levels)
    else:
        esi_level = 5
        rationale.append("All vitals within normal range, no symptom flags present -> ESI-5")

    specialty = triggered_specialty or DEFAULT_SPECIALTY

    return {
        "esi_level": esi_level,
        "label": LEVELS[str(esi_level)]["label"],
        "meaning": LEVELS[str(esi_level)]["meaning"],
        "target_hospital_response_seconds": LEVELS[str(esi_level)]["target_hospital_response_seconds"],
        "specialty": specialty,
        "rationale": rationale,
        "requires_emt_confirmation": True,  # FR-2.4 — never auto-dispatch without this
    }


if __name__ == "__main__":
    # Quick self-test with a few representative cases
    test_cases = [
        {
            "name": "Cardiac arrest - forced ESI-1",
            "vitals": {"heart_rate_bpm": 160, "spo2_percent": 82, "bp_systolic_mmhg": 65, "temperature_celsius": 37.0},
            "flags": {"cardiac_arrest_flag": True},
        },
        {
            "name": "Chest pain, vitals borderline - ESI-2",
            "vitals": {"heart_rate_bpm": 118, "spo2_percent": 91, "bp_systolic_mmhg": 88, "temperature_celsius": 37.2},
            "flags": {"chest_pain_flag": True},
        },
        {
            "name": "Fracture, stable vitals - ESI-3",
            "vitals": {"heart_rate_bpm": 95, "spo2_percent": 97, "bp_systolic_mmhg": 118, "temperature_celsius": 37.0},
            "flags": {"fracture_flag": True},
        },
        {
            "name": "Normal vitals, mild pain - ESI-4",
            "vitals": {"heart_rate_bpm": 78, "spo2_percent": 98, "bp_systolic_mmhg": 120, "temperature_celsius": 36.8},
            "flags": {"mild_pain_flag": True},
        },
        {
            "name": "All normal, no symptoms - ESI-5",
            "vitals": {"heart_rate_bpm": 72, "spo2_percent": 99, "bp_systolic_mmhg": 115, "temperature_celsius": 36.9},
            "flags": {},
        },
    ]

    for case in test_cases:
        result = score_case(case["vitals"], case["flags"])
        print(f"\n=== {case['name']} ===")
        print(f"ESI-{result['esi_level']} ({result['label']}) | specialty: {result['specialty']}")
        for r in result["rationale"]:
            print(f"  - {r}")
