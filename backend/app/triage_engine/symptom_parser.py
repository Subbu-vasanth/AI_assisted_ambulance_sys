"""
symptom_parser.py
------------------
Converts free-text / voice-transcribed symptom description into the same
structured `symptom_flags` dict that esi_rules.score_case() expects
(FR-2.2). This is the "AI-assisted" half of triage — but per FR-2.3, its
output only ever feeds INTO the deterministic ESI engine as an input flag.
It never computes or overrides the urgency number itself. That split is
what keeps the liability story clean (see SRS §5.3).

Manually-tapped flag chips in the ambulance app always take precedence over
LLM-inferred ones for the same key — if the EMT explicitly marked something,
that's ground truth; the LLM only fills in gaps the EMT didn't tap.

Requires ANTHROPIC_API_KEY in the environment. If unset, falls back to
returning an empty dict (manual-flags-only mode) rather than crashing the
whole triage flow — an outage here should degrade, not block dispatch.
"""

import json
import os

import anthropic

_HERE = os.path.dirname(__file__)
_REF_PATH = os.path.join(_HERE, "..", "..", "..", "shared", "esi_reference_table.json")
with open(_REF_PATH) as f:
    _REF = json.load(f)

VALID_FLAGS = list(_REF["symptom_flags"].keys())

_SYSTEM_PROMPT = f"""You are a clinical symptom classifier for an ambulance triage system.
Given a free-text or voice-transcribed description from an EMT, identify which of the
following structured flags apply. Only mark a flag True if the description clearly
supports it — when uncertain, leave it False. Never invent symptoms not implied by the text.

Valid flags: {json.dumps(VALID_FLAGS)}

Respond with ONLY a JSON object mapping flag names to true/false, no other text,
no markdown fences, no preamble. Include every flag from the list above."""

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def parse_symptoms(symptom_text: str) -> dict:
    """Returns {flag_name: bool, ...} for every flag in VALID_FLAGS.
    Returns all-False dict if no API key is configured or the call fails —
    degrades gracefully rather than blocking dispatch (never a hard dependency)."""
    empty_result = {flag: False for flag in VALID_FLAGS}

    if not symptom_text or not symptom_text.strip():
        return empty_result

    client = _get_client()
    if client is None:
        return empty_result

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": symptom_text}],
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        # Only accept known flags; ignore anything the model hallucinates outside the schema
        return {flag: bool(parsed.get(flag, False)) for flag in VALID_FLAGS}
    except Exception as e:
        print(f"[symptom_parser] LLM call failed, degrading to empty flags: {e}")
        return empty_result


def merge_flags(manual_flags: dict, llm_flags: dict) -> dict:
    """Manual (EMT-tapped) flags always win over LLM-inferred ones. LLM fills gaps only."""
    merged = dict(llm_flags)
    merged.update({k: v for k, v in manual_flags.items() if v})
    return merged


if __name__ == "__main__":
    test_texts = [
        "Patient clutching chest, sweating heavily, says pain radiating to left arm.",
        "Sudden facial drooping on right side, slurred speech, left arm weakness.",
        "Fall from ladder, patient conscious, right wrist swollen and deformed, pain 7/10.",
        "Patient reports mild dizziness, vitals stable, requesting precautionary checkup.",
    ]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — showing graceful-degradation behavior only.")
    for t in test_texts:
        result = parse_symptoms(t)
        active = [k for k, v in result.items() if v]
        print(f"\nText: {t}")
        print(f"Flags: {active if active else '(none — degraded/no key)'}")
