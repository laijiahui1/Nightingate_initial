# Nightingale — Demo Script (English)

A guided walkthrough of the Care Note. Every interaction below is either a UI
flow or its exact `curl` equivalent. Run against a local stack
(`make up && make migrate && make seed`).

> Identifiers like `{patient_id}` are placeholders — substitute the real UUIDs
> from your session. All demo data is synthetic (backend/app/seed.py).

## 0. Stack & roles

| Role      | Email                 | What they see / may do                                    |
|-----------|-----------------------|-----------------------------------------------------------|
| Clinician | marcus.lee@meridian.demo | Full Care Note, writes own sections, accepts highlights |
| Staff     | priya.nair@meridian.demo | Full Care Note, writes own sections, comments, tasks     |
| Admin     | sandra.ho@meridian.demo  | Everything + audit log + decay control                   |
| Patient   | alice.tan@meridian.demo  | Only `patient_visible` content, no internal notes        |

## 1. Boot

```bash
make up && make migrate && make seed
open http://localhost:5173          # web UI (PWA)
```

Dev login (any role) — curl form:

```bash
curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"marcus.lee@meridian.demo"}'
# => { "access_token": "...", "role": "clinician", ... }
```

## 2. The Top Card (P95 ≤ 300 ms)

- Open Alice Tan. The Top Card (glance) summarizes the highest-signal items:
  recent entries, risk level, open actions, allergies — precomputed server-side.
- `GET /api/patients/{patient_id}/glance` returns the compact top-card payload
  (recency + risk + entity-importance + learned-boost scoring).

```bash
curl -s http://localhost:8000/api/patients/{patient_id}/glance -H "authorization: bearer $TOKEN"
```

## 3. The Care Note timeline

- Clinician & staff see the full longitudinal timeline: intake, AI-scribed
  consult summary, care plan, review, follow-ups — each with `author_role`,
  `risk_level`, and provenance.
- The patient sees only their own `patient_visible` notes (e.g. a discharge
  summary), never internal or raw AI notes.
- `GET /api/patients/{patient_id}` → page bundle `{entries, comments, tasks}`.

## 4. AI Scribe (redaction at the LLM boundary)

1. In the AI Scribe section, paste a raw transcript that mentions a name,
   NRIC, and phone — e.g. "Dr. Marcus Lee reviewed Alice Tan, NRIC S9123456D,
   call +65 9123 4567."
2. Submit. The redactor turns every PHI span into a deterministic
   `[REDACTED:<TYPE>:<N>]` token BEFORE the mock LLM sees it; the model's
   summary is ingested as a `system`-authored entry with provenance.
3. A `redaction_mask` (category counts only — never the PHI) is persisted with
   the note.

```bash
curl -s -X POST http://localhost:8000/api/patients/{patient_id}/ai-scribe \
  -H "authorization: bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"transcript":"Dr. Marcus Lee reviewed Alice Tan, NRIC S9123456D, call +65 9123 4567."}'
```

## 5. Collaboration

- **Comments & @mentions:** add a comment on an entry; use `@Marcus Lee` to
  mention a teammate. The mentions bell shows unread count; read marks them.
- **Tasks:** create and re-assign follow-ups (status, priority, assignee, due).
- **Revision history:** open an entry → *History* → see every version → restore
  any prior version. A same-section race is surfaced as a conflict (flagged
  child version, never silently dropped).

```bash
# comment + mention
curl -s -X POST http://localhost:8000/api/entries/{entry_id}/comments \
  -H "authorization: bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"body":"Please review BP trend. @Marcus Lee"}'
# unread mentions
curl -s http://localhost:8000/api/mentions/unread -H "authorization: bearer $TOKEN"
# history + restore
curl -s http://localhost:8000/api/entries/{entry_id}/history -H "authorization: bearer $TOKEN"
curl -s -X POST http://localhost:8000/api/entries/{entry_id}/revert \
  -H "authorization: bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"target_version":1}'
```

## 6. Risk highlights (span-stable provenance)

1. On the latest AI-scribed note, click **Generate highlights**. The generator
   ranks risk phrases and emits suggested highlights — the top un-highlighted
   one (e.g. **"BP 148/92"**, high) with a frozen `quoted_text` and a span that
   resolves exactly.
2. Suggested highlights show **Accept / Reject**. Accept → the span is marked
   and the audit trail records `highlight_accept`.
3. Every highlight resolves: `body[offset_start:offset_end] == quoted_text`.
   Even after an edit, the UI re-locates by the frozen text.

```bash
curl -s -X POST http://localhost:8000/api/patients/{patient_id}/highlights/generate \
  -H "authorization: bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"entry_id":"{entry_id}"}'
curl -s -X POST http://localhost:8000/api/patients/{patient_id}/highlights/{highlight_id}/accept \
  -H "authorization: bearer $TOKEN" -H 'content-type: application/json' -d '{}'
```

## 7. Self-learning importance (bonus)

- Accepting the amlodipine-related highlight teaches the system: the entry's
  extracted entities flow into `learning_weight` (+1 on accept).
- `GET /api/patients/{patient_id}/highlights/suggestions?q=amlodipine` now
  ranks amlodipine above unlearned entities (score > 0) — the Top Card's
  learned-boost uses the same weights.

```bash
curl -s "http://localhost:8000/api/patients/{patient_id}/highlights/suggestions?q=amlodipine" \
  -H "authorization: bearer $TOKEN"
```

## 8. Data decay (admin)

- Admin **Archive stale entries** runs `decay_old_entries(730)`: entries older
  than the threshold with no open work are archived to `entry_archive`,
  `is_decayed=true`, body emptied, and can be restored on demand. The decay is
  **clinic-scoped** — an admin at one clinic can never archive another
  clinic's entries, and re-decay after a restore is idempotent.

```bash
# admin-only: archive stale entries (default 730 days)
curl -s -X POST http://localhost:8000/api/admin/decay \
  -H "authorization: bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"days":730}'
# restore a decayed body on demand (staff / clinician / admin)
curl -s -X POST http://localhost:8000/api/entries/{entry_id}/restore \
  -H "authorization: bearer $TOKEN" -H 'content-type: application/json' -d '{}'
# => { "body": "<restored note body>" }
```

## 9. RBAC / security spot-checks

- Patient bundle exposes zero internal comments / raw AI notes / highlights.
- Cross-clinic ids resolve to **404** (no existence leak), never 403.
- Staff cannot edit a clinician entry (403); the DB RLS backstop matches zero
  rows independently.
- `audit_log` is append-only, metadata-only; admin-only reads.
- Every role-bound request runs as the restricted `app_nightingale` role under
  `FORCE ROW LEVEL SECURITY` — no superuser in the request path.

## 10. Close-out

```bash
make logs        # review API + web logs
make test        # backend suite: 0 failures / 0 xfail at M6
```
