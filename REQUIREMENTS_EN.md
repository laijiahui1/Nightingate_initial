# Nightingale 72-Hour Build — Requirements Breakdown

> Source: [2026 72 Hour Build_ Nightingale Candidate Brief 2.md](./2026%2072%20Hour%20Build_%20Nightingale%20Candidate%20Brief%202.md)
> Updated: 2026-08-26
> Chinese version: [REQUIREMENTS.md](./REQUIREMENTS.md)

---

## 1. One-Line Positioning

Build a **single, shared, longitudinal Care Note clinic web application**: centered on a "timeline + collaboration + AI scribe + traceable provenance" model that lets clinical staff understand a patient's full picture in under 10 seconds, while integrating patient-contributed insights, AI-generated summaries, and multi-role notes into a single **trustworthy, traceable, actionable** record.

The scoring weights determine the priority order: **Glanceability(6) > Collaboration+AI(5) > Provenance(4) > Security(3) > Communication(2) > Bonus(10)**

---

## 2. Requirement Tiers

| Tier | Content | Nature |
|---|---|---|
| **P0 Hard Constraints** | RBAC enforced server-side, Glance P95 ≤ 300ms (warm), PHI redaction before LLM, synthetic data only, TLS + encryption at rest | Must-have |
| **P0 Core Features** | Care Note page / Glance Top Card / Longitudinal Timeline / Inline Collaboration / Revision History + Revert / AI Scribe Integration / RBAC / Provenance + Conflict Handling | Must-have |
| **P0 Deliverables** | Git repo + micro-tests + README + 2-3 page technical brief + ATTRIBUTION.txt + demo video (3 scenarios) | Must-have |
| **P1 Bonus** | Self-Learning Importance Logic, Hybrid Storage / Data Decay, Ambient Voice Capture (patient + clinical) | Bonus |

---

## 3. Module Breakdown (Modules to Build)

### M1. Data Model & Persistence Layer (Foundation for everything)

- **Core entities & relationships**: `Patient` ↔ `Entry/Note` ↔ `Comment/Thread` ↔ `Version` ↔ `Highlight` ↔ `Provenance` ↔ `AI_Scribed_Note`
- **Per-entry metadata**: `author_role` (patient/staff/clinician/system), `author_id`, `timestamp`, `type` (session/consult/instruction/admin), `provenance_pointer`
- **3 AI-scribed types**: `ai_doctor_consult_summary` / `ai_nurse_consult_summary` / `ai_patient_session_summary`, with `author_role=system`
- **Audit log** (**metadata only**, no content stored — meets test requirements)
- Suggestion: **PostgreSQL** (native RLS support, directly satisfies "server-side enforced RBAC"); version storage via snapshots or diffs (architectural choice)

### M2. Care Note Single Page (one unified page per patient)

- **Glance / Top Card**: readable + actionable within 10 seconds; surfaces open actions (e.g., "needs lab order," "waiting nurse follow-up") and risk flags
- **Longitudinal Timeline**: time-ordered continuous feed, mixing manual notes and AI-scribed notes
- **Inline Collaboration**: threaded comments + resolve/unresolve, @mentions, assignments ("Assign to staff")
- **Revision History**: full snapshots, "view changes since X", revert to any version
- **Real-time collaboration**: Google-Docs-style concurrent multi-role editing (WebSocket)

### M3. Glance Highlight / Smart Prioritization Engine

- Core logic: combine **recency + explicit risk_level + tagged clinical entities (medications/chief complaint/allergies) + unresolved tasks**
- Every highlight must show **risk_reason + provenance_pointer**, with fast accept/reject
- **Self-Learning component (bonus)**: learn from clinicians' manual highlights/edits/comments on AI-scribed notes → adapt future suggestion priority (weights must be persisted)

### M4. RBAC Permission System (server-side enforced)

- 4 roles: patient / staff / clinician / admin
- Rules: patient cannot view internal comments or raw AI-scribed notes; staff can only view+write staff_notes and cannot access other clinics' data; clinician can read/write clinician_sections, view staff notes + all AI-scribed notes, clinic-scoped; admin has clinic-scoped oversight
- Constraint: **Clinician cannot overwrite Staff notes; Staff cannot overwrite Clinician notes**
- Implementation: Postgres RLS + backend/middleware checks as defense-in-depth (UI-only checks do not count)

### M5. Provenance & Trust Module

- Clicking any highlight → navigates to the **source entry/span** in the timeline
- Conflict handling: clinician edits take precedence over AI/patient memory, **or** flag the conflict for review (demonstrate both paths)

### M6. Security & Privacy Pipeline

- **No PHI Redaction Pipeline**: names, IC/ID numbers, phones → redacted **before** sending to the LLM
- TLS in transit + encryption at rest
- **Clean logs**: no PHI leaked in logs
- Synthetic data only + synthetic data seed generator

### M7. Ambient Voice Capture (bonus, two separate views)

- **Patient side** (patient view only): PWA mobile recording → redact before LLM → transcribe → extract structured facts → generate patient consult session summary
- **Clinical side** (clinical view only): PWA mobile/laptop recording → speaker-labeled transcript, timestamps, confidence markers, code-switching support, clinical summary, provenance back to source segments
- Advanced bonus: noisy environments, diarization, overlap handling, multilingual medical terminology, multi-device capture

### M8. Micro-Test Suite (5 specified files)

- `test_rbac_scope.py` (roles cannot write/edit as each other; patient isolation)
- `test_revision_history.py` (version increments, revert restores prior state, audit log shows who changed what — metadata only)
- `test_highlight_provenance.py` (every highlight's provenance pointer resolves to an entry/span)
- `test_concurrent_edits.py` (different sections edited concurrently don't overwrite each other; deterministic resolution for same-section conflicts)
- `test_self_learning_importance.py` (bonus: simulate pinning a highlight → similar content gets increased priority)

### M9. Deliverables & Docs

- README (setup/run, **where redaction happens**, **how RBAC is enforced**)
- 2-3 page technical brief (architecture diagram + full data schema + assumptions/trade-offs)
- ATTRIBUTION.txt (all external libraries/models + licenses)
- Demo video covering 3 scenarios (A: Glance + AI Scribe traceability; B: Collaboration + Audit + Learning; C: Longitudinal history + highlight logic + data decay)

---

## 4. Potential / Implicit Requirements Checklist (Easy to Miss)

1. **Multi-clinic tenancy** — "access is clinic-scoped" appears repeatedly; schema needs `clinic_id`, RLS filters by clinic
2. **Implicit pre-aggregation for 10s glance** — Top Card must be precomputed/cached to hit both 300ms and "quick read" goals
3. **Deterministic concurrency conflict strategy** — explicitly required by tests; same-section conflicts need a defined rule (e.g., timestamp/version/field-level merge + conflict flag)
4. **Mentions/assignments → notification mechanism** — @nurse @clinician semantically implies notifications / task queue
5. **resolve/unresolve state machine** — comment lifecycle needs modeling
6. **Learning weight persistence** — self-learning mechanism must persist (learning_weights / interaction_log)
7. **"View changes since X"** — needs point-in-time diff capability, not just revert
8. **Clinical entity recognition** — highlight logic must identify medications/chief complaint/allergies (LLM or NER)
9. **LLM trust psychology** — brief repeatedly stresses "trust but need reassurance": UI must show human reviewability and visible provenance — this is an **implicit scoring item**
10. **Visual distinction between AI and manual notes** — `author_role=system` must be clearly distinguishable in the timeline
11. **Voice capture PHI redaction timing** — must happen before transcription→LLM, covering all data streams
12. **Multi-device / cross-end consistency** — data sync between PWA capture and web viewing
13. **Demo reproducibility** — need a **synthetic data seed script** so all 3 demo scenarios can be demonstrated with one command
14. **Log compliance** — "clean logs" means the redaction pipeline must also apply to log output
15. **Data decay strategy** (bonus) — schema + logic for compressing/archiving older data, coordinated with the timeline display
16. **Documented test run steps** — "how to run tests" is a delivery requirement

---

## 5. Suggested Tech Stack (inferred from constraints)

| Layer | Suggestion | Rationale |
|---|---|---|
| Backend | **Python / FastAPI** | All test filenames are `.py`, strongly implying Python tests; FastAPI enables fast iteration |
| Database | **PostgreSQL** | Native RLS directly implements "server-side enforced RBAC" |
| Frontend | **React / Next.js + Tailwind** | Fast collaborative UI development |
| Real-time | **WebSocket** | Google-Docs-style concurrency |
| Transcription | **Whisper** family | Speaker labels / multilingual / noisy environments |
| LLM | Any (OpenAI/Claude) | Summaries + highlight suggestions + entity extraction |

---

## 6. Suggested Build Order

1. Data model + Postgres schema + RLS (lay the foundation once)
2. Glance / Top Card (highest score at 6, prioritize)
3. Timeline + AI Scribe 3-note-type ingestion
4. Collaboration (comments/mentions/tasks) + revision history
5. RBAC + redaction pipeline + 5 test files (write tests first for stability)
6. Provenance navigation + conflict handling
7. Bonus: self-learning, data decay, voice capture

---

## 7. Scoring Map (Scoring Dimension → Module)

| Scoring Dimension | Points | Corresponding Modules |
|---|---|---|
| Glanceability & Actionability | 6 | M2(Glance) + M3 |
| Collaboration & AI Integration | 5 | M2(Collaboration/Version) + M1 + M3(AI integration) |
| Provenance & Trust | 4 | M5 |
| Security & Privacy | 3 | M4 + M6 |
| Communication | 2 | M9(brief/demo) |
| Bonus: Nightingale Alignment | 10 | M3(self-learning) + M7 + data decay |

---

## 8. Hard-Constraint Checklist (Verify Before Submission)

- [ ] RBAC enforced server-side (RLS + backend checks), not UI layer
- [ ] Glance P95 ≤ 300ms (warm path), measurement method stated in the brief
- [ ] PHI (names/IC/ID/phones) redacted **before** sending to LLM
- [ ] Synthetic data only
- [ ] TLS in transit + encryption at rest
- [ ] All 5 test files present and passing; README documents how to run them
- [ ] README explains where redaction happens and how RBAC is enforced
- [ ] 2-3 page technical brief + architecture diagram + full data schema + trade-offs
- [ ] ATTRIBUTION.txt (all external libraries/models + licenses)
- [ ] Demo video covering scenarios A/B/C
