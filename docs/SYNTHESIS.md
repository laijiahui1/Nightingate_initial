# Nightingale 72-Hour Build — Integrated Planning Package (SYNTHESIS)

> **Role:** TEAM LEAD synthesis of five specialist reports (Requirements Analysis, Phased Implementation, System Architecture, PostgreSQL Schema & Data Layer, Security & RBAC).
> **Due:** Friday, 2026-08-28, 17:30 SGT/MYT. **Today:** 2026-08-26. **Effective remaining window ≈ 60–64 h** (the 72 h title is nominal; everything below is budgeted to fit ~64 h by applying the cut order in §3.3).
> **Posture:** server-authoritative, deterministic, provenance-first, test-first. Sequence risk-adjusted points: secure the 20 core points first, then the 10-pt scored bonus, cut unscored voice capture last.

---

## 1. Finalized Tech Stack (one line per layer)

| Layer | Decision | Why (synthesis) |
|---|---|---|
| **Frontend** | **Vite + React + Tailwind PWA SPA** (NOT Next.js) | The product is a WebSocket-heavy realtime collaborative editor; CSR is the natural fit, SSR adds deploy/dev overhead with zero demo value, and a PWA is required anyway for mobile voice capture. |
| **Backend** | **Python / FastAPI** (one app service, routers per domain) | All 5 required micro-tests are `.py`; FastAPI gives fast iteration, async WS, and easy dependency-injected auth. |
| **Database** | **PostgreSQL 14+**, native **RLS** as the primary server-side RBAC backstop | `FORCE ROW LEVEL SECURITY`, session GUCs (`app.user_id/role/clinic_id`) set from verified JWTs, restricted application role in dev/test/prod; tests run as that role. |
| **Real-time** | **Plain WebSocket**, custom **section-level server-total-order last-write-wins + version + conflict_flag** | Reject OT/CRDT as overkill; section is the natural concurrency unit, deterministic convergence, directly testable by `test_concurrent_edits.py`. WS used for presence/refresh only, never document merging. |
| **LLM** | Single **LLMGateway** chokepoint; deterministic regex+lexicon PHI redaction before every call; **mock LLM from Day 1** | Redact-before-send is an invariant enforced at the call site; tests/demos never depend on a live model; any model (OpenAI/Claude) behind one interface. |
| **Transcription** | **faster-whisper (CTranslate2), local, base/small multilingual**; diarization optional | Raw audio never leaves the perimeter; only de-identified text reaches a hosted LLM. Hosted-API upgrade path documented. |
| **Storage** | Local filesystem behind an S3-compatible repository interface | Demo-appropriate; S3/Redis upgrade paths documented, never built. |
| **Glance** | Precomputed `patient_glance` JSONB snapshot per patient, recomputed synchronously on write | Warm read = one PK lookup (no joins/scans) → P95 ≤ 300 ms; measurement methodology (locust, N≥200 warm) published in the brief. |
| **Versioning** | Full snapshots per version + stored `delta_from_prev` optimization; **metadata-only audit log** with content sha256 | "View changes since X" = two-snapshot diff; revert = non-destructive new version. |
| **Encryption** | TLS 1.2/1.3 in transit; AES-256-GCM envelope at app layer on PHI-bearing columns (structural columns stay plaintext for RLS/indexing) + encrypted volumes/backups | Satisfies "encryption at rest" without breaking RLS predicates or the P95 path. |
| **Deliverables infra** | One docker-compose stack (API + Postgres + mock LLM + SPA); fixture Postgres for the 5 micro-tests; locust load script | One-command demo reproducibility + "how to run tests" requirement. |

---

## 2. Ordered 72-Hour Milestone Roadmap

> Each milestone is hours-from-start; task lists are ordered. **Effective compression:** the last 8 h of M6/M7 slack and the cut order (§3.3) absorb the ~8–12 h gap between the nominal 72 h and the realistic ~64 h window. Milestones are cumulative — nothing is demoable off the Care Note page before M3.

### M0 — Scaffold, Schema Freeze, One-Command Stack (H0–H4, 4 h)
- Scaffold FastAPI app + Vite/React/Tailwind SPA + docker-compose (API, Postgres, mock LLM, app) + fixture DB.
- Write and **freeze the schema** in the first commit (design `clinic_id`, `author_role`, `entry_type`/`section`, `provenance_pointer`, `entry_version` up front); **additive migrations only** thereafter.
- Wire the mock LLM service (deterministic canned output) and the **LLMGateway with the redaction chokepoint** before any feature work.
- Commit 1 checkpoint.

### M1 — Data Layer, RLS, Synthetic Seed (H4–H14, 10 h)
- Implement 13 core + 4 supporting tables: Clinic/User/Patient; `note_entries` (`kind`: `ai_doctor_consult_summary | ai_nurse_consult_summary | ai_patient_session_summary | staff_note | clinician_section/plan | patient_insight | instruction | system_event`); Entry→Comment→Version→Highlight→Provenance→AI_Scribed_Note chain; Task/Assignment; Mention; `learning_interaction` + `learning_weight`; metadata-only AuditLog.
- RLS on **every table including child tables** (comments/versions/highlights/tasks/audit): `USING`/`WITH CHECK` per role + `clinic_id` tenant filter + section-ownership predicate (`author_role = app.actor_role`, section `WITH CHECK`); `FORCE ROW LEVEL SECURITY`.
- Restricted application DB role; `SET LOCAL` GUCs inside each transaction from verified JWT; middleware defense-in-depth.
- **Synthetic seed generator** — first-class deliverable reproducing all 3 demo scenarios (fixed dates **2025-04-15** and **2026-02-06**), all 3 AI-scribed types.
- Security tests first: `test_rbac_scope.py` against live Postgres.

### M2 — Care Note Vertical Slice: Glance + Timeline + AI Scribe (H14–H26, 12 h)
- Single-patient Care Note page: Longitudinal Timeline (time-ordered, mixed manual + AI entries, `author_role=system` visually distinct) + **Glance Top Card**.
- **Glance precomputed at write time** into `patient_glance` (recency + risk_level + tagged entities + unresolved tasks + clinician-confirmed highlights), recomputed in the same transaction; warm read = one PK JSONB read.
- **AI-scribe ingestion pipeline** (endpoint/service): applies redaction → sets `author_role=system` + 3 `ai_*` types → stamps `provenance_pointer` to source session/segment **before** the LLM is called.
- Tasks/open-actions surfaced (drives Glance 6 pts + Scenario A).
- **Perf smoke test** (locust, N≥200 warm, P95) — hard constraint gate. Commit 2 checkpoint.

### M3 — Collaboration, Revision History, Concurrency (H26–H40, 14 h)
- Threaded comments (resolve/unresolve state machine), @mentions, assignments/tasks (first-class entities), notifications.
- **Revision history**: full snapshots, "view changes since X" (two-snapshot diff), non-destructive revert. `test_revision_history.py` written alongside.
- **Concurrent editing**: section-level write ownership + optimistic locking (`UPDATE … WHERE version=base`) + advisory locks keyed `(patient, section)` + deterministic tie-break (role precedence → version → id) + `conflict_flag`; clinician-over-AI precedence preserved. `test_concurrent_edits.py` with **deterministic sequential interleaving** (no racy threads).
- WebSocket presence/refresh fan-out (in-process asyncio pub/sub, Redis upgrade path documented).

### M4 — Highlights, Provenance, Conflict Handling (H40–H50, 10 h)
- Highlight engine: `risk_reason`, accept/reject, `provenance_id` FK required; **span-stable provenance** (stable segment ID + anchor text, NOT raw offsets) + soft-delete/archive so pointers always resolve; resolver returns target entry/span or an explicit tombstone.
- Click-to-navigate highlight → timeline span (Scenario A). `test_highlight_provenance.py` asserts **resolution returns the target**, not merely a non-null string.
- Conflict UX: clinician edit takes precedence **and** flags for review; both brief-demanded paths demonstrated.
- **H48 gate:** re-read the source brief for the announced ~48 h hint; 30–60 min decision gate; keep cuts reversible.

### M5 — RBAC Hardening, Redaction Completeness, Micro-Test Suite (H50–H60, 10 h)
- Harden RBAC across three layers (FastAPI route deps → service scope checks → RLS); admin clinic-scoped audit; patient isolation (no internal comments / no raw AI-scribed notes).
- Redaction completeness: deterministic regex+lexicon PHI (names / NRIC-FIN / phones), **applied to all log output** via one scrubbing log helper; unit tests assert zero PHI patterns on all seeded fixtures.
- Deliver **all 5 required micro-tests** green, runnable per README; CI step runs them against a live Postgres as the restricted role; cross-clinic leak tests assert 404/empty.

### M6 — Bonus: Self-Learning + Data Decay (then Voice stretch) (H60–H66, 6 h)
- **Self-learning (scored, 10-pt bonus):** persisted `learning_weight` (per clinic/feature, positive/negative counts + recency), `learning_interaction` event log, SECURITY DEFINER update functions (weights non-tamperable via RLS bypass), heuristic scoring from manual highlight/edit/comment interactions. `test_self_learning_importance.py` (conceptual OK) + Scenario B before/after weight change.
- **Data decay (scored):** nightly SECURITY DEFINER archive job moving old, unreferenced entry/version bodies to `entry_archive`/`entry_version_archive` (zstd-compressed bodies), leaving timeline stubs; refuses entries referenced by open highlights/tasks/comments; `restore_entry()` on demand; Scenario C demonstration.
- **Stretch only:** ambient voice capture (patient + clinical), local faster-whisper, redact-then-LLM. Cut first if time runs short (unscored extra credit).

### M7 — P0 Deliverables: README, Brief, ATTRIBUTION, Demo Video (H66–H72, 6 h — reserved, nothing new built inside)
- README: setup/run, **where redaction happens**, **how RBAC is enforced**, how to run the 5 tests.
- 2–3 page Technical Brief: architecture diagram + full data schema (Entries↔Comments↔Versions↔Highlights↔Provenance↔AI_Scribed_Notes, learning integration) + assumptions/trade-offs + P95 measurement methodology.
- ATTRIBUTION.txt: every external library/model + license (append the moment each dependency is added).
- Demo video covering Scenarios A/B/C; screen-recorded continuously from Day 1 so this is an **edit, not a shoot**; one-command run for reviewers.
- Final hard-constraint checklist pass (§5 of REQUIREMENTS_EN) + submission to the required email with correct subject line.

---

## 3. Where the Specialists Agree (convergence)

1. **Greenfield, server-authoritative, deterministic.** Repo is empty; first action is scaffold + schema; freeze the schema at the Day-1 commit with additive migrations only.
2. **PostgreSQL RLS is the RBAC backstop** (native server-side enforcement, satisfies the "UI-only insufficient" hard constraint), with FastAPI route + service checks as defense-in-depth and clean 403s for tests.
3. **Glance Top Card is precomputed/materialized at write time**, never computed on the read path; warm path is a single PK/JSONB read to hold P95 ≤ 300 ms; LLM stays strictly in ingestion/write paths.
4. **Full-snapshot version storage + metadata-only audit log** (no body content in AuditLog) — "view changes since X" is a two-snapshot diff; revert is a non-destructive new version; both satisfy `test_revision_history.py`.
5. **Section-level concurrency unit + deterministic LWW + conflict_flag**; explicit rejection of whole-document last-write-wins and of OT/CRDT; the losing version is always preserved in history.
6. **Single LLM chokepoint (LLMGateway) with deterministic, non-LLM, unit-testable PHI redaction** applied before every model call AND to all log output; **mock LLM from Day 1** so tests/demos never depend on the live model.
7. **Test-first: the 5 required micro-tests ARE the spec** for RBAC, revision, provenance, concurrency, and learning — written alongside each feature, never a final-weekend activity; tests run against live Postgres as the restricted role.
8. **Synthetic seed script is a first-class deliverable** reproducing all 3 demo scenarios with fixed dates (2025-04-15, 2026-02-06) — one-command demo.
9. **Section/scope is a first-class discriminator** (`staff_note | clinician_section | patient_insight | ai_* | instruction`): the only way RLS can enforce "clinician cannot overwrite staff notes and vice versa," and what the concurrency test's "different sections" implies.
10. **Provenance must be span-stable:** stable segment IDs + anchor text, not raw character offsets, so edits/reverts never dangle highlights; soft-delete/archive so pointers always resolve; the test must assert resolution to a target, not a non-null string.
11. **RLS on ALL child tables** (comments, versions, highlights, tasks, audit) + restricted application role + `FORCE ROW LEVEL SECURITY` + tests run as that role (no superuser bypass, no silent owner bypass).
12. **Bonus sequencing:** self-learning + data decay (scored, 10 pts) are harvested before ambient voice capture (unscored stretch, cut-first). **Final 6 h reserved** exclusively for README/brief/ATTRIBUTION/demo with continuous drafting and screen-recording from Day 1.

---

## 4. Tensions Between Recommendations and Resolutions

| # | Tension | Resolution |
|---|---|---|
| T1 | **Frontend: Next.js** (Phased plan, REQUIREMENTS) **vs Vite+React PWA SPA** (Architect). | **Vite + React + Tailwind PWA SPA.** The product is a WS-heavy realtime collaborative editor; CSR is the natural fit, SSR adds deploy/dev overhead with zero demo value, and a PWA is required for mobile voice capture anyway. Next.js reserved only if a static-render need emerges (it won't in 72 h). |
| T2 | **Version storage: "full snapshot + stored char-level delta"** (Schema) **vs "full snapshot, diffs computed on demand"** (Phased). | **Full snapshot per version is the source of truth.** `delta_from_prev` is an optional precomputed optimization column; if it costs schedule time it can be dropped because the diff view is always computed from two snapshots. Lowest-stakes divergence. |
| T3 | **Concurrency test strategy:** advisory locks + optimistic locking (Schema) **vs deterministic sequential simulation** (Phased/Requirements). | **Implement** with row-level optimism + `pg_advisory_xact_lock(patient, section)` + deterministic tie-break (role precedence → version → id). **Test** with deterministic interleaving (sequential simulation) — not racy threads — so the required micro-test never flakes. |
| T4 | **Encryption at rest scope:** Security demands app-layer AES-256-GCM envelope per patient; other specialists omit it. | The brief requires encryption at rest, so it is in scope but **bounded**: TLS in transit; AES-256-GCM envelope only on PHI-bearing body columns with a `key_version` column; structural columns (clinic_id, patient_id, author, timestamps) stay plaintext so RLS predicates and the P95 read path still work. Document honestly as demo-grade key management (gitignored `.env`, envelope key versioning in DB, KMS upgrade path). |
| T5 | **Transcription: hosted Whisper quality (REQUIREMENTS) vs local faster-whisper (Architect).** | **Local faster-whisper (base/small multilingual)** — raw audio must never leave the perimeter; redact-before-LLM holds by construction. Hosted-API and diarization (pyannote) upgrade paths documented; diarization optional with LLM-assisted speaker attribution fallback. |
| T6 | **Glance recompute: synchronous recompute-on-write adds write latency (Architect) vs invalidate+recompute in same txn (Schema).** | **Recompute synchronously inside the same transaction** as the mutation via a SECURITY DEFINER scoring function (idempotent, cache can never serve content RLS would deny). If bursty write volume appears, add a 250 ms debounce/batch window; the P95 constraint is on the **read** path, which stays a single PK read. |
| T7 | **Voice capture prioritization:** REQUIREMENTS tiers it as P1 equal to the other bonuses (Requirements-analysis) — the Scoring line does NOT include it. | **Voice capture is unscored extra credit; cut-first.** The 10-pt Bonus bucket needs only self-learning + data decay. Ship simplified self-learning + decay first (M6); voice is a stretch only after the 20 core points and 10 bonus points are green. |
| T8 | **AI-scribe provenance timing:** when to stamp provenance. | Stamp `provenance_pointer` to the source session/segment **before** the LLM call, in the ingestion pipeline, alongside `author_role=system` and the 3 `ai_*` entry types — not after. |
| T9 | **P95 measurement in CI:** timing assertions are flaky in CI (Schema/Security vs Phased). | P95 ≤ 300 ms is a **warm-path local test with documented methodology** (N≥200 warm requests) + a locust script whose output is pasted into the technical brief. The hard CI gate is **functional correctness**; the P95 number is measured and documented, not a flaky CI assertion. |
| T10 | **Real-time architecture:** in-process asyncio fan-out (Architect) vs Redis upgrade (REQUIREMENTS implicit multi-device). | **In-process asyncio pub/sub for the single-node demo** behind a small broadcaster interface; Redis pub/sub (channel per note_id) documented as the scale-up path. Multi-device sync is satisfied by the server-total-order protocol regardless of fan-out transport. |

---

## 5. Top 5 Overall Risks (with Mitigations)

1. **Time overrun — docs + demo video + brief production eat the build window.** Only ~60–64 h remain and deliverables are inside that window.
   *Mitigation:* freeze scope the moment the 20 core points are greenlit; M7 is a hard 6 h reserved block with nothing new built inside; continuous drafting + screen-recording from Day 1; explicit cut order (§3.3 below).
2. **Glance P95 > 300 ms** — Top Card computed on the read path (LLM 1–10 s) or via N+1 queries.
   *Mitigation:* precompute `patient_glance` on every write (entry/task/highlight/comment change); serve one indexed JSON endpoint; LLM only in ingestion/write paths; perf smoke test on Day 1 (M2); publish the measurement methodology (locust, N≥200 warm, P95) in the brief.
3. **Server-side RBAC silently bypassed** — RLS absent on child tables, app connects as superuser/owner, or tests run as the table owner.
   *Mitigation:* restricted DB role everywhere; `FORCE ROW LEVEL SECURITY` on every table including comments/versions/highlights/tasks/audit; micro-test must call **server APIs** (not UI) as each role and expect 403; cross-clinic reads assert 404/empty in CI.
4. **Concurrency micro-test flaky or fails on whole-document last-write-wins.**
   *Mitigation:* section-level optimistic locking with deterministic tie-break (role precedence then version); deterministic sequential interleaving in the test (no racy threads); losing version always preserved with `conflict_flag`; clinician-over-AI precedence.
5. **Provenance pointers dangle after revert/delete or shift after edits.**
   *Mitigation:* stable segment IDs + anchor text (not raw offsets); soft-delete/archive so pointers always resolve; resolver returns the target span or an explicit tombstone; `test_highlight_provenance.py` asserts resolution **returns the target**, not merely a non-null string.

### 5.1 Cut Order (descope sequence, applied only if behind)
1. Ambient voice capture (unscored extra credit) — cut first.
2. Data-decay polish (partial demo of archive + stubs is sufficient for the scored bonus).
3. Self-learning depth (simplified persisted weights + heuristic scoring is enough for the conceptual test + Scenario B).
4. Mention notifications / assignment notifications (keep the entities; drop push).
5. Delta-from-prev version optimization (diff-on-demand from snapshots suffices).

---

## 6. Immediate Next Actions (next 24–48 h)

1. **Scaffold + schema freeze (M0):** `mkdir` the monorepo layout; scaffold FastAPI + Vite/React/Tailwind + docker-compose (API, Postgres, mock LLM, SPA); write the full DDL (13+4 tables, `clinic_id`/`author_role`/`entry_type`/`provenance_pointer`/`entry_version` up front); commit the frozen schema; start ATTRIBUTION.txt and append entries as dependencies are added.
2. **Stand up the restricted DB role + RLS (M1):** `FORCE ROW LEVEL SECURITY` on every table incl. child tables; `SET LOCAL` GUCs from verified JWT inside each transaction; mock LLM + redaction gateway in place before any feature.
3. **Build the seed generator:** one command reproduces Scenarios A/B/C with fixed dates 2025-04-15 and 2026-02-06 and all 3 AI-scribed types; fixture Postgres for the 5 micro-tests.
4. **Ship the Care Note vertical slice (M2, end of Day 1):** timeline + precomputed Glance Top Card + AI-scribe ingestion with redaction + open tasks; run the locust P95 smoke test and record the number.
5. **Test-first on the slice:** land `test_rbac_scope.py` and `test_concurrent_edits.py` early; keep the other 3 micro-tests attached to their features (M3/M4/M6).
6. **Start screen-recording and continuous drafting now** (README / brief skeleton), not at the end; book the final 6 h deliverable block explicitly.
7. **Calendar the H48 gate** (~48 h from start): re-read the brief for the announced hint; 30–60 min decision gate; re-verify the cut order before hardening.
8. **Run the OMC delegation loop:** backend, frontend, and docs as three parallel tracks against the fixed API contract; delegate heavy code to `executor` agents and use `code-reviewer`/`verifier` passes per the execution protocol — never self-approve the final state.

---

*Sources: REQUIREMENTS_EN.md breakdown, Phased Implementation Plan, System Architecture Design, PostgreSQL Schema & Data-Layer Design, Security & RBAC Design — synthesized 2026-08-26.*
