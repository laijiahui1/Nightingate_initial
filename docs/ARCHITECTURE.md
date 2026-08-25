# Nightingale 72-Hour Build — System Architecture Design

> Author: Architect (planning team)
> Source of truth: `/Users/apple/Nightingate_initial/2026 72 Hour Build_ Nightingale Candidate Brief 2.md`
> Requirements breakdowns: `/Users/apple/Nightingate_initial/REQUIREMENTS.md` (CN), `/Users/apple/Nightingate_initial/REQUIREMENTS_EN.md` (EN)
> Status: Greenfield (repo contains only briefs/requirements — no code scaffolding yet).

---

## 0. First-Principles Framing

The product brief is explicit: *"We trust LLMs but up to a point and then we need reassurance from clinicians/staff."* This is the load-bearing insight. The architecture is therefore shaped around four principles that cascade into every decision below:

1. **Trust = traceability + determinism + human review.** Every piece of AI content carries a `provenance_pointer` to a source entry/span/session. Every mutation is versioned, authored, and audited. Every conflict has a *deterministic* resolution rule defined in advance. Every highlight has a `risk_reason` and can be accepted/rejected quickly.
2. **Performance is a data-shape problem, not a hardware problem.** The Glance Top Card must load in P95 ≤ 300 ms *warm*. You cannot scan the timeline on the hot path. You pre-aggregate into a single row and read one primary key.
3. **Security boundaries live at the database and the LLM boundary, never at the UI.** RBAC is enforced by PostgreSQL Row-Level Security + backend middleware. PHI redaction is an invariant enforced *at the LLM call site* by a gateway, so no future code path can accidentally leak raw text to the model.
4. **72 hours rewards clarity, not novelty.** Where a simpler mechanism provably meets the constraint, choose it. OT/CRDT libraries, SSR frameworks, hosted audio transcription, and Redis-based fan-out all add complexity without buying the required outcome at demo scale. Each is rejected or demoted with a stated reason.

Scoring weights drive build priority: Glanceability (6) > Collaboration+AI (5) > Provenance (4) > Security (3) > Communication (2) > Bonus (10). The architecture front-loads the top-scoring, hardest-to-change items (data model + RLS, Glance, real-time) and keeps bonus features (voice, self-learning, data decay) as additive, isolated modules.

---

## 1. Component Architecture

Text-based diagram of the full system:

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                              PWA CLIENT  (React SPA)                                │
│                                                                                    │
│   Care Note page (per patient):  Glance Top Card │ Longitudinal Timeline │          │
│   Inline threaded comments + mentions + tasks │ Revision history / diff panel       │
│   Voice capture views:  [Patient view]  [Clinical view]  (MediaRecorder)            │
│   Service worker: installable PWA, upload queue + retry for voice                   │
└───────┬─────────────────────────────┬───────────────────────────────┬──────────────┘
        │ HTTPS  REST  /api/*         │ WSS  /ws/notes/{note_id}       │ /api/voice/* (multipart)
        ▼                             ▼                               ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│                          FASTAPI BACKEND  (uvicorn, asyncio)                        │
│                                                                                    │
│  [Auth + RBAC middleware]  (JWT → SET LOCAL app.role → per-request role)            │
│  [REST routers]           entries, sections, comments, highlights, revisions,       │
│                           tasks, notifications, patients, glance                    │
│  [WS broadcaster]         per-note-room pub/sub (in-process), authoritative apply   │
│  [Scribe orchestrator]    per-note-type ingestion pipeline                           │
│  [Glance pre-aggregator]  importance scoring → JSONB snapshot, recompute-on-write   │
│  [RedactionService]       regex/NER PHI → placeholders → de-redact (deterministic)  │
│  [LLM Gateway]            ONLY path to any LLM; enforces redaction invariant        │
└───┬───────────┬──────────────┬─────────────────┬──────────────────┬───────────────┘
    │           │              │                 │                  │
    ▼           ▼              ▼                 ▼                  ▼
┌──────────┐ ┌──────────────┐ ┌───────────────┐ ┌──────────────┐ ┌───────────────────┐
│PostgreSQL│ │   LLM Gateway │ │ Whisper/Diari.│ │ Local object │ │ In-process task /  │
│  16 + RLS│ │ (OpenAI/Claude│ │ faster-whisper│ │ store (audio, │ │ notification queue │
│ + JSONB  │ │  → de-redact) │ │  + pyannote*) │ │ transcripts) │ │ (Redis upgrade)   │
│ snapshot │ │              │ │               │ │              │ │                   │
└──────────┘ └──────────────┘ └───────────────┘ └──────────────┘ └───────────────────┘
               * pyannote diarization optional (bonus); graceful fallback
```

**Components and their responsibilities**

| Component | Responsibility | Notes |
|---|---|---|
| PWA Client (React) | Care Note page: Glance, Timeline, comments, revisions; voice capture; PWA installability | Client-rendered; state synced via WS; optimistic section edits with `base_revision` |
| FastAPI Backend | All REST + WS; auth/RBAC middleware; scribe orchestration; glance aggregation; redaction + LLM gateway | Single async process serves HTTP + WS together; CPU-bound redaction/transcription dispatched to thread pool or worker queue |
| PostgreSQL 16 | Source of truth; RLS-enforced RBAC; JSONB flexible entry payloads; glance snapshot cache; audit log (metadata only) | Also serves as the durable cache layer |
| Real-time/WS layer | Authoritative apply of section ops; ordered broadcast; presence | Custom LWW protocol (§5), in-process pub/sub |
| LLM Gateway | Sole egress to any LLM; enforced redaction-in / de-redaction-out; prompt templates; JSON mode | Model-agnostic; swap OpenAI/Claude via env |
| Voice/Transcription pipeline | faster-whisper (local) transcribe; optional diarization; timestamps/confidence; code-switching (multilingual model) | Local so raw audio never reaches an external model |
| Object store | Audio + transcript blobs | Filesystem for demo behind a repository interface (S3 later) |
| Task/notification queue | Mentions/assignments → inbox items + optional WS push | In-process asyncio for demo |

---

## 2. Stack Validation & Adjustments (72-hour lens)

The suggested stack (Python/FastAPI + PostgreSQL + React/Next.js + WebSocket + Whisper + any LLM) is validated with **two adjustments**: frontend moved from Next.js to a Vite SPA, and the real-time layer changed from generic "WebSocket" to a specific custom LWW protocol (a decision, not a new dependency).

| Layer | Suggested | Decision | Rationale (first-principles) |
|---|---|---|---|
| Backend | Python / FastAPI | **Adopt** | All five mandated test filenames are `.py` — tests will be Python. FastAPI is async-native (WebSocket + REST in one process), Pydantic validation, auto OpenAPI docs, fastest iteration. Sync CPU-bound work (redaction, transcription) goes through `run_in_threadpool`/worker so the event loop stays responsive. |
| Database | PostgreSQL | **Adopt** | Native Row-Level Security is the direct, server-side implementation of the RBAC hard constraint (brief: "must be enforced server-side, e.g. via RLS"). JSONB + GIN covers flexible AI note payloads and entity search. Doubles as the glance cache. |
| Frontend | React / Next.js | **Adjust → Vite + React + Tailwind PWA** | The product is a real-time collaborative editor. Client-side rendering is the natural fit; SSR buys nothing for a WebSocket-driven SPA and adds a second server, build complexity, and cold-start risk. Vite gives a static bundle the backend can serve, plus a PWA manifest + service worker for mobile voice capture. (Next.js remains acceptable if the implementer is strictly faster in it; note the trade-off explicitly in the brief.) |
| Real-time | WebSocket | **Adopt with a specific protocol** | WebSocket is the transport; the *protocol* is custom section-level server-total-order LWW (§5). Google-Docs-grade OT/CRDT is rejected as overkill for section granularity and for the explicit deterministic-strategy test requirement. |
| Transcription | Whisper family | **Adopt: faster-whisper (local)** | `faster-whisper` (CTranslate2) is ~4× faster than openai-whisper and runs on CPU — fits the demo. Multilingual models handle code-switching. Crucially, local transcription means **raw audio never leaves the perimeter**, which makes "redact before LLM" satisfiable by construction. Diarization via pyannote is optional bonus with graceful fallback. |
| LLM | Any | **OpenAI gpt-4o-mini or Claude via gateway** | Cheap, fast, good at summarization + entity extraction + JSON structured output. Model-agnostic gateway means either works. All calls pass the redaction invariant. |

**Deliberate non-choices** (each with a reason):
- *Redis*: not needed for a single-node demo; in-process WS fan-out + Postgres snapshot suffice. Interface it behind a broadcaster/cache abstraction so it is a config change, not a rewrite, if we multi-node.
- *S3*: local filesystem behind a `BlobStore` interface.
- *yjs/Yjs CRDT*: black-box convergence is harder to explain and test than our own monotonic-revision LWW (§5).
- *Django/Node*: FastAPI's async + Pydantic fits the async/WS + validation profile better for this scope.
- *Hosted transcription API*: would require shipping raw audio to a third party — conflicts with the PHI discipline we want to demonstrate.

---

## 3. Data Flow — Three AI-Scribed Note Types

All three types share a **pipeline** (see §6 for redaction placement): `source → transcribe/serialize → REDACT → LLM → DE-REDACT → persist as Entry(author_role=system) → WS broadcast → glance re-aggregation`.

### 3.1 `ai_doctor_consult_summary` (post doctor–patient consult)
```
1. Consult audio captured via clinical view (PWA, laptop/mobile) OR a manual consult event.
2. Transcription: faster-whisper → text with segments/timestamps (+ optional diarization for speaker labels).
3. REDACT: RedactionService replaces names / IC / phone with placeholders (transcript text only).
4. LLM (gateway): produce structured summary — SOAP-style sections, follow-ups, risk items,
   clinical entities (meds, chief complaint, allergies), open actions.
5. DE-REDACT: placeholders mapped back to real values deterministically (per-call mapping table).
6. Persist: Entry{ author_role=system, type='ai_doctor_consult_summary',
   provenance_pointer→transcript_id + segment offsets } as version 1. (Scribe orchestrator.)
7. WS broadcast to note room → timeline updates live for authorized roles.
8. Glance pre-aggregator recompute (synchronous) → new Top Card snapshot.
9. (Optional) highlight suggestions generated via the same redacted pipeline.
```

### 3.2 `ai_nurse_consult_summary` (post nurse–patient consult)
Identical pipeline to 3.1, differing only in `type='ai_nurse_consult_summary'` and provenance → nurse consult transcript. The nurse's summary is a `staff`-adjacent stream but authored by `system`; RBAC (staff can view/add staff notes; clinician views all AI notes; patient cannot view raw AI notes) applies at the entry level.

### 3.3 `ai_patient_session_summary` (pre/post AI–patient session)
```
1. Patient interacts with the AI assistant (pre-consult questions, post-consult follow-ups).
   Session messages stored with a session_id.
2. Serialize session transcript → REDACT → LLM summary + "questions the patient asked"
   + structured facts → DE-REDACT.
3. Persist: Entry{ author_role=system, type='ai_patient_session_summary',
   provenance_pointer→session_id } .
4. WS broadcast + glance recompute.
5. A patient-facing summary (de-identified, simpler language) is generated and linked —
   this is the ONLY form the patient can see (RBAC: patient cannot view raw AI-scribed notes).
```
Patient-contributed insight (concerns, preferences) extracted by the LLM lands in a dedicated patient-insight entry, also `type=ai_patient_session_summary`-linked with full provenance.

**Common invariants for all three:** `author_role='system'`, always distinguishable visually in the timeline (per requirement), and every entry carries `provenance_pointer` so a highlight click jumps to the exact source entry/span (§7).

---

## 4. Data Flow — Both Voice Capture Paths

### 4.1 Patient Voice Capture (patient view only — PWA on mobile)
```
1. PWA patient view: MediaRecorder records the consult conversation locally (mobile).
2. Upload → POST /api/voice/patient (TLS; service worker queues + retries on flaky mobile network).
3. Backend: local faster-whisper transcription. (Audio stays local/in-process — never sent to an LLM.)
4. REDACT transcript (names / IC / phones) BEFORE any LLM call.
5. LLM: identify structured facts (symptoms, concerns, questions, requests) + generate a
   patient consult session summary in plain, patient-safe language.
6. DE-REDACT (patient's own data is restored for the internal record).
7. Persist internal Entry(type=ai_patient_session_summary, provenance→audio id + segments).
8. Persist patient-facing summary entry (only what the patient is allowed to see).
9. WS broadcast + glance recompute.
```
**Ordering note:** the brief's phrase "redact PHI before LLM processing, transcribe the recording" — transcription is local, so the redaction gate applies to the *transcript text* that flows to the LLM. Nothing textual (or audio) reaches an external model unređacted.

### 4.2 Clinical / Staff Voice Capture (clinical view only — PWA mobile or laptop)
```
1. Clinical view: record a clinician–patient or nurse–patient consult.
2. Upload → POST /api/voice/clinical.
3. Local transcription: faster-whisper multilingual (code-switching support) → timestamps,
   confidence markers per segment; optional pyannote diarization → speaker labels.
   (Bonus: VAD for noisy environments; overlap handling documented as a known limitation.)
4. REDACT transcript before LLM.
5. LLM: clinical summary + structured facts + entity extraction, each fact carrying provenance
   back to source segments (quote + [t0.00–t0.12]).
6. DE-REDACT → persist Entry(ai_doctor_consult_summary | ai_nurse_consult_summary,
   provenance→segment ranges). Author role = system.
7. WS broadcast + glance recompute.
```
Multi-device capture (bonus) = same note room accepts audio from multiple PWA clients; segments are merged in transcript order with device ids in metadata.

---

## 5. Real-Time Collaboration Design

### 5.1 Concurrency unit: the **section**
Every note/entry is composed of **sections** (e.g., SOAP blocks — Subjective/Objective/Assessment/Plan/Follow-ups, or free blocks for manual notes). The section is the concurrency unit because it matches the domain (RBAC says staff writes staff sections, clinician writes clinician sections) and the test (`two roles editing different sections concurrently do not overwrite each other's changes`).

Section row: `section_id | note_id | content | revision (int, server-assigned) | base_revision | updated_at | last_author_role | last_author_id`.

### 5.2 Protocol: server-total-order Last-Write-Wins + version + conflict flag
- Client sends an edit op: `{note_id, section_id, base_revision, content, client_op_id}` (WS or REST).
- Server applies **only if `base_revision == section.revision`** (clean apply → increment revision → broadcast full section content + new revision).
- **Different sections** → both edits apply independently. This is what makes concurrent cross-role edits non-destructive.
- **Same section, stale `base_revision`** → conflict. Deterministic resolution (rule, not judgment):
  1. **Later server revision wins.** Since the server assigns a strictly monotonic per-section revision at apply time, "which op is later" is unambiguous — the server total order is the arbiter. No wall-clock ties are possible.
  2. The **losing content is never deleted**: it is preserved as a new version in `versions`, and the section is marked `conflict_flag=true` with `superseded_revision` so the UI can show "resolved by last-write-wins — see diff."
  3. **Cross-stream precedence** (clinician vs AI/patient memory, per brief): a clinician edit over an AI-scribed or patient-memory entry creates a **fork** — a new section/version authored by `clinician` with a `supersedes_provenance` link to the AI entry, *and* a `conflict_flag` for review. Both brief-mandated paths (clinician precedence AND flag-for-review) are realized simultaneously.
- **Convergence argument (why this is safe):** every accepted op carries the section's new monotonic revision; clients apply ops in revision order, so all clients converge to identical state. No OT transforms, no CRDT merge semantics to debug.
- **Presence/cursors:** optional; low priority for 72h.

### 5.3 Comparison & why not the alternatives

| Approach | Verdict | Reason |
|---|---|---|
| **OT** | Rejected | Full text-transform algebra is complex, bug-prone, and overkill at section granularity. 72h budget. |
| **CRDT (yjs)** | Rejected (documented) | Deterministic but *black-box*; harder to write the required deterministic-strategy test and brief explanation against a library than against our own version rule. Adds a runtime + sync state. |
| **Lock (mutex per note)** | Rejected | Blocks collaboration, poor UX; even then you'd need section-level locks, i.e., our granularity minus the merge. |
| **Section-level server-total-order LWW + version + flag** | **Chosen** | Deterministic, testable in `test_concurrent_edits.py`, no dependencies, matches the domain model, and satisfies both conflict paths the brief demands. |

**Trade-off (honest):** two users editing the *same paragraph region* in the same section collapse to per-op LWW rather than character-level merge. Mitigations: sections are small blocks; RBAC naturally separates roles into different sections; every superseded version is retained for revert/audit.

---

## 6. PHI Redaction — Placement in Every Stream

**Design invariant: the LLM Gateway is the *only* egress to any LLM, and it refuses to execute without going through the RedactionService.** A decorator/middleware enforces `redact → call → de-redact`, so no future code path can call the model with raw PHI.

Redaction mechanics:
- **Redactor**: regex rules for the mandated categories (names, IC/ID numbers, phones) + optional NER pass for robustness, over synthetic data.
- **Placeholder scheme**: `[REDACTED:<type>:<n>]` mapped to a per-call mapping table → deterministic, reversible de-redaction restores the exact original text after the LLM responds. Only de-redacted text is ever persisted.

Placement per stream (all "before the LLM"):

| Stream | Where redaction sits |
|---|---|
| AI-patient session summary (§3.3) | Session transcript text → REDACT → LLM → DE-REDACT → persist. |
| Doctor/nurse consult summaries (§3.1/3.2) | Transcript text → REDACT → LLM → DE-REDACT → persist. Audio never sent to any model (local Whisper). |
| Patient voice capture (§4.1) | Local transcription → REDACT transcript → LLM (facts + patient summary) → DE-REDACT. |
| Clinical voice capture (§4.2) | Local transcription + diarization → REDACT transcript → LLM → DE-REDACT. |
| Highlight suggestion generation | Candidate text from entries/highlights → REDACT → LLM → DE-REDACT. |
| Entity extraction (meds, allergies, chief complaint) | Runs inside the same redacted gateway call. |
| **Logs ("clean logs")** | A `sanitize()` layer scrubs PHI from every emitted log; audit log stores **metadata only** (who/what/when, not content) per the required test. Raw note content is never logged. |

De-redaction subtlety: the LLM may reformat or move a placeholder; the de-redactor restores by placeholder token, and any unrecognized placeholder is flagged rather than silently kept — provenance of restoration is testable (`test_highlight_provenance.py`).

---

## 7. Glance / Top Card — Pre-Aggregation & Caching (P95 ≤ 300 ms warm)

### 7.1 Why a cache is mandatory
The Top Card surfaces highlights, open actions, and risk flags computed by the importance engine across *all* of a patient's entries. Computing that on the read path (scan + score + sort) violates 300 ms by definition. Pre-aggregate.

### 7.2 Design: one JSONB snapshot row per patient
```
glance_cache (
  patient_id   PK,
  data_version bigint,     -- increments on every relevant write
  payload      jsonb,      -- { highlights[], open_actions[], risk_flags[], updated_at }
  updated_at   timestamptz
)
```
- **Warm read path** `GET /api/patients/{id}/glance`: auth (RLS filter by clinic/role) → one indexed PK read → JSON → return. No joins, no scoring, no scans. Budget: PK lookup ~2–5 ms + JSON serialize ~1–5 ms + framework/auth ~10 ms → comfortably < 300 ms, dominated by network.
- **Writes trigger recompute**: the Glance Pre-Aggregator recomputes **synchronously after commit** (with a small 250 ms debounce window to batch bursts). Because writes are low-frequency in a demo, synchronous recompute keeps the cache *always fresh*, which is what makes the warm measurement meaningful and simple. The snapshot is written in the same transaction boundary (or immediately after commit) so readers never see a stale Top Card.
- **Importance scoring** (deterministic, reproducible): `score = w_recency * exp(-λ·age) + w_risk * risk_level + w_entity * clinical_entity_bonus + w_task * unresolved_action_bonus + self_learning_weight·(engagement_signal)`. Weights live in `learning_weights` (persisted, updated by the self-learning module — bonus). Ordering is stable because all inputs are stored fields.
- **Payload is small** (5–10 highlights + a handful of actions + flags) — tiny for "readable and actionable in under 10 seconds."
- **Multi-node upgrade**: swap the snapshot store to Redis with the same key shape; the interface is already a cache abstraction.

### 7.3 Measurement approach (stated, per brief)
1. **Functional/CI**: `test_glance_p95.py` — seed a patient, warm the cache, then issue 200 sequential `GET /patients/{id}/glance` calls via `httpx`, record `time.perf_counter()` per call, assert `p95 ≤ 300 ms` (with an env-toggle to skip in constrained CI).
2. **Load test for the brief**: `locust` file — 50 concurrent users hammering the warm glance endpoint for 60 s; capture the p95 from the HTML/JSON report and paste it into the technical brief with the exact command (`locust -f load/glance_locust.py --headless -u 50 -r 10 -t 60s`).
3. **Decomposition budget** (documented): TLS/network ≤ 50 ms (local loopback ~1 ms), auth+RLS ~5 ms, PK read ~3 ms, JSON ~2 ms, framework ~5 ms. Even with the worst local-TLS leg this is far under 300 ms; the margin is what absorbs demo machine variance.
4. **What "warm" means**: snapshot present and fresh (data_version matches). Cold path (first load after seed) recomputes once and is explicitly out of scope for the P95 claim.

---

## 8. Where the PHI Redaction Pipeline Sits (summary table) — see §6 (consolidated above to avoid duplication).

---

## 9. Deployment Topology (Demo + Tests)

**Demo (one command, reproducible):** `docker-compose up`
```
services:
  db:        postgres:16  →  init.sql (schema + RLS + seed); separate nightingale_db + test_db
  backend:   Dockerfile (uvicorn app.main:app --workers 1 --host 0.0.0.0 --port 8000)
             → serves FastAPI + static frontend bundle + /ws
  frontend:  (static build mounted into backend/nginx; optional separate nginx for TLS)
  tls:       caddy or nginx sidecar terminates TLS for the demo URL (self-signed local / Let's Encrypt if deployed)
```
- **Single worker** intentionally (keeps in-process WS fan-out correct). If >1 worker is needed, enable the Redis broadcaster.
- **TLS in transit** via the caddy/nginx reverse proxy; **encryption at rest** via Postgres volume encryption (documented) — synthetic data only, but the controls exist.
- **Environment** driven by `.env` (DB URL, LLM keys, model names, redaction config). Secrets validated at startup.

**Tests:** `pytest` against a **fixture Postgres** (real RLS — cannot be faked in SQLite). CI/demo runs spin the `test_db` schema, then:
- `test_rbac_scope.py` — staff vs clinician cross-role write denial; patient isolation from comments/raw AI notes; cross-clinic denial.
- `test_revision_history.py` — version increments, revert restores prior state, audit log shows metadata only.
- `test_highlight_provenance.py` — every highlight's `provenance_pointer` resolves to an entry/span.
- `test_concurrent_edits.py` — two roles editing different sections concurrently → both survive; same-section race → deterministic LWW + conflict flag + preserved loser.
- `test_self_learning_importance.py` — pin a highlight → similar content scores higher afterward (weights persisted).
Run instructions: `docker compose up -d db && pytest -q` (documented in README).

**Demo video:** run the stack locally, record the three scenarios (A: Glance + AI-scribe traceability; B: collaboration + audit + revert + learning; C: longitudinal history across dates + highlight logic + data-decay explanation).

---

## 10. Key Trade-offs & Decisions (explicit for the brief's Communication score)

| Decision | Trade-off | First-principles justification |
|---|---|---|
| Custom LWW vs yjs CRDT | No character-level merge in same-section region | Deterministic, explainable, testable; versions preserve everything; matches domain granularity |
| Vite SPA vs Next.js | No SSR/SEO | Product is a private, real-time editor; SSR adds cost, not value |
| Local faster-whisper vs hosted | Lower ceiling on transcription quality | Raw audio never leaves perimeter; redaction-before-LLM holds by construction |
| Sync recompute-on-write | Slightly slower writes | Demo write volume is low; keeps warm cache always fresh; P95 constraint is on reads |
| Postgres JSONB snapshot vs Redis | Slightly slower cache; no TTL | One durable source; single PK read already << 300 ms; Redis is a config change away |
| In-process WS fan-out vs Redis | Single-node only | One container demo; interface isolates the upgrade |
| Synchronous, deterministic everything | Less "magic" | Trust = determinism; reviewers can reason about every rule |
| Minimal JWT auth | Not production-grade auth | RBAC/RLS/redaction are the real security controls; auth is a thin token carrier for the demo |

**Assumptions:** demo is single-node on a dev machine or one small VM; synthetic data only; write volume is demo-scale; Postgres and the app are co-located for the warm-path measurement; LLM latency is excluded from the Glance read path (Glance reads the cache, never the LLM).

---

## 11. Data Model Map (for the 2–3 page brief)

```
clinic(id) ──< user(id, role, clinic_id)            -- role ∈ {patient, staff, clinician, admin}
patient(id, clinic_id, ...)
patient ──< note(id, patient_id, clinic_id)          -- one Care Note per patient
note ──< entry(id, note_id, author_role, author_id, ts, type, provenance_pointer, payload jsonb)
        type ∈ {ai_doctor_consult_summary, ai_nurse_consult_summary, ai_patient_session_summary,
                staff_note, clinician_note, patient_insight, system_event}
entry ──< section(id, entry_id, content, revision, base_revision, conflict_flag, supersedes_*)
entry ──< comment(id, entry_id, author_role, body, resolve_state, parent_id)
entry ──< version(id, entry_id, snapshot, author, ts)         -- full snapshots (simpler than diffs; see trade-off)
entry ──< highlight(id, entry_id, span, risk_reason, provenance_pointer, state{accepted|rejected|pending}, score)
highlight ──< provenance_link(target_type, target_id, span)    -- resolvable to entry/span/session
entry ──< task(id, assignee_role, state, due)
learning_weights(id, feature, weight) ; interaction_log(user, entry_id, action)  -- self-learning
audit_log(id, user, action, entity_id, ts)                    -- metadata only, never content
```
Version storage: **full snapshots** chosen over diffs — simpler, satisfies revert + "view changes since X" via snapshot-to-snapshot diff, and at demo scale storage is trivial. Trade-off stated: snapshot-per-edit is heavier than diffs; acceptable for 72h, upgrade to diff/delta later.

Self-learning integration: interaction (pin/edit/comment on an AI highlight) writes to `interaction_log` and bumps the matching `learning_weights.feature`; the Glance scorer reads those weights, so future similar content scores higher — persisted, deterministic, testable.

Data decay (bonus): `entries` gain a `tier` (hot/warm/archived). Older, low-engagement entries are summarized into compact rollup entries and flagged `decayed=true`; timeline can toggle "show archived." Rolling rule: age > N days AND no clinician engagement → eligible.

---

*Source files read: `/Users/apple/Nightingate_initial/2026 72 Hour Build_ Nightingale Candidate Brief 2.md`, `/Users/apple/Nightingate_initial/REQUIREMENTS.md`, `/Users/apple/Nightingate_initial/REQUIREMENTS_EN.md`.*
