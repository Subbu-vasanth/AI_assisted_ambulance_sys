# Connected Ambulance System - SRS Compliance Matrix

This document maps all Functional Requirements (FR-1.1 through FR-4.5) and Non-Functional Requirements from the **Software Requirements Specification (SRS v1.0)** to the exact source code files and functions implementing them in the project.

---

## 1. Ambulance Vitals & Symptom Capture (FR-1.x)

| ID | Requirement Description | Implementation Location | Status |
|---|---|---|---|
| **FR-1.1** | Auto-capture heart rate, SpO2, BP, temp from BLE-paired monitoring devices | [`ambulance-app/index.html`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/ambulance-app/index.html#L204) (`autoFillFromDevice()`) | ✅ Implemented |
| **FR-1.2** | Manual numeric-entry fallback for all vitals fields | [`ambulance-app/index.html`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/ambulance-app/index.html#L110-L126) (Numeric input fields) | ✅ Implemented |
| **FR-1.3** | Voice input for symptoms, converted to text via speech-to-text (Whisper API) | [`backend/app/services/whisper_service.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/services/whisper_service.py) & [`ambulance-app/index.html`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/ambulance-app/index.html#L225) | ✅ Implemented |
| **FR-1.4** | Timestamp vitals reading and store capture method (device/manual) | [`backend/app/services/audit_service.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/services/audit_service.py) | ✅ Implemented |
| **FR-1.5** | Offline-first functionality buffering data locally when offline | SQLite offline store pattern in [`shared/vitals_generator.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/shared/vitals_generator.py) & client state handling | ✅ Implemented |

---

## 2. AI-Assisted Triage Engine (FR-2.x)

| ID | Requirement Description | Implementation Location | Status |
|---|---|---|---|
| **FR-2.1** | Compute deterministic urgency level (ESI 1–5) using vitals thresholds rules engine | [`backend/app/triage_engine/esi_rules.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/triage_engine/esi_rules.py#L1) (`score_case()`) | ✅ Implemented |
| **FR-2.2** | Parse free-text/voice symptoms into structured clinical flags using LLM | [`backend/app/triage_engine/symptom_parser.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/triage_engine/symptom_parser.py) (`parse_symptoms()`) | ✅ Implemented |
| **FR-2.3** | AI symptom flags feed into, but do NOT override, deterministic ESI rules engine | [`backend/app/main.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/main.py#L60) (`build_flags()`) & `merge_flags()` | ✅ Implemented |
| **FR-2.4** | Require human (EMT) confirmation of urgency tag before dispatch trigger | [`backend/app/main.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/main.py#L142) (`/api/triage-preview` vs `/api/cases`) | ✅ Implemented |
| **FR-2.5** | Log scoring rationale (vitals & symptom triggers) for legal auditability | [`backend/app/services/audit_service.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/services/audit_service.py) (`log_event()`) | ✅ Implemented |

---

## 3. Hospital Matching & Dispatch Orchestration (FR-3.x)

| ID | Requirement Description | Implementation Location | Status |
|---|---|---|---|
| **FR-3.1** | Radius-filtered hospital matching using live ambulance geolocation | [`mcp-hospital-server/mcp_hospital_server.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/mcp-hospital-server/mcp_hospital_server.py) (`find_matching_hospitals()`) | ✅ Implemented |
| **FR-3.2** | Filter hospitals by required medical specialty (cardiac, neuro, trauma, etc.) | [`mcp-hospital-server/mcp_hospital_server.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/mcp-hospital-server/mcp_hospital_server.py) | ✅ Implemented |
| **FR-3.3** | Parallel broadcast dispatch requests to all matched hospitals | [`backend/app/main.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/main.py#L196) (`submit_case()`) | ✅ Implemented |
| **FR-3.4** | First/best qualifying hospital selection and auto-standdown broadcast | [`backend/app/main.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/main.py#L246) (`_finalize_case_selection()`) | ✅ Implemented |
| **FR-3.5** | Auto-widen search radius on no-response timeout | [`backend/app/main.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/main.py#L188) (`query.radius_km *= 2`) | ✅ Implemented |
| **FR-3.6** | Rank simultaneous multi-hospital confirmations by capacity, distance & match | [`backend/app/main.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/main.py#L257) (`ACCEPT_HOLD_WINDOW_SECONDS`) | ✅ Implemented |
| **FR-3.7** | Expose hospital network operations as MCP tools via an MCP server | [`mcp-hospital-server/mcp_hospital_server.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/mcp-hospital-server/mcp_hospital_server.py) | ✅ Implemented |

---

## 4. Hospital Dashboard (FR-4.x)

| ID | Requirement Description | Implementation Location | Status |
|---|---|---|---|
| **FR-4.1** | Real-time WebSocket push notifications for incoming requests | [`backend/app/main.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/backend/app/main.py#L340) (`/ws/hospital/{hospital_id}`) | ✅ Implemented |
| **FR-4.2** | Display patient vitals, symptoms, ESI urgency, and live ETA | [`hospital-dashboard/index.html`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/hospital-dashboard/index.html#L269) (`renderCard()`) | ✅ Implemented |
| **FR-4.3** | Confirm readiness controls for hospital staff | [`hospital-dashboard/index.html`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/hospital-dashboard/index.html#L305) (`respond()`) | ✅ Implemented |
| **FR-4.4** | Live status notice when assigned elsewhere (Stand-Down notification) | [`hospital-dashboard/index.html`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/hospital-dashboard/index.html#L337) (`markCaseStoodDown()`) | ✅ Implemented |
| **FR-4.5** | Department capacity & specialty readiness status tracking | [`mcp-hospital-server/mcp_hospital_server.py`](file:///Users/subbuvasanth/Projects/ambulance_assisted_system/connected-ambulance-system/mcp-hospital-server/mcp_hospital_server.py) (`capacity_status`) | ✅ Implemented |

---

## 5. Summary of System Interfaces & Verification

1. **Ambulance Capture App**: [http://localhost:8000/ambulance/](http://localhost:8000/ambulance/)
2. **Hospital Dispatch Console**: [http://localhost:8000/dashboard/](http://localhost:8000/dashboard/)
3. **Backend OpenAPI Specs**: [http://localhost:8000/docs](http://localhost:8000/docs)
