# Nightingale — Technical Brief

A shared, longitudinal patient **Care Note** for multi-clinic care teams: a
glanceable Top Card, AI-scribed notes with deterministic No-PHI redaction,
role-segregated collaboration, and span-stable risk highlights with a
self-learning importance loop. Built in a 72-hour window.

## 1. Problem

Clinical context lives in many disconnected places — EMR entries, referral
letters, follow-up tasks, care-plan edits. Care teams waste time re-reading and
miss changes. Nightingale is a single longitudinal note per patient, built for
collaboration: **who** wrote **what** is always visible, **nobody** sees what
they must not, and the highest-signal items surface instantly.

## 2. Architecture (three layers)

**Stack:** FastAPI (Python) · PostgreSQL 16 · React + Tailwind PWA · Docker
Compose. No ORM object graph on the write path — all SQL is raw, parameterized
`text()` so the RLS policies are the single source of truth.

```
┌────────────┐   JWT (role+clinic)    ┌──────────────┐   raw text() SQL   ┌────────────────────┐
│ React PWA  │ ─────────────────────► │  FastAPI     │ ─────────────────► │ PostgreSQL 16      │
│ Top Card / │   get_current_actor →  │  (routes)    │   app_nightingale │  FORCE RLS          │
│ timeline / │   SET LOCAL ROLE/GUCs  │  + mock LLM  │   (restricted)    │  role-class policies│
│ highlights │                        └──────────────┘   + redaction     │  SECURITY DEFINER   │
└────────────┘                                                           │  functions          │
                                                                         └────────────────────┘
```

**AuthN:** JWT issued by a dev-only login route (prod path is OIDC). The token
carries `role` + `clinic_id`; `get_current_actor` sets the PostgreSQL session
GUCs (`app.user_id`, `app.role`, `app.clinic_id`) and `SET LOCAL ROLE` to the
matching role class.

## 3. Security model (RLS-first)

Every tenant read/write is enforced by PostgreSQL Row-Level Security, not just
by application guards:

- The API connects as `app_nightingale` — a `NOINHERIT` member of
  `patient_role` / `staff_role` / `clinician_role` / `admin_role` /
  `system_pipeline` with **no table privileges of its own**. Per request it
  `SET LOCAL ROLE` to one class; `FORCE ROW LEVEL SECURITY` means even a policy
  mistake cannot leak a row.
- **Write isolation:** staff policies match only staff-authored entries;
  clinician policies only clinician-authored; cross-section writes match zero
  rows.
- **Cross-clinic:** every policy is `clinic_id = app_clinic_id()`. Out-of-scope
  ids resolve to 404 (no existence disclosure).
- **Patient view:** patients see only `patient_visible` notes and their own
  comments — never internal staff/clinician notes, raw AI notes, tasks, or
  highlights.
- **Pipeline role:** the AI-scribe and highlight-generator flows use a
  `system_pipeline` role that can INSERT provenance/notes/highlights but has
  no SELECT on `entry` — a generated artifact can never be read back by the
  pipeline itself.
- **SECURITY DEFINER functions** (`save_entry`, `log_audit`,
  `record_interaction`, `decay_old_entries`, …) are the only way to mutate
  shared state, and their `EXECUTE` grants are role-scoped.

## 4. No-PHI redaction

The only LLM boundary is `llm_gateway.chat()`, and every message is redacted
before the model sees it — **deterministically and locally** (regex + lexicon,
never an LLM). Names, NRICs/FINs, ID numbers, and phones become stable
`[REDACTED:<TYPE>:<N>]` tokens; a per-category `redaction_mask` (counts only,
never the PHI) is persisted. The same `scrub()` is wired into the logging
filter, so even log lines cannot leak PHI.

## 5. Core features

- **Top Card (P95 ≤ 300 ms):** a precomputed glance — recency + risk score +
  entity importance (allergy boost) + open-action bonus + learned boost — so
  the clinician gets the signal at a glance.
- **Care Note timeline:** role-scoped entries with `author_role`, risk level,
  and provenance. AI Scribe ingests a raw transcript into a system-authored
  entry with a resolvable provenance pointer.
- **Collaboration:** threaded comments with `@mention` notifications and unread
  badges, tasks with assignee/priority/status/due, revision history with
  version restore and deterministic conflict handling (a same-section race
  keeps the loser verbatim as a flagged child version).
- **Risk highlights:** a deterministic generator ranks risk phrases; every
  highlight freezes its `quoted_text` at `(entry_id, offset_start, offset_end)`
  so the span always resolves — even after an edit, the UI re-locates by the
  frozen text.
- **Self-learning importance:** accepting/rejecting a highlight feeds the
  entry's extracted entities into `learning_weight`; a suggestions endpoint and
  the Top Card rank by learned importance, so the system improves at proposing
  what matters to this clinic.
- **Data decay:** stale, open-work-free entries archive to `entry_archive`
  (admin-triggered) and restore on demand — the note stays longitudinal without
  accumulating dead weight.

## 6. Testing & verification

- **Micro-test suite** (pytest, isolated fixture PostgreSQL): RBAC scope,
  revision history, concurrent-edit conflict resolution, highlight provenance,
  self-learning importance, plus an M2-M6 integration layer.
- **Baseline:** M2 = 15 passed / 3 xfailed (the xfailed = highlight provenance
  + self-learning, flipped green in M4/M6). **Final (M6): 111 passed / 0
  xfailed / 0 failed** — including the verifier-driven clinic-scoped decay and
  restore→re-decay idempotency regressions.
- **Adversarial verification per milestone:** a fresh-perspective verifier
  re-checks RLS isolation, cross-clinic 404s, admin `role_arg` handling,
  append-only audit, and redaction completeness.

## 7. Team & process

Multi-agent orchestration (Claude Code): planning (analyst/planner/architect/
database-reviewer/security-reviewer) → development (executor/tdd-guide/
reviewers) → verification (test-engineer/verifier/security-reviewer). Milestones
M0-M7 were executed overnight on a feature branch and merged to `main` with a
per-milestone commit + independent verification.
