# Nightingale 72-Hour Build — Phased Implementation Plan (PLANNER)

## 0. Orientation

**Current state:** Greenfield. The repo contains only `2026 72 Hour Build_ Nightingale Candidate Brief 2.md`, `REQUIREMENTS.md`, `REQUIREMENTS_EN.md`, a stub `README.md`, and `Nightingate_initial.md`. **No application code exists.** The clock is already running: today is 2026-08-26 (Wed); the deadline is 2026-08-28 (Fri) 17:30 SGT/MYT — roughly **~64 wall-clock hours** remain, so the 72h budget below assumes aggressive parallelization and a hard cut list (Section 6).

**Scoring map (the priority ordering that drives every scheduling decision):**

| Dimension | Pts | Primary modules | Build strategy |
|---|---|---|---|
| Glanceability & Actionability | 6 | M2 (Glance), M3 | Build first (Day 1 slice), polish with highlight engine (Day 2) |
| Collaboration & AI Integration | 5 | M2 (comments/versions), M1, M3 (AI scribe) | Day 2 core; AI scribe ingestion begins Day 1 |
| Provenance & Trust | 4 | M5 | Day 2; provenance_pointer is in the schema from Day 1 |
| Security & Privacy | 3 | M4 (RBAC), M6 (redaction) | RLS + redaction chokepoint are in the Day-1 foundation |
| Communication | 2 | M9 (brief/demo) | Continuous drafting; final 6h buffer |
| **Bonus Nightingale Alignment** | **10** | M3 (self-learning), data decay, M7 (voice) | Harvest self-learning + decay (cheap) before voice (heavy) |

**Note on the 10-point bonus:** the bonus is worth more than any single core dimension. The plan deliberately harvests the two cheap bonus items (self-learning weights + data decay) before the expensive one (voice capture), because they reuse M1/M3 plumbing.

## 1. Execution Model (how 72h maps to work)

Three parallel tracks so a single builder + AI agents can cover the surface:

- **Track A — Backend/Data:** FastAPI, Postgres schema + RLS, RBAC middleware, redaction pipeline, LLM service, micro-tests.
- **Track B — Frontend/UX:** Care Note single page, Glance Top Card, timeline renderer, collaboration UI, revision/diff UI, provenance jump.
- **Track C — Docs/Demo:** README, 2-3p technical brief, ATTRIBUTION.txt, screen recording, seed script coordination.

Rule: the **API contract is fixed at the end of Day 1** so A and B never block each other. Every phase lists its target modules, hours, and scoring dimensions.

## 2. DAY 1 (0–24h): Foundation, RBAC, and the de-risked vertical slice

### Phase 1 — Scaffold, Schema, RLS, Seed Generator (0–6h) — Modules: M1, M6, M9 — Track A/C
- Repo scaffold: `backend/` (FastAPI), `web/` (Next.js + Tailwind), `docker-compose.yml` (Postgres), Alembic migrations, `.env` template, pytest + ruff setup.
- **Data model (freeze this at commit):**
  - `clinic(id, name)`, `user(id, clinic_id, role[patient|staff|clinician|admin], display_name)`
  - `patient(id, clinic_id, name, ic_number, phone, allergies[], medications[], chief_complaint)` — synthetic only
  - `entry(id, patient_id, author_role, author_id, entry_type[ai_doctor_consult_summary|ai_nurse_consult_summary|ai_patient_session_summary|manual_staff_note|manual_clinician_note|system_event], timestamp, body, provenance_pointer, risk_level, clinic_id)`
  - `entry_section(id, entry_id, section_key, owner_role)` — clinician_sections vs staff_sections (enables non-overwrite rule)
  - `entry_version(id, entry_id, version_no, snapshot, author_role, author_id, created_at)` — full snapshots
  - `comment_thread(id, entry_id, section_span, status[open|resolved], created_by_role)` and `comment(id, thread_id, body, mentions[], assignee_role|null, created_at)`
  - `highlight(id, patient_id, source_entry_id, source_span, text, risk_reason, provenance_pointer, status[pending|accepted|rejected], created_at, created_by)`
  - `provenance(id, entry_id, source_type[ai_session|consult_transcript], source_ref, source_span)` — every AI entry + highlight points here
  - `task(id, patient_id, title, assignee_role, status[open|done], due, source_entry_id)`
  - `audit_log(id, user_id, role, action, entity_type, entity_id, timestamp, clinic_id)` — **metadata only, no content**
  - `learning_weight(id, patient_id, feature, weight, updated_at)` + `interaction_log(id, patient_id, user_role, action[pinned_highlight|edited_ai_note|commented_ai_note], feature, timestamp)` — for self-learning
  - `top_card(id, patient_id, payload_json, updated_at)` — the precomputed Glance
- **Postgres RLS policies** per role × clinic (baseline; refined in Phase 2).
- **Synthetic seed generator** (`scripts/seed.py`): one clinic, 2 patients, users of all 4 roles, entries spanning **2025-04-15 … 2026-02-06** (the two dates the brief names), all 3 AI scribe types + manual notes + open tasks. This is what makes every demo one command.
- **Scoring:** Security 3 (RLS foundation), Communication 2 (README skeleton), and the base for all other dimensions.

### Phase 2 — Vertical Slice: Auth + RBAC + Timeline + Glance (6–18h) — Modules: M2, M4, M8 — Tracks A/B
- Auth: JWT login for seeded users; FastAPI dependency resolves current user + role + clinic.
- RBAC rules enforced in **RLS + middleware** (defense-in-depth): patient sees only patient-facing summaries (never internal comments or raw AI notes); staff RW staff_notes only, clinic-scoped; clinician RW clinician_sections, R staff_notes + all AI notes, clinic-scoped; admin clinic-wide R. Clinician cannot overwrite staff notes and vice versa (section ownership).
- APIs: `GET /patients/{id}` page bundle; `GET /patients/{id}/timeline` (paged, time-ordered, entry metadata); `GET /patients/{id}/glance` (reads precomputed `top_card`, single query).
- **Write `test_rbac_scope.py` first (RED), then implement to GREEN** — asserts staff/clinician cannot write/edit as each other; patient cannot read internal comments or raw AI notes; all checks fail server-side (401/403).
- Frontend: patient page shell, timeline renderer with **visual AI-vs-manual distinction** (`author_role=system` badge, "system-generated" styling), Glance Top Card component showing open actions + risk flags.
- **Perf smoke test on the glance endpoint** (the 300ms P95 hard constraint); precompute path already in place so the read is one cached query.
- **Scoring:** Glanceability 6 (first usable Glance), Security 3 (RBAC), base for Collaboration+AI and Provenance.

### Phase 3 — AI Scribe Ingestion + Redaction Pipeline (18–22h) — Modules: M6, M2, M8 — Tracks A/B
- **PHI redaction pipeline** (`redact.py`, the single chokepoint): strips names / IC-ID / phones via rule-based patterns, called **before any LLM call**, and also applied to every log line (clean-logs). Unit tests assert the three pattern classes are removed.
- **LLM service abstraction**: `summarize(transcript, scribe_type)` — mock LLM with deterministic canned output first (tests/demos never depend on a live key), real model behind the same interface.
- **Ingestion endpoint** `POST /patients/{id}/ai-scribe`: accepts a consult/session payload → redacts → summarizes → writes an entry with `author_role=system`, one of the 3 `entry_type` values, and a `provenance_pointer` to `session_id`/transcript segment. Also upserts `top_card` (precompute on write).
- Frontend: AI entries render distinctly, expandable to show source pointer.
- **Scoring:** Collaboration+AI 5 (scribe integration), Security 3 (redaction), Provenance 4 (pointer on every AI entry).

### Phase 4 — Day-1 Test Checkpoint + Commit + README draft (22–24h) — Modules: M8, M9
- Run `test_rbac_scope.py` + redaction unit tests + glance perf smoke; gate on green. Commit (schema freeze). Draft README (setup/run, where redaction happens, how RBAC is enforced). Start screen recorder.
- **Scoring:** Security 3 (verified), Communication 2 (draft), all dimensions benefit from a working slice.

**Day-1 exit state:** a runnable app showing a glanceable Timeline + Top Card for a seeded patient, server-side RBAC proven by a passing test, AI scribe ingestion with redaction, and demo data covering both required dates.

## 3. DAY 2 (24–48h): Collaboration, Revision, Glance Engine, Provenance

### Phase 5 — Inline Collaboration: comments/threads/mentions/tasks (24–34h) — Modules: M2, M1 — Tracks A/B
- Comment threads with **resolve/unresolve** state machine; `@nurse_name`/`@clinician_name` mentions; assignments ("Assign to staff") creating `task` rows; in-app notification badge (no email/SMS).
- Section-level editing with role ownership enforced server-side (clinician cannot save to staff-owned section, and vice versa).
- Frontend: inline comment affordance on any entry/span, resolve toggle, mention autocomplete, assignee dropdown.
- **Scoring:** Collaboration+AI 5, Glanceability 6 (open tasks feed the Glance card).

### Phase 6 — Revision History, Revert, Concurrent Edits (34–42h) — Modules: M2, M1, M8 — Tracks A/B
- Every edit writes a full snapshot to `entry_version` and a metadata-only row to `audit_log` (who/what/when, no content).
- "View changes since X": on-demand diff between snapshots; **revert** restores a chosen snapshot as a new version (never destructive).
- **Concurrent edits:** different sections merge via JSON patch; same-section conflict → deterministic resolution by version number (highest wins) + a visible **conflict flag** on the entry for review.
- **Write `test_revision_history.py` and `test_concurrent_edits.py` first, implement to GREEN.**
- **Scoring:** Collaboration+AI 5 (revision control), Provenance 4 (auditability feeds trust).

### Phase 7 — Highlight Engine + Provenance Navigation + Conflict Handling (42–48h) — Modules: M3, M5, M8 — Tracks A/B
- **Highlight/prioritization engine:** score = f(recency, explicit `risk_level`, tagged clinical entities [meds/chief complaint/allergies via rule-based NER or LLM], unresolved tasks). Top-N become Glance highlights.
- Every highlight shows **`risk_reason` + `provenance_pointer`** and a fast accept/reject control (hard constraint).
- **Provenance navigation:** clicking a highlight jumps to its exact source entry/span in the timeline (scroll + flash).
- **Conflict handling — both paths:** clinician edits take precedence over AI/patient memory, OR the system flags the conflict for review (a `conflict_flag` badge on the affected AI entry). Demonstrate both in the UI.
- **Write `test_highlight_provenance.py` first; every generated highlight's pointer must resolve to a real timeline span.**
- **Scoring:** Provenance 4 (full), Glanceability 6 (highlight polish), Bonus 10 (pinning is the learning signal).

**Day-2 exit state:** collaboration + revision + revert + conflict handling + click-through provenance, with 4 of the 5 required test files passing.

## 4. DAY 3 (48–72h): Bonus harvest, hardening, deliverables

### Phase 8 — Self-Learning Importance + Data Decay (48–56h) — Modules: M3, M1, M8 — Track A
- **Self-learning:** log clinician interactions (`interaction_log`: pinned highlights, edits/comments on AI notes) → update persisted `learning_weight` per feature → boost future highlights of similar content. Accept/reject stays fast and explicit (hard constraint).
- **Write `test_self_learning_importance.py`:** simulate pinning a highlight from an AI-scribed note; assert subsequent similar suggestions rank higher (conceptual test acceptable per brief).
- **Data decay:** schema for archiving (compressed summary rows for entries older than a threshold, e.g., > 90 days) + age-based demotion in timeline and Glance; demonstrate one decayed entry and document the policy in the brief.
- **Scoring:** Bonus 10 (self-learning + decay), Glanceability 6 (adaptive ranking keeps the card relevant).

### Phase 9 — Ambient Voice Capture (best-effort) (56–62h) — Modules: M7, M6 — Track B
- **Patient view:** PWA `MediaRecorder` capture → redact before LLM → transcription → structured-fact extraction → patient session summary entry with provenance.
- **Clinical view:** speaker-labelled transcript, timestamps, confidence markers, clinical summary, provenance to source segments (Whisper family).
- **Descoppe-able:** if behind, ship a single pre-recorded sample through the redact→transcribe→summarize pipeline and document the full architecture in the brief.
- **Scoring:** Bonus 10 (voice portion), Security 3 (voice redaction path).

### Phase 10 — Full Test Pass + Hard-Constraint Verification (62–66h) — Modules: M8 — Track A
- Run all 5 test files + unit/integration suite; fix regressions.
- Verify hard-constraint checklist: RBAC server-side (not UI), Glance P95 ≤ 300ms warm **with measurement method recorded**, PHI redacted before LLM across all streams, synthetic data only, TLS in transit + encryption at rest, clean logs.
- **Scoring:** Security 3 (verified end-to-end), Communication 2 (evidence for the brief).

### Phase 11 — Deliverables: README, Technical Brief, ATTRIBUTION (66–70h) — Modules: M9 — Track C
- Finalize README (setup/run, **where redaction happens**, **how RBAC is enforced**, how to run tests).
- **2-3 page technical brief:** architecture diagram, full data schema (Entries ↔ Comments ↔ Versions ↔ Highlights ↔ Provenance ↔ AI_Scribed_Notes ↔ learning weights), assumptions/trade-offs, the 300ms measurement method, data-decay policy, self-learning design.
- **ATTRIBUTION.txt:** every external library/model + license (add entries as deps land — never reconstructed at the end).

### Phase 12 — Demo Video + Submission Buffer (70–72h) — Modules: M9 — Track C
- Assemble the 3-scenario video (A: Glance + AI scribe provenance jump; B: collaboration + audit + revert + learning; C: longitudinal history + highlight logic + data decay) from Day-1+ screen capture; record voiceover; keep it under ~5 minutes.
- Final commit history tidy, hard-constraint checklist re-run, email submission (repo link/zip + brief + deliverables).

## 5. Critical Path

The longest non-parallelizable chain, with the de-risk slice at its head:

```
M1 schema+RLS+seed  →  Auth + RBAC + timeline/glance APIs  →  Timeline/Glance UI
  →  AI scribe ingestion + redaction  →  Version history + revert
  →  Highlight engine + provenance navigation  →  Self-learning weights (bonus)
  →  Final test pass  →  Brief + demo video  →  submission
```

Any slippage in M1, RBAC, or the AI ingestion path pushes everything downstream. Controls: schema frozen Day 1; seed data present Day 1 (UI never blocked on data); API contract fixed end of Day 1 (tracks parallel); tests written per feature so regressions surface within hours.

## 6. Build-First (De-Risk) Priorities

1. **Postgres schema + RLS + one seeded patient** (hours 0-6) — proves tenancy, roles, and the data model before any UI exists.
2. **The vertical slice** (Glance + timeline + RBAC, hours 6-18) — de-risks the 6- and 3-point dimensions and surfaces integration issues earliest.
3. **PHI redaction pipeline + its unit tests** (hours 18-22) — a hard constraint, cheap to verify, must not slip.
4. **300ms Glance perf smoke test** (hour 18) — validates the precompute approach before it is built upon.
5. **Synthetic seed covering both required dates + all 3 scribe types** (hours 0-6) — makes every demo reproducible from Day 1.

## 7. Minimal-but-Safe MVP (what to cut if time runs short)

**Must keep (P0 core + hard constraints):** M1 schema+RLS; server-side RBAC; Glance ≤300ms; timeline with 3 AI scribe types + manual notes; PHI redaction before LLM; provenance_pointer on every entry; version history + revert; highlights with `risk_reason` + provenance + accept/reject; the 5 test files; README; brief; ATTRIBUTION.txt; demo video (Scenario A alone if pressed).

**Cut/descope, in this order (least painful first):**
1. **Real-time WebSocket collaboration** → optimistic UI + section-level version checks (last-write-wins + conflict flag). Still passes `test_concurrent_edits.py` via section ownership.
2. **@mentions / assignments / notifications** → keep a minimal assignee field on comments; drop the notification engine.
3. **Ambient voice capture** (both views) → ship a pre-recorded sample through the pipeline + document architecture. Bonus-only, so cut first among bonuses.
4. **Data-decay background job** → keep the schema + one demonstrated decayed entry + documented policy; skip the scheduler.
5. **Self-learning** → keep weights + the single pin→boost signal + its test; keep it simple and explainable.
6. **"View changes since X" diff UI** → plain snapshot side-by-side diff, not an OT engine.

## 8. Testing Checkpoint Schedule

| # | Time | Gate |
|---|---|---|
| 1 | ~22-24h (end Day 1) | `test_rbac_scope.py` + redaction unit tests + glance perf smoke all green; seed reproducible; commit (schema freeze) |
| 2 | ~42h (Day 2) | `test_revision_history.py` + `test_concurrent_edits.py` green |
| 3 | ~48-50h (start Day 3) | `test_highlight_provenance.py` green; walkthrough of P0 features vs hard-constraint checklist; **re-read brief for the announced ~48h hint and set priorities for the final 24h** |
| 4 | ~62-66h (Day 3) | full suite (5 files) + extras green; 300ms P95 measured & documented; redaction/log-cleanliness tests pass |
| 5 | ~70-72h (final) | submission checklist: hard constraints re-verified, all 5 tests pass, README/brief/ATTRIBUTION present, video done, commit history clean |

## 9. Deliverables Buffer

- **Continuous:** README drafted Day 1 and updated per feature; ATTRIBUTION.txt appended at the moment each dependency is added; screen recording runs from Day 1 so the demo video is an edit, not a shoot; brief notes accumulated in a `docs/` scratch file each day.
- **Dedicated reserve (66-72h):** 2h brief finalize + ATTRIBUTION audit + README polish; 2h video assembly/voiceover; 2h submission buffer (repo hygiene, checklist, email). **No new features inside this block** — it is protected by the cut list, not squeezed.

## 10. Scoring Dimension → Phase/Point Capture

| Dimension | Pts | Phases that deliver it | Realistic capture |
|---|---|---|---|
| Glanceability & Actionability | 6 | Ph2 (slice), Ph7 (highlights), Ph8 (adaptive ranking) | 5-6 |
| Collaboration & AI Integration | 5 | Ph3 (scribe), Ph5 (collab), Ph6 (revision) | 4-5 |
| Provenance & Trust | 4 | Ph3 (pointers), Ph7 (jump + conflicts) | 3.5-4 |
| Security & Privacy | 3 | Ph1 (RLS), Ph2 (RBAC), Ph3 (redaction), Ph10 (verify) | 3 |
| Communication | 2 | Ph4/11/12 (README, brief, video) | 2 |
| Bonus: Nightingale Alignment | 10 | Ph8 (learning + decay), Ph9 (voice) | 5-8 |

## 11. Key Assumptions & Open Questions

- **Calendar reality:** ~64 wall-clock hours remain, not 72; the 72h budget is the target, and Section 7 is the lever that keeps it real. A single builder with heavy OMC/executor-agent delegation is assumed.
- **Test names imply Python** → FastAPI chosen; Postgres chosen for native RLS (the brief's "server-side enforcement" is most defensible via RLS + middleware backstop).
- **"Clinician cannot overwrite staff / staff cannot overwrite clinician"** is implemented as section ownership (`entry_section.owner_role`) + app-layer checks on top of RLS; the micro-test asserts the behavior.
- **Conflict strategy** is deterministic (version number, highest wins + conflict flag) and documented in the brief as the explicit architecture choice.
- **Open question for the brief:** whether "real-time" collaboration must be true OT/CRDT or a demonstrable non-destructive concurrent-edit story; we ship the latter and state the trade-off.
