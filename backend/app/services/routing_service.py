"""
routing_service.py
-------------------
Traffic-aware route calculation using TomTom Routing API (Calculate Route +
Traffic sections). Returns polyline coordinates annotated with per-segment
congestion level so frontends can color-code the route line.

Graceful degradation: if TOMTOM_API_KEY is missing or the API call fails,
falls back to Haversine distance and a straight-line polyline with
traffic="unknown" — same pattern as whisper_service.py / symptom_parser.py.
Never crashes or blocks dispatch.

Requires TOMTOM_API_KEY in the environment.
"""

import math
import os

import httpx

_TOMTOM_BASE = "https://api.tomtom.com/routing/1/calculateRoute"


def _get_api_key():
    return os.environ.get("TOMTOM_API_KEY")


def _haversine_km(lat1, lng1, lat2, lng2):
    """Haversine distance in km — same formula as mcp_hospital_server.py."""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _haversine_fallback(origin_lat, origin_lng, dest_lat, dest_lng):
    """Fallback route: straight line, Haversine distance, avg 35 km/h ETA."""
    dist = round(_haversine_km(origin_lat, origin_lng, dest_lat, dest_lng), 2)
    eta = round((dist / 35) * 60, 1)  # minutes at 35 km/h average
    return {
        "distance_km": dist,
        "eta_minutes": eta,
        "polyline": [
            {"lat": origin_lat, "lng": origin_lng, "traffic": "unknown"},
            {"lat": dest_lat, "lng": dest_lng, "traffic": "unknown"},
        ],
        "source": "haversine",
    }


def _classify_traffic(section):
    """Map TomTom traffic section simpleCategory to a UI-friendly level."""
    cat = section.get("simpleCategory", "").upper()
    if cat in ("JAM", "STANDSTILL"):
        return "blocked"
    if cat in ("SLOW",):
        return "heavy"
    if cat in ("MODERATE",):
        return "moderate"
    # FREE_FLOW, UNKNOWN, or anything else
    return "free"


def _annotate_polyline_with_traffic(points, sections):
    """Take the raw TomTom points list and traffic sections, return annotated polyline.

    Each point gets a 'traffic' field based on which traffic section (if any)
    covers its index range. Points not covered by any traffic section default
    to 'free'.
    """
    # Build an index→traffic lookup from sections
    traffic_at_index = {}
    for sec in sections:
        if sec.get("sectionType") != "TRAFFIC":
            continue
        level = _classify_traffic(sec)
        start_idx = sec.get("startPointIndex", 0)
        end_idx = sec.get("endPointIndex", start_idx)
        for i in range(start_idx, end_idx + 1):
            traffic_at_index[i] = level

    annotated = []
    for i, pt in enumerate(points):
        annotated.append({
            "lat": pt["latitude"],
            "lng": pt["longitude"],
            "traffic": traffic_at_index.get(i, "free"),
        })
    return annotated


def get_route(origin_lat, origin_lng, dest_lat, dest_lng):
    """Calculate route with traffic data.

    Returns:
        {
            "distance_km": float,
            "eta_minutes": float,
            "polyline": [{"lat", "lng", "traffic"}, ...],
            "source": "tomtom" | "haversine"
        }
    """
    api_key = _get_api_key()
    if not api_key:
        return _haversine_fallback(origin_lat, origin_lng, dest_lat, dest_lng)

    try:
        coords = f"{origin_lat},{origin_lng}:{dest_lat},{dest_lng}"
        url = f"{_TOMTOM_BASE}/{coords}/json"
        params = {
            "key": api_key,
            "traffic": "true",
            "sectionType": "traffic",
            "travelMode": "car",
        }

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        route = data["routes"][0]
        summary = route["summary"]
        distance_km = round(summary["lengthInMeters"] / 1000, 2)
        eta_minutes = round(summary["travelTimeInSeconds"] / 60, 1)

        # Extract points from the first leg
        raw_points = route["legs"][0]["points"]
        sections = route.get("sections", [])

        polyline = _annotate_polyline_with_traffic(raw_points, sections)

        return {
            "distance_km": distance_km,
            "eta_minutes": eta_minutes,
            "polyline": polyline,
            "source": "tomtom",
        }

    except Exception as e:
        print(f"[routing_service] TomTom API call failed, degrading to Haversine: {e}")
        return _haversine_fallback(origin_lat, origin_lng, dest_lat, dest_lng)


if __name__ == "__main__":
    # Self-test: route from RS Puram to Coimbatore General Hospital
    if not os.environ.get("TOMTOM_API_KEY"):
        print("TOMTOM_API_KEY not set — showing Haversine fallback behavior.")
    result = get_route(11.0050, 76.9610, 11.0018, 76.9629)
    print(f"Source: {result['source']}")
    print(f"Distance: {result['distance_km']} km")
    print(f"ETA: {result['eta_minutes']} min")
    print(f"Polyline points: {len(result['polyline'])}")
    if result['polyline']:
        print(f"First point: {result['polyline'][0]}")
        print(f"Last point: {result['polyline'][-1]}")
