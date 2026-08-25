# Nightingale 72-Hour Build — Deep Requirements Analysis

Analyst review of the Chinese and English requirements breakdowns against the original candidate brief.
Source of truth: `/Users/apple/Nightingate_initial/2026 72 Hour Build_ Nightingale Candidate Brief 2.md`
Reviewed artifacts: `/Users/apple/Nightingate_initial/REQUIREMENTS.md` (Chinese) and `/Users/apple/Nightingate_initial/REQUIREMENTS_EN.md` (English). The two breakdown files are faithful translations of each other with no substantive drift; the analysis below treats them as one document.

---

## 1. Bottom-line assessment of the REQUIREMENTS breakdown

The breakdown is strong on coverage: it captures all 8 brief modules (Care Note page, Timeline, Inline Collaboration, Revision History, AI Scribe, RBAC, Provenance, plus security pipeline), the 4 required micro-tests plus the bonus test, the 3 deliverables, and it has a genuinely good implicit-requirements list (16 items) that already includes multi-clinic tenancy, glance pre-aggregation, notifications for mentions/assignments, resolve/unresolve state machine, learning-weight persistence, point-in-time diffs, PHI redaction timing, demo reproducibility, and clean-log compliance.

It is materially wrong in three ways:

1. **Scoring mis-prioritization.** The breakdown states the priority as "Glanceability(6) > Collaboration+AI(5) > Provenance(4) > Security(3) > Communication(2) > Bonus(10)". Bonus (Nightingale Alignment) is 10 points — the single largest bucket, 33% of the maximum 30. The arrow implies Bonus is last in importance, but it is worth more than any individual core dimension. Self-Learning and Data Decay are scored; treating them as a generic "P1 加分项" alongside voice capture understates them.
2. **Voice capture is not a scored bonus.** The brief's scoring line reads "Bonus Nightingale Alignment: (10): Hybrid Storage / Data Decay Policy, Self-Learning Implementation". Ambient Voice Capture is Bonus item 5 and earns "extra bonus credit", but it is NOT listed in the 10-point line. The breakdown treats M3 self-learning, M7 voice, and data decay as one equal P1 group. Voice is the lowest ROI item and should be the first cut.
3. **Section granularity is missing and it is load-bearing.** Three things in the brief force a `section`/`scope` discriminator on entries: (a) the RBAC rule "Clinicians cannot overwrite Staff notes. Staff cannot overwrite Clinician notes" (staff writes staff_notes, clinician edits clinician_sections — separate scopes); (b) the concurrency micro-test "two roles editing different sections concurrently"; (c) Scenario B "edits a section of the patient's plan". The breakdown's M1 models "Entry/Note" as a generic entity without sections, which makes RLS enforcement and the concurrency test unimplementable as specified.

---

## 2. Scoring reality check (what the 30 points actually are)

| Bucket | Points | Weight of max | Requirements doc says |
|---|---|---|---|
| Glanceability & Actionability | 6 | 20% | P0 core, prioritize (correct) |
| Collaboration & AI Integration | 5 | 17% | P0 core (correct) |
| Provenance & Trust | 4 | 13% | P0 core (correct) |
| Security & Privacy | 3 | 10% | P0 hard constraint (correct) |
| Communication | 2 | 7% | P0 deliverable (correct) |
| Bonus: Nightingale Alignment (self-learning + data decay) | 10 | 33% | Treated as P1 bonus (WRONG — it is the biggest bucket) |
| Ambient voice capture | not scored | — | Treated as equal P1 (WRONG — it is unscored extra credit) |

Strategic implication: secure the 20 explicit core points first (they are required and graded), but once the 4 required micro-tests and the P95<=300ms glance are green, the highest-ROI remaining work is a *simplified but real* self-learning mechanism and a *data-decay schema + archival job* — not voice capture. A credible self-learning demo is additionally required implicitly by Scenario B ("Audit Trail & Importance Learning"), so it is effectively needed anyway.

---

## 3. Validation of the breakdown module-by-module

| Breakdown module | Verdict vs brief | Notes / gaps |
|---|---|---|
| M1 Data model & persistence | Mostly correct | Missing `section`/`scope` discriminator; missing `clinic_id` called out at schema level (it is in implicit list #1 but must be in the DDL); no explicit ingestion entity/model for how AI notes arrive; "audit metadata only" is correct and matches test. |
| M2 Care Note page | Correct intent | "Real-time collaboration (WebSocket)" is over-scoped — the brief only requires the concurrency test to pass, which optimistic locking satisfies. Missing the "Plan" section and patient-facing summary/instruction views. Missing timeline pagination for 10 months of data. |
| M3 Glance / Smart Prioritization | Correct core | Missing that open actions/tasks are a required input; missing that highlights must come in two flavors (AI-suggested with accept/reject vs clinician-created manual highlight); missing learning-weight persistence detail (present in implicit list #6). |
| M4 RBAC | Correct | Missing cross-table RLS (comments, versions, highlights, tasks, audit) and the restricted app-role requirement; missing that staff/clinician overwrite rules need per-section (not per-note) policies. |
| M5 Provenance & Trust | Correct | Missing span-stable pointers (survive edits), two-level provenance for AI highlights (highlight -> AI note span -> source session), and soft-delete so pointers never dangle. |
| M6 Security pipeline | Correct | Missing that redaction must sit at EVERY LLM ingress (3 AI-note ingestion flows, highlight generation, summary generation, learning signals, voice) and that clean logs apply to ingestion/redaction service logs too. |
| M7 Ambient voice | Captured but mis-tiered | Not in the scored bonus line; should be the last stretch item, and only a minimal patient-voice mock is worth attempting. |
| M8 Micro-tests | Correct names | Header says "5 specified files" but only 4 are required (self-learning is BONUS in the brief). Failure modes enumerated in section 7. |
| M9 Deliverables | Correct | Brief's technical-brief requirement "show how/if learning mechanism integrates into the schema" is not explicit in M9; seed script for demo reproducibility is in implicit list #13 (keep). |

---

## 4. Missing requirements (not in the breakdown; must be added)

Severity: [HIGH] = load-bearing / risk of failing a micro-test or the hard constraints; [MED] = needed for demo or scoring; [LOW] = polish.

1. [HIGH] **`section`/`scope` discriminator on entries.** Values at minimum: `staff_note`, `clinician_section` (incl. the patient "plan"), `patient_summary`, `ai_doctor_consult_summary`, `ai_nurse_consult_summary`, `ai_patient_session_summary`, `instruction`, `system_event`. Enables RLS ("staff writes staff_notes only"), the concurrency test ("different sections"), and Scenario B ("plan section").
2. [HIGH] **Task/assignment as a first-class entity feeding the Glance.** "Open actions (e.g., needs lab order, waiting nurse follow-up)" and "unresolved tasks" are a primary input to both the 6-pt Glance and the importance logic. Minimum viable: task with assignee role, status open/closed/resolved, due flag, linked entry. Assignments are "optional" in the brief but effectively required by the Glance scoring.
3. [HIGH] **AI-scribe ingestion pipeline.** A first-class flow (API endpoint/service) that receives the 3 AI note types, runs PHI redaction BEFORE any LLM call, sets `author_role=system`, stamps the correct `ai_*` type, and attaches `provenance_pointer` (e.g., session_id / source segments). The breakdown models storage/display but not the ingestion path, and "redact before LLM" cannot be demonstrated without it.
4. [HIGH] **Cross-table RLS + restricted application DB role.** RLS must be enabled on entries, comments, versions, highlights, tasks, and audit; the app must connect as a non-superuser role so RLS actually fires in dev/test/prod; micro-tests must run as that role.
5. [HIGH] **Patient-facing summary/instruction generation.** RBAC says the patient "can view patient-facing summaries and instructions generated from the clinic web app notes." This is a generation mechanism (LLM, redacted, simplified) plus a patient view — currently unmodeled.
6. [MED] **System-generated events as timeline entries.** The brief lists them ("system-generated events") — e.g., entry created, task resolved, version reverted, conflict flagged, highlight accepted. Needed for the timeline feed to feel complete and for Scenario C.
7. [MED] **Span-stable provenance.** Highlights point at a segment; raw char offsets shift after edits. Store stable segment ID + anchor text; resolver returns the entry/span or an explicit tombstone.
8. [MED] **Two-level provenance for AI highlights.** A highlight inside an AI note resolves to the AI note's span, AND the AI note itself resolves to the source session/segments (provenance_pointer). test_highlight_provenance should exercise both hops.
9. [MED] **Timeline pagination / lazy loading.** 10 months of longitudinal data must not be shipped in one payload (perf + P95 implications).
10. [MED] **Deterministic seed data with fixed dates.** Scenario C explicitly names Apr 15 2025 and Feb 6 2026; a seed script (one command) must reproduce all 3 demo scenarios, spanning clinics and roles, and should include PHI-like names/ICs/phones so the redaction pipeline is demonstrably exercised.
11. [MED] **P95 latency budget + measurement methodology published.** The brief requires you to "state how you measured/approximated this in your brief." Produce a budget (e.g., static assets cached; one precomputed glance JSON; DB read < 50ms; serialization < 50ms; total P95 < 250ms) and a reproducible measurement (script, N>=200 warm requests, define "warm").
12. [MED] **Glance cache invalidation rules.** Recompute the glance_snapshot on entry create/edit, task status change, comment resolve, highlight accept/reject. Without this the pre-aggregation goes stale or is recomputed on the read path.
13. [MED] **Revert semantics for children.** Decide what happens to comments/highlights attached to a reverted version (keep attached to the new head; never hard-delete — soft-delete).
14. [MED] **Attribute diffs to authors.** "view changes since X" should show who changed what per field (ties to audit + trust scoring).
15. [LOW] **Open design decision: may the patient write entries directly?** The brief gives patient contributions via AI sessions and voice capture; it does not grant direct write to the care note. Decide and encode in RLS (recommended: patient writes only via AI-patient sessions/voice, plus patient-facing summary reads).
16. [LOW] **"Key questions" from AI-patient sessions** as a distinct field surfaced to clinicians in the glance (the timeline entry type includes "key questions").
17. [LOW] **System role attribution.** AI/system entries must be attributed to `system` in audit (who changed what), not to the seeding user.

---

## 5. Mis-stated / mis-prioritized requirements in the breakdown

1. **Priority arrow is misleading.** "Glanceability(6) > ... > Bonus(10)" reads as if Bonus is least important. Re-rank by risk-adjusted points: core 20 first, then self-learning + data decay (scored 10), voice last (unscored).
2. **All three P1 bonuses treated equally.** Data decay + self-learning are the scored bonus; voice is extra credit. Split them.
3. **WebSocket real-time editing over-scoped.** The brief's "real-time ... collaboration" and the Google-Docs reference do not require OT/CRDT. The concurrency micro-test passes with section-level optimistic locking. Recommend: optimistic locking + short polling (or lightweight presence), NOT a full collaborative engine. This is a primary scope-cut candidate.
4. **Micro-test count.** M8 header says "5 specified files"; only 4 are required, the 5th (self-learning) is explicitly BONUS. Say so, so a flaky/time-heavy conceptual self-learning test does not threaten the core deliverable.
5. **Mentions/assignments framed as required collaboration.** The brief labels them "optional ... up to your creativity." Keep them minimal (a mention renders as a tag and creates a task/in-app notification; no email/push). They still matter for Scenario B.
6. **"Patient can view patient-facing summaries" reduced to a permission line.** It is also a functional generation requirement (see missing #5).

---

## 6. Requirement dependencies and build ordering

Dependency DAG (must-haves):
```
Schema + RLS + app role  ──> timeline read API ──> Glance precompute + endpoint (P95 target)
       │                       │                     └─ needs: tasks (open actions), highlights
       ├──> sections (staff_note / clinician_section / plan / ai_*)
       ├──> AI ingestion (redaction BEFORE LLM) ──> timeline display as author_role=system
       ├──> comments (thread + resolve) ; tasks ──> glance open-actions
       ├──> versions (snapshots) + diffs + revert ──> "view changes since X"
       ├──> concurrency control (per-section OCC) ──> test_concurrent_edits
       ├──> highlights (risk_reason + provenance) ──> provenance resolver (span-stable) ──> test_highlight_provenance
       ├──> RBAC hardening (cross-table RLS) ──> test_rbac_scope
       └──> redaction + clean logs ──> test_revision_history (audit metadata only)
Bonus layer (after core is green):
       ├──> self-learning: needs highlights + interaction_log + learning_weights persistence + scoring path that consults weights ──> test_self_learning
       └──> data decay: archive schema/partition + archival job ──> Scenario C explanation
Deliverables (last, but video/README are hard deadlines): seed script, README, technical brief, ATTRIBUTION.txt, demo video.
```

Recommended 72h (realistically ~55-65h) ordering:
1. Schema + RLS + restricted role + deterministic seed generator (half day) — everything depends on this.
2. Timeline + AI ingestion (3 types, synthetic) + system events (day 1) — the feed all features read from.
3. Sections + staff/clinician write APIs + minimal tasks (day 1-2) — prerequisite for Glance.
4. Glance endpoint with precomputed snapshot + P95 measurement script (day 1-2) — highest score (6).
5. Comments (thread + resolve) + mentions/assignments + versioning (snapshots) + per-section OCC + revert + view-changes-since-X (day 2).
6. Highlights (suggested + manual) + provenance resolver + conflict flag/precedence (day 2).
7. RBAC hardening (cross-table RLS, negative-path checks) + redaction across all LLM streams + clean logs + the 4 required micro-tests passing (day 2-3).
8. Bonus: simplified self-learning (weight table + interaction log) + data decay schema + archival job (remaining time).
9. Docs + demo video + technical brief + ATTRIBUTION (last 4-6h — non-negotiable).

Write the 4 required micro-tests against API contracts early (they define the acceptance of RBAC, versioning, provenance, concurrency), even though they only pass once the features land.

---

## 7. Hardest acceptance criteria and how each can fail

### 7.1 test_rbac_scope.py
Requirement: staff and clinicians cannot write/edit notes as each other; patient cannot access internal comments or raw AI-scribed notes.
Failure modes:
- RLS exists only on `entries`; comments/versions/highlights/tasks/audit are exposed — patient fetches an internal comment by guessed ID (IDOR) and reads staff/clinician discussion.
- App connects as a superuser/table owner (RLS bypassed), or tests run as the owner role and silently pass.
- Enforcement is UI-only (buttons hidden, API open) — the test must hit server endpoints as each role and expect 403, not just assert on the UI.
- No `section` discriminator: "staff writes staff_notes only" cannot be expressed; a generic note table forces either over-permissive or over-restrictive policies.
- No `clinic_id` filter: staff reads another clinic's patient data.
- Role spoofing: server trusts a client-supplied role/JWT claim without verifying it.
- Raw-SQL / stored-procedure paths bypass RLS; aggregate/search/glance endpoints join across scopes without RLS-aware queries.
- "Clinician cannot overwrite staff notes" implemented as "cannot edit any note not owned by you" — which wrongly blocks clinician edits to clinician_sections. Must be per-section, not per-note.
- Cross-clinic test missing entirely (brief only asserts cross-role and patient isolation; add a cross-clinic negative case for staff).

### 7.2 test_revision_history.py
Requirement: editing increments version; revert restores prior content; audit log shows who changed what (metadata only).
Failure modes:
- Version increments on every save (including no-op saves) — audit noise; define increment only on content change.
- Revert implemented in-place (mutates the current version) instead of creating a new head — the "prior state" assertion fails or history is corrupted.
- Diff-based storage + concurrent edits between snapshots → lossy revert (base mismatch). Prefer full snapshots per version.
- Audit log stores content (violates "metadata only" AND clean-logs) — assert audit rows have no content column and no PHI.
- Attribution wrong: AI/system writes attributed to the seeding user; or "who" taken from client body instead of server session.
- Revert does not produce its own audit entry (revert must be visible as a distinct action).
- Timeline ordering breaks after revert (revert should place the restored content as the new head, not reinsert at the old date).

### 7.3 test_highlight_provenance.py
Requirement: every highlight (incl. from AI-scribed notes) has a provenance_pointer that resolves to an entry/span.
Failure modes:
- Provenance stored as a free-text string with no resolver/FK — the test asserts non-null and passes, but clicking fails.
- Raw char-offset spans shift after an edit → resolver returns the wrong span or null.
- Hard-delete or revert of an entry orphans its highlights → pointer dangles. Use soft-delete/archival.
- AI-note highlight only resolves one hop (to the AI note) but not the AI note's own provenance (to the session) — the brief wants the source of truth to be reachable.
- A highlight sourced from multiple segments has a single pointer instead of a collection.
- The test does not actually call the resolver and assert the target exists.

### 7.4 test_concurrent_edits.py
Requirement: two roles editing different sections concurrently do not overwrite each other; same-section conflicts resolve deterministically.
Failure modes:
- Whole-document versioning or whole-note last-write-wins → editing section A then section B sequentially clobbers the first save.
- Optimistic locking with a whole-note version counter → both writers read v1, both write, second fails or silently overwrites.
- No OCC at all → lost updates.
- No `section` model → cannot even express "different sections".
- Deterministic resolution undefined: same-section conflicts resolved by wall-clock timestamps that tie, or random order → test is non-reproducible.
- The test uses real threads/async races and is flaky — instead simulate a deterministic interleaving (write A, write B, apply).
- Both writers are the same role → test does not catch cross-role overwrite.

### 7.5 test_self_learning_importance.py (BONUS)
Requirement: simulate pinning a highlight from an AI-scribed note; assert similar content later scores higher (may be conceptual).
Failure modes:
- Weights kept in memory only → restart wipes learning; persistence (learning_weights + interaction_log tables) is required for a credible claim.
- "Similar content" has no defined similarity metric (token overlap, entity overlap, embedding cosine) → the assertion is untestable.
- Weight table is updated but the scoring path never consults it → no observable change.
- Signals captured at the wrong point (e.g., only "pin", missing accept/reject/manual-highlight/edit/comment) → learning ignores most clinician interaction.
- Cold-start with empty weights → divide-by-zero / NaN → handle gracefully.
- Even though the test may be conceptual, Scenario B's demo must show a before/after priority change, so a minimal observable implementation is expected.

### 7.6 Glance P95 <= 300ms (warm path)
Failure modes:
- Top Card computed on the read path via an LLM call (1-10s) instead of a precomputed snapshot.
- N+1 queries: entries then per-entry comments/highlights/tasks → dozens of round trips.
- Missing index on (patient_id, timestamp) → full timeline scan.
- Glance recomputed on every request (no cache / no snapshot invalidation).
- Synchronous redaction or summarization on the read path (must be write-path work).
- Returning the full timeline payload to render the top card (endpoint bloat).
- Auth middleware doing DB lookups per request (cache sessions).
- "Warm" undefined and methodology absent — the brief explicitly requires stating how P95 was measured (script, N>=200 warm requests, concurrency, environment). A 1-request localhost timing is not credible.
- WebSocket streaming the glance (head-of-line blocking, first-connect latency) instead of a plain HTTP GET.

### 7.7 Server-side RBAC (the constraint itself)
Failure modes: consolidated list — child-table leaks; superuser/app-role bypass; UI-only enforcement; role spoofing; missing clinic_id; raw SQL/stored procedures; aggregate endpoints bypassing RLS; admin scope not clinic-limited; per-section vs per-note confusion; tests running as the wrong DB role. Mitigation recipe: one restricted role everywhere, RLS on every table, negative-path API tests per role, clinic_id on every row, and a README section describing the exact RLS + middleware architecture.

---

## 8. Must-have vs nice-to-have scoping for the remaining build window

Must-have (P0 — required for the 20 core points and the deliverables):
- Schema + RLS + restricted role; seed generator (deterministic, multi-clinic, PHI-like).
- Care Note page; timeline (entries of all types, author_role=system visually distinct, paginated).
- Glance top card: precomputed, shows top highlights (risk_reason + provenance + accept/reject) + open actions (tasks) + risk flags, P95 <= 300ms with published measurement.
- Sections incl. clinician "plan"; staff_notes and clinician_sections write paths with per-section RBAC.
- Comments (threaded, resolve/unresolve); minimal mentions + assignments feeding open actions.
- Version history (snapshots) + view-changes-since-X (diffs) + revert; audit metadata-only.
- AI-scribe ingestion for the 3 types with pre-LLM PHI redaction; display as distinct system entries with provenance.
- Highlights (AI-suggested + manual) and provenance resolution (jump to source); conflict flag/precedence path.
- 4 required micro-tests passing; README with run steps, where redaction happens, how RBAC is enforced.
- Technical brief (2-3pp), ATTRIBUTION.txt, demo video (Scenarios A/B/C), one-command local run.

Nice-to-have, in recommended cut order (cut earliest / lowest ROI first):
1. Ambient voice capture (both patient and clinical) — unscored extra credit; the full spec (diarization, overlap, noisy environments, multilingual, multi-device) is a multi-day rabbit hole. If attempted, ship only a canned patient-voice demo with a pre-recorded clip through Whisper, no real-time.
2. Real-time WebSocket collaborative editing (OT/CRDT) — replace with per-section optimistic locking + polling/presence. Saves a large fraction of a day and still passes the concurrency test.
3. Notifications (email/push) — in-app task inbox only.
4. Full self-learning ML — replace with a persisted weight heuristic (keyword/topic/entity affinity) + interaction_log; enough to pass the conceptual test and Scenario B's before/after demo. This is SCORED, so it outranks items 1-3.
5. Hybrid storage / data decay — schema (archive/partition + compression flag) + archival job + explanation. Also SCORED; implement after self-learning.
6. Advanced NER service — use the LLM at ingestion to tag clinical entities and cache them; no separate NER stack.

Recommendation: after the 20 core points are green, spend remaining time on (4) simplified self-learning and (5) data-decay schema — together worth 10 points — then stop. Voice is the last possible stretch.

---

## 9. Open design decisions the team must lock before coding

1. Section taxonomy: final list of `section`/`scope` values (suggest staff_note, clinician_section/plan, patient_summary, ai_doctor_consult_summary, ai_nurse_consult_summary, ai_patient_session_summary, instruction, system_event).
2. Patient write access: direct writes forbidden (patient contributes only via AI sessions/voice and reads patient-facing summaries) — recommended for a clean RBAC story.
3. Version storage: full snapshots per version (recommended) vs deltas. Snapshot makes revert and "diff since X" trivial; storage cost is irrelevant for synthetic data.
4. Same-section conflict resolution rule: role precedence (clinician > staff > patient/system) then higher version wins, with an explicit conflict flag recorded. Deterministic and testable.
5. Conflict model for clinician-vs-AI/patient memory: a clinician edit to an AI-scribed note either (a) creates a clinician-override entry flagged as override, or (b) creates a version of the AI note marked conflict-pending-review. Pick one; both are defensible, but the brief wants both "precedence" and "flag for review" paths demonstrable.
6. Revert semantics for comments/highlights on the reverted version: keep attached to the new head; soft-delete only.
7. Glance snapshot storage: a dedicated `glance_snapshot` table (JSON/columns) recomputed on writes, vs recompute-on-read with a 1-2s TTL cache. Recommend the snapshot table for a credible P95.
8. Similarity metric for self-learning: token-overlap on keywords + clinical-entity overlap is sufficient; keep it explainable for the brief.

---

## 10. Concrete recommendations (prioritized)

1. Add the `section`/`scope` discriminator and `clinic_id` to the schema before any code — everything else keys off it.
2. Add the AI-ingestion pipeline module and the tasks entity to the breakdown; both are load-bearing for the two highest-scored features (Glance 6, Collaboration+AI 5).
3. Fix the scoring tiers: core 20 first, then self-learning + data-decay (scored 10), voice last (unscored). Update the priority arrow.
4. Cut WebSocket OT/CRDT; use per-section optimistic locking.
5. Publish the P95 measurement methodology early (script + warm definition + budget) and keep it in the technical brief.
6. Enforce cross-table RLS with a restricted app role from day one; the 4 micro-tests must run as that role.
7. Use span-stable provenance (segment ID + anchor) and soft-delete so test_highlight_provenance and the demo's highlight-to-timeline jump never dangle.
8. Build the deterministic seed (fixed dates Apr 15 2025 / Feb 6 2026, multi-clinic, PHI-like values) as a one-command fixture; it is the backbone of demo reproducibility and doubles as a redaction-pipeline test.
9. Budget the last 4-6h for README + technical brief + ATTRIBUTION.txt + demo video; these are hard deliverables and the Communication (2) + demo-clarity scoring depends on them.
10. Track the real deadline: today is 2026-08-26; due 2026-08-28 17:30 SGT. Assume ~55-65h, not 72h, and freeze scope at the core-cut line if anything slips.

Primary source files:
- `/Users/apple/Nightingate_initial/2026 72 Hour Build_ Nightingale Candidate Brief 2.md`
- `/Users/apple/Nightingate_initial/REQUIREMENTS.md`
- `/Users/apple/Nightingate_initial/REQUIREMENTS_EN.md`
