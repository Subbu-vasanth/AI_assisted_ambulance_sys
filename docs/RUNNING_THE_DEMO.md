# Running the live demo (backend + dashboard)

## 1. Start the backend
```bash
cd backend
pip install -r requirements.txt --break-system-packages   # or inside your venv
cd app
uvicorn main:app --reload --port 8000
```
Confirm it's up: open http://localhost:8000 — should return `{"status":"ok",...}`.

## 2. Open the frontends — through the backend, NOT by double-clicking the file
Both frontends are now served **by the backend itself** (same origin as the API),
specifically to avoid `file://` WebSocket connection issues that some browsers
(especially Safari) block or silently drop:

- Hospital dashboard: **http://localhost:8000/dashboard/**
- Ambulance app: **http://localhost:8000/ambulance/**

Do NOT open `hospital-dashboard/index.html` or `ambulance-app/index.html`
directly in the browser (no `file://...` URLs) — that's what caused dispatched
cases to silently not appear and accepted requests to hang on "confirming best
match" forever. Always go through the `localhost:8000` URLs above.

## 3. Trigger a case
Two ways:
- Click any of the scenario buttons at the top of the dashboard itself
  ("Chest Pain", "Cardiac Arrest", etc.) — fastest for a live demo.
- Or from a terminal: `curl -X POST http://localhost:8000/api/simulate/chest_pain`

## 4. What to expect
- A card appears instantly, color-coded by ESI level (red = ESI-1/2, pulsing
  left edge for anything time-critical).
- Click **Confirm Readiness** to accept.
- Open a second browser tab on the dashboard, switch the hospital dropdown to
  another specialty-matched hospital, and fire a case that matches both — you'll
  see both tabs get the request live, and the moment one accepts, the other
  flips to "Assigned to [hospital]" automatically. **This is your stand-down
  demo** — the clearest way to show judges the parallel-broadcast behavior
  actually working, not just described.

## Notes
- Hospital capacity (`available` / `busy` / `full`) comes from
  `shared/hospital_seed_data.json` — edit it live to change matching behavior
  for the demo (e.g. set a closer hospital to `full` to show radius auto-widen).
- All dispatch state is in-memory (`mcp_hospital_server.DISPATCH_REQUESTS`) —
  restarting the backend clears it. Fine for a demo; swap for Postgres/Redis
  per the SRS before any real deployment.
