"""
whisper_service.py
-------------------
Transcribes ambulance-recorded audio (symptom description only, never
numeric vitals — FR-1.3) using Whisper (whisper-large-v3) served via Groq's
inference API, chosen specifically for noise tolerance over the browser's
built-in speech recognition, which degrades badly with siren/road/traffic
noise in a moving ambulance.

Why Groq specifically (not OpenAI's Whisper endpoint): Groq serves the same
open-source Whisper model family, with a free tier and no card required to
get an API key, and noticeably lower inference latency — all of which matter
more for a hackathon build than for a funded production system. The SDK call
shape is OpenAI-compatible, so swapping providers later is a one-line change.

Requires GROQ_API_KEY in the environment. Degrades gracefully (returns an
error message, not a crash) if unset or the call fails — voice capture is a
convenience layer, it must never block an EMT from typing symptoms manually.
"""

import os
from openai import OpenAI

_client = None
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_MODEL = "whisper-large-v3-turbo"  # swap to "whisper-large-v3" for max accuracy over speed


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            return None
        _client = OpenAI(api_key=api_key, base_url=_GROQ_BASE_URL)
    return _client


def transcribe_audio(audio_bytes: bytes, filename: str = "symptom.webm") -> dict:
    """Returns {"success": bool, "text": str, "error": str|None}."""
    client = _get_client()
    if client is None:
        return {"success": False, "text": "", "error": "GROQ_API_KEY not configured — type symptoms manually."}

    try:
        import io
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = filename  # SDK needs a name attribute to infer format
        transcript = client.audio.transcriptions.create(
            model=_MODEL,
            file=audio_file,
            language="en",  # drop this if you want auto-detection for regional languages
        )
        return {"success": True, "text": transcript.text, "error": None}
    except Exception as e:
        print(f"[whisper_service] transcription failed, degrading: {e}")
        return {"success": False, "text": "", "error": "Voice transcription failed — type symptoms manually."}


if __name__ == "__main__":
    if not os.environ.get("GROQ_API_KEY"):
        print("GROQ_API_KEY not set — this will show graceful-degradation behavior.")
    result = transcribe_audio(b"fake-audio-bytes-for-local-test")
    print(result)
