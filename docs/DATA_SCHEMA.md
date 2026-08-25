# Nightingale 72-Hour Build — PostgreSQL Data-Layer Design

**Role:** Database Reviewer (PostgreSQL specialist)
**Source of truth:** candidate brief + REQUIREMENTS.md / REQUIREMENTS_EN.md (read in full).
**Design stance:** PostgreSQL 16 is the single source of truth for RBAC (native RLS), provenance, versioning, concurrency, and the persisted self-learning weights. The app layer adds middleware checks as defense-in-depth; RLS is the enforcement that counts for scoring ("server-side enforced; UI-only checks are insufficient").

---

## 0. Guiding decisions (first-principles)

1. **Tenancy is clinic-scoped.** Every patient-scoped table carries `clinic_id`; RLS filters on `app_clinic_id()` (a session GUC set from the verified JWT). No cross-clinic read is possible even if app code is buggy.
2. **Roles are concrete DB roles** (`patient_role`, `staff_role`, `clinician_role`, `admin_role`) plus a `system_pipeline` role for AI ingestion. Policies are written per role, so role semantics live in one place and are testable.
3. **Write isolation is structural.** A staff cannot edit a clinician note because (a) the row is owned by `author_role='clinician'` and (b) the section is owned by the clinician via `section_role_of()`. Both are checked in `WITH CHECK`.
4. **Versioning = full snapshots + stored deltas (hybrid).** "View changes since X" is a two-snapshot diff (O(1), exact, no replay); revert is a non-destructive new version.
5. **Concurrency = section-scoped rows + optimistic locks + advisory locks + deterministic tiebreak.** Losers are never silently dropped; they are preserved as flagged child versions.
6. **Every citable thing carries a provenance pointer.** `entry.provenance_id` and `highlight.provenance_id` are FKs to a `provenance` registry; highlights also store entry span offsets so a click resolves to an exact timeline span.
7. **Glance is precomputed, not computed live.** `patient_glance` is invalidated on mutation and rebuilt by a SECURITY DEFINER scorer; this is what makes P95 ≤ 300ms plausible on a warm path.
8. **PHI is synthetic and redacted before LLM.** `ai_scribed_note.redaction_confirmed` records that the pipeline ran; logs are metadata-only.

---

## 1. Entity / Relationship map

```
clinic 1 ── * users            (one user belongs to one clinic)
clinic 1 ── * patient
patient 1 ──1 users            (patient-facing login; user.role='patient')
patient 1 ── * entry           (the timeline)
entry    1──1 provenance      (provenance_pointer)
entry    1──1 ai_scribed_note (author_role='system', 1 of 3 types)
entry    1── * comment         (thread = entry_id + parent_id tree; resolve state)
entry    1── * entry_version   (hybrid snapshots + deltas)
entry    1── * highlight       (risk_reason + accept/reject + span + provenance)
entry    1── * entry_entity    (tagged clinical entities → risk + learning)
entry    1── * task            (assignment; "assign to staff")
comment 1── * mention          (@nurse / @clinician → notification queue)
entry/comment → learning_interaction → learning_weight (persisted learning)
entry    1──1 patient_glance   (precomputed top-card cache)
all writes → audit_log         (metadata only)
entry/entry_version → entry_archive / entry_version_archive (data decay)
```

**Entity roles in the brief ↔ tables:**

| Brief entity | Table | Key note |
|---|---|---|
| Clinic | `clinic` | tenancy root |
| User (role) | `users` | role enum; `author_role` variant adds `system` |
| Patient | `patient` | 1:1 optional with `users` |
| Entry/Note | `entry` | `author_role`, `entry_type`, `provenance_id`, `section`, `visibility` |
| Comment/Thread | `comment` | `parent_id` threading; `status` open/resolved |
| Version | `entry_version` | full snapshot + `delta_from_prev` + `conflict_flag` |
| Highlight | `highlight` | `risk_reason`, `status` suggested/accepted/rejected, span, provenance FK |
| Provenance | `provenance` | registry; `source_type`, `external_ref`, `payload` |
| AI_Scribed_Note | `ai_scribed_note` | 1:1 with entry; 3 types; pipeline metadata |
| Task/Assignment | `task` | assignee/assigner, status, priority, due |
| Mention | `mention` | unread queue via `read_at IS NULL` |
| LearningInteraction | `learning_interaction` | event log (who/what/feature/weight_delta) |
| LearningWeight | `learning_weight` | aggregated per (clinic, feature_key) |
| AuditLog | `audit_log` | metadata only |
| (Glance cache) | `patient_glance` | precomputed top-card |
| (Decay) | `entry_archive`, `entry_version_archive` | cold bodies |

---

## 2. Types (enums)

```sql
CREATE TYPE user_role       AS ENUM ('patient','staff','clinician','admin');
CREATE TYPE entry_author_role AS ENUM ('patient','staff','clinician','system');
CREATE TYPE entry_type      AS ENUM (
  'patient_note','staff_note','clinician_note','instruction',
  'ai_doctor_consult_summary','ai_nurse_consult_summary','ai_patient_session_summary',
  'system_event');
CREATE TYPE entry_visibility AS ENUM ('patient_visible','internal');
CREATE TYPE section_key     AS ENUM (
  'chief_complaint','history','medications','allergies','vitals',
  'plan','instructions','diagnosis','lab_orders','staff_handoff','general');
CREATE TYPE comment_status  AS ENUM ('open','resolved');
CREATE TYPE task_status     AS ENUM ('open','in_progress','done','cancelled');
CREATE TYPE risk_level      AS ENUM ('low','medium','high','critical');
CREATE TYPE highlight_status AS ENUM ('suggested','accepted','rejected');
CREATE TYPE highlight_source AS ENUM ('ai','rule','manual');
CREATE TYPE ai_note_type    AS ENUM ('ai_doctor_consult_summary','ai_nurse_consult_summary','ai_patient_session_summary');
CREATE TYPE provenance_source AS ENUM ('ai_patient_session','ai_doctor_consult','ai_nurse_consult','manual_note','voice_capture','system');
CREATE TYPE audit_action    AS ENUM ('create','update','revert','delete','comment','resolve_comment','unresolve_comment',
  'assign_task','update_task','highlight_suggest','highlight_accept','highlight_reject','mention','conflict_flagged','decay_archive','system');
CREATE TYPE interaction_type AS ENUM ('highlight_accept','highlight_reject','manual_highlight','edit','comment','pin','unpin','view_expand','task_complete');
```

---

## 3. Core tables (DDL)

All PKs `uuid DEFAULT gen_random_uuid()` unless noted; `timestamptz NOT NULL DEFAULT now()` for timestamps. Every tenant table has `clinic_id uuid NOT NULL REFERENCES clinic(id)`.

### 3.1 clinic, users, patient

```sql
CREATE TABLE clinic (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  email text NOT NULL UNIQUE,
  full_name text NOT NULL,                    -- synthetic; a redacted copy is what ever reaches the LLM
  role user_role NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_users_clinic ON users(clinic_id);

CREATE TABLE patient (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  user_id uuid UNIQUE REFERENCES users(id),   -- patient-facing login (user.role='patient'); nullable
  mrn text,                                   -- medical record number, synthetic
  display_name text NOT NULL,
  date_of_birth date,
  gender text,
  consent_to_store boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (clinic_id, mrn)
);
CREATE INDEX idx_patient_clinic ON patient(clinic_id);
```

### 3.2 provenance (registry for provenance_pointer)

```sql
CREATE TABLE provenance (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  patient_id uuid NOT NULL REFERENCES patient(id),
  source_type provenance_source NOT NULL,
  external_ref text,                          -- session_id, message_id, encounter id
  span_start int, span_end int,               -- char offsets into the source transcript/message
  payload jsonb NOT NULL DEFAULT '{}',        -- speaker labels, timestamps, confidence markers, code-switch segments
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (span_end IS NULL OR span_end >= span_start)
);
```

### 3.3 entry (the timeline)

```sql
CREATE TABLE entry (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  patient_id uuid NOT NULL REFERENCES patient(id),
  author_id uuid REFERENCES users(id),        -- NULL when author_role='system'
  author_role entry_author_role NOT NULL,
  entry_type entry_type NOT NULL,
  title text NOT NULL DEFAULT '',
  body text NOT NULL DEFAULT '',              -- markdown; COMPRESSION zstd (PG14+)
  section section_key,                        -- NULL = standalone timeline item
  visibility entry_visibility NOT NULL DEFAULT 'internal', -- patient_visible only for patient-facing summaries/instructions
  provenance_id uuid REFERENCES provenance(id),            -- provenance_pointer
  risk_level risk_level,                      -- explicit risk tag
  is_decayed boolean NOT NULL DEFAULT false,
  version int NOT NULL DEFAULT 1,             -- current version number (mirrors entry_version)
  status text NOT NULL DEFAULT 'final' CHECK (status IN ('draft','final')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (author_role <> 'system' OR author_id IS NULL),    -- system entries have no author
  CHECK (entry_type <> 'system_event' OR body <> '')       -- system events must carry context
);
CREATE INDEX idx_entry_patient_time      ON entry(patient_id, created_at DESC);
CREATE INDEX idx_entry_patient_type      ON entry(patient_id, entry_type, created_at DESC);
CREATE INDEX idx_entry_clinic            ON entry(clinic_id);
CREATE INDEX idx_entry_risk              ON entry(patient_id, risk_level) WHERE risk_level IS NOT NULL;
CREATE INDEX idx_entry_vis               ON entry(patient_id, visibility) WHERE visibility='patient_visible';
```

The three AI-scribed types are `entry_type` values with `author_role='system'`; they are visually distinct in the timeline because `author_role` is carried on every row.

### 3.4 comment (thread + resolve state)

```sql
CREATE TABLE comment (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  patient_id uuid NOT NULL REFERENCES patient(id),
  entry_id uuid NOT NULL REFERENCES entry(id) ON DELETE CASCADE,
  parent_id uuid REFERENCES comment(id) ON DELETE CASCADE,   -- reply tree = thread
  author_id uuid REFERENCES users(id),
  author_role entry_author_role NOT NULL,
  body text NOT NULL,
  visibility entry_visibility NOT NULL DEFAULT 'internal',   -- patient cannot see 'internal'
  status comment_status NOT NULL DEFAULT 'open',             -- resolve/unresolve
  resolved_at timestamptz,
  resolved_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_comment_entry  ON comment(entry_id, created_at);
CREATE INDEX idx_comment_patient_open ON comment(patient_id, created_at DESC) WHERE status='open';
```

### 3.5 entry_version (hybrid full snapshot + delta)

```sql
CREATE TABLE entry_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entry_id uuid NOT NULL REFERENCES entry(id) ON DELETE CASCADE,
  version int NOT NULL,
  body text NOT NULL,                          -- FULL SNAPSHOT of that version (zstd)
  delta_from_prev jsonb,                       -- char-level diff vs previous version; NULL for v1
  author_id uuid REFERENCES users(id),
  author_role entry_author_role NOT NULL,
  change_summary text,
  conflict_flag boolean NOT NULL DEFAULT false,
  conflict_of uuid REFERENCES entry_version(id),  -- the version this one superseded under a conflict
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entry_id, version)
);
CREATE INDEX idx_ev_entry_version ON entry_version(entry_id, version DESC);
```

`entry.version` on the parent row is the "checkout version" used for optimistic locking (see §7).

### 3.6 highlight (risk_reason + accept/reject + provenance + span)

```sql
CREATE TABLE highlight (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  patient_id uuid NOT NULL REFERENCES patient(id),
  entry_id uuid NOT NULL REFERENCES entry(id) ON DELETE CASCADE,
  offset_start int NOT NULL,
  offset_end int NOT NULL,                     -- span into entry.body (resolves the exact timeline span)
  quoted_text text NOT NULL,                   -- frozen text at time of highlight
  risk_reason text NOT NULL,                   -- REQUIRED: short human-readable reason
  risk_level risk_level NOT NULL DEFAULT 'medium',
  source highlight_source NOT NULL,            -- ai | rule | manual
  status highlight_status NOT NULL DEFAULT 'suggested',  -- accept/reject state
  confidence numeric(4,3),
  provenance_id uuid NOT NULL REFERENCES provenance(id), -- provenance_pointer (required)
  created_by uuid REFERENCES users(id),        -- NULL for system-suggested
  resolved_by uuid REFERENCES users(id),
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (offset_end >= offset_start),
  CHECK (char_length(quoted_text) > 0)
);
CREATE INDEX idx_hl_patient_status      ON highlight(patient_id, status, created_at DESC);
CREATE INDEX idx_hl_patient_suggested   ON highlight(patient_id) WHERE status='suggested';
CREATE INDEX idx_hl_entry               ON highlight(entry_id);
```

**Provenance contract (test_highlight_provenance.py):** every highlight row has non-null `provenance_id`, `entry_id`, `offset_start`, `offset_end`. A click resolves via `highlight → entry_id + span` to the exact timeline span; `quoted_text` is the frozen proof.

### 3.7 ai_scribed_note (1:1 with entry, author_role='system')

```sql
CREATE TABLE ai_scribed_note (
  entry_id uuid PRIMARY KEY REFERENCES entry(id) ON DELETE CASCADE,
  ai_note_type ai_note_type NOT NULL,           -- one of the 3 types
  model_name text NOT NULL,
  model_version text,
  pipeline_version text,
  source_session_id text,                       -- AI-patient session id for provenance
  redaction_confirmed boolean NOT NULL DEFAULT false,  -- PHI redaction ran BEFORE LLM
  redaction_mask jsonb NOT NULL DEFAULT '[]',   -- [{category,count}] only — never the PHI itself
  speaker_timestamps jsonb,                     -- clinical voice capture: [{speaker,start,end,confidence}]
  clinical_summary text,
  raw_confidence numeric(4,3),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ais_type ON ai_scribed_note(ai_note_type);
```
A trigger `ai_note_type_matches_entry` enforces `ai_note_type = entry.entry_type` and `entry.author_role='system'`, keeping the 1:1 relationship honest.

### 3.8 task (assignment)

```sql
CREATE TABLE task (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  patient_id uuid NOT NULL REFERENCES patient(id),
  entry_id uuid REFERENCES entry(id) ON DELETE SET NULL,  -- the note it was assigned from
  assignee_id uuid NOT NULL REFERENCES users(id),
  assigner_id uuid REFERENCES users(id),
  title text NOT NULL,
  description text,
  status task_status NOT NULL DEFAULT 'open',
  priority risk_level NOT NULL DEFAULT 'medium',   -- urgency scale (reuses enum)
  due_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX idx_task_patient_open ON task(patient_id, status, due_at) WHERE status IN ('open','in_progress');
CREATE INDEX idx_task_assignee     ON task(assignee_id, status) WHERE status='open';
CREATE INDEX idx_task_clinic       ON task(clinic_id);
```
A trigger `task_assignee_in_clinic` rejects assignee/assigner outside the task's clinic. Open tasks feed the Glance "open actions" (e.g., "needs lab order", "waiting nurse follow-up").

### 3.9 mention

```sql
CREATE TABLE mention (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  comment_id uuid REFERENCES comment(id) ON DELETE CASCADE,
  entry_id uuid REFERENCES entry(id) ON DELETE CASCADE,
  mentioned_user_id uuid NOT NULL REFERENCES users(id),
  created_by uuid NOT NULL REFERENCES users(id),
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (comment_id IS NOT NULL OR entry_id IS NOT NULL),
  UNIQUE (comment_id, mentioned_user_id)         -- one notification per @per comment
);
CREATE INDEX idx_mention_unread ON mention(mentioned_user_id, read_at) WHERE read_at IS NULL;
```
Unread mentions act as the notification queue for `@nurse_name` / `@clinician_name`.

### 3.10 entry_entity (tagged clinical entities → risk + learning)

```sql
CREATE TABLE entry_entity (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_id uuid NOT NULL REFERENCES entry(id) ON DELETE CASCADE,
  entity_type text NOT NULL,                     -- medication | chief_complaint | allergy | symptom | ...
  entity_value text NOT NULL,                    -- normalized (lowercased/canonical)
  offset_start int, offset_end int,
  created_by text NOT NULL DEFAULT 'ai' CHECK (created_by IN ('ai','rule','manual')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entry_id, entity_type, entity_value)
);
CREATE INDEX idx_ee_value ON entry_entity(entity_type, entity_value);
```
Allergy tags can force `risk_level='critical'` in the scorer. Entity values are also the **features** the learning mechanism keys on.

### 3.11 learning_interaction + learning_weight (persisted self-learning)

```sql
CREATE TABLE learning_interaction (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  patient_id uuid NOT NULL REFERENCES patient(id),
  entry_id uuid REFERENCES entry(id) ON DELETE SET NULL,
  user_id uuid REFERENCES users(id),
  interaction_type interaction_type NOT NULL,     -- accept/reject/edit/comment/pin...
  feature_keys jsonb NOT NULL DEFAULT '[]',       -- normalized features extracted from interacted content
  weight_delta numeric NOT NULL DEFAULT 0,        -- applied to each feature
  occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_li_time ON learning_interaction(occurred_at DESC);

CREATE TABLE learning_weight (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  clinic_id uuid NOT NULL REFERENCES clinic(id) ON DELETE CASCADE,
  feature_key text NOT NULL,        -- e.g. 'entity:medication:amoxicillin', 'keyword:asthma', 'section:plan'
  weight numeric NOT NULL DEFAULT 0, -- learned boost, clamped to [-1, +2] via trigger/function
  positive_count int NOT NULL DEFAULT 0,
  negative_count int NOT NULL DEFAULT 0,
  total_interactions int NOT NULL DEFAULT 0,
  last_seen_at timestamptz NOT NULL DEFAULT now(), -- used for time-decay of weight
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (clinic_id, feature_key)
);
CREATE INDEX idx_lw_key   ON learning_weight(feature_key);
CREATE INDEX idx_lw_clinic ON learning_weight(clinic_id);
```

**Learning loop (§9):** a clinician/`staff` accepts/rejects/pins a highlight or comments on/edits an AI note → `record_interaction()` (SECURITY DEFINER) logs it and upserts `learning_weight` for each extracted feature (positive on accept/comment/edit/pin, negative on reject). Future AI notes with overlapping features get a higher suggestion score. **Weights are written only by SECURITY DEFINER functions — RLS grants no direct write to any user role.**

### 3.12 audit_log (metadata ONLY)

```sql
CREATE TABLE audit_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  patient_id uuid REFERENCES patient(id),
  actor_id uuid REFERENCES users(id),            -- NULL for system
  actor_role entry_author_role,
  action audit_action NOT NULL,
  target_type text NOT NULL,                     -- 'entry','version','comment','highlight','task','patient'
  target_id uuid NOT NULL,
  version int,                                   -- entry version if applicable
  metadata jsonb NOT NULL DEFAULT '{}',          -- pointers/ids/state transitions ONLY; NEVER body content
  occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_patient_time ON audit_log(patient_id, occurred_at DESC);
CREATE INDEX idx_audit_clinic_time  ON audit_log(clinic_id, occurred_at DESC);
```
Convention enforced by the `log_audit()` function + a comment on the column: **metadata holds ids and state transitions, never note content** — satisfies `test_revision_history` ("audit log shows who changed what, metadata only"). A BEFORE INSERT/UPDATE trigger rejects any JSON key named `body`/`content`.

### 3.13 patient_glance (precomputed top-card for ≤300ms)

```sql
CREATE TABLE patient_glance (
  patient_id uuid PRIMARY KEY REFERENCES patient(id) ON DELETE CASCADE,
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  top_items jsonb NOT NULL DEFAULT '[]',    -- [{item_type,item_id,preview,score,risk_reason}]
  open_actions jsonb NOT NULL DEFAULT '[]', -- [{kind:'task'|'comment'|'highlight'|'conflict', id, label}]
  risk_flags jsonb NOT NULL DEFAULT '[]',
  computed_at timestamptz NOT NULL DEFAULT now(),
  invalidated boolean NOT NULL DEFAULT false
);
CREATE INDEX idx_glance_clinic ON patient_glance(clinic_id);
```
Invalidated (set true) inside the same transaction as any mutation to that patient's data; recomputed by `recompute_glance(patient_id)` (SECURITY DEFINER) on read if `invalidated`. Because recompute runs under RLS, the cache can never serve a row the caller may not read.

### 3.14 data-decay archives

```sql
CREATE TABLE entry_archive (                    -- cold copies of decayed entry bodies
  entry_id uuid PRIMARY KEY,                    -- same id as the stub in entry
  patient_id uuid NOT NULL,
  clinic_id uuid NOT NULL,
  body text NOT NULL,                           -- zstd-compressed column
  section section_key,
  entry_type entry_type,
  provenance_id uuid,
  archived_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE entry_version_archive (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_id uuid NOT NULL,
  version int NOT NULL,
  body text NOT NULL,
  author_role entry_author_role NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_arch_patient ON entry_archive(patient_id);
CREATE INDEX idx_arch_entry ON entry_archive(entry_id);
CREATE INDEX idx_archv_entry ON entry_version_archive(entry_id, version);
```

---

## 4. Row-Level Security (concrete SQL)

### 4.1 Session context & helper functions

Middleware, immediately after JWT verification and inside the request transaction, runs:
```sql
SET LOCAL app.user_id = '<verified user id>';
SET LOCAL app.role    = '<verified role: patient|staff|clinician|admin>';
SET LOCAL app.clinic_id = '<verified clinic id>';
```
GUCs come only from the verified token — never from client input. Helpers:

```sql
CREATE FUNCTION app_user_id() RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.user_id', true),'')::uuid $$;
CREATE FUNCTION app_role() RETURNS text LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.role', true),'') $$;
CREATE FUNCTION app_clinic_id() RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.clinic_id', true),'')::uuid $$;
-- patient-scoped id: returns the patient row for the logged-in patient, else NULL
CREATE FUNCTION app_patient_id() RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT id FROM patient WHERE user_id = app_user_id() $$;
-- which role owns a section (drives write-isolation)
CREATE FUNCTION section_role_of(s section_key) RETURNS user_role LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE s WHEN 'staff_handoff' THEN 'staff'::user_role ELSE 'clinician'::user_role END $$;
```

DB roles and base grants:
```sql
CREATE ROLE patient_role; CREATE ROLE staff_role; CREATE ROLE clinician_role; CREATE ROLE admin_role;
CREATE ROLE system_pipeline;
GRANT USAGE ON SCHEMA public TO patient_role, staff_role, clinician_role, admin_role, system_pipeline;
ALTER TABLE entry, comment, highlight, task, mention, entry_version, provenance,
  ai_scribed_note, entry_entity, learning_interaction, learning_weight,
  audit_log, patient_glance, entry_archive, entry_version_archive
  ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;   -- FORCE: even owner can't bypass
```

### 4.2 entry — SELECT / INSERT / UPDATE policies (write-isolation core)

```sql
-- PATIENT: own patient only, patient-facing content only
CREATE POLICY entry_sel_patient ON entry FOR SELECT TO patient_role
  USING (patient_id = app_patient_id() AND visibility = 'patient_visible');
CREATE POLICY entry_ins_patient ON entry FOR INSERT TO patient_role
  WITH CHECK (patient_id = app_patient_id()
              AND author_role = 'patient'
              AND visibility = 'patient_visible');

-- STAFF: clinic scope read of all entries (incl. patient insights & AI notes); WRITE only staff_notes in staff-owned section
CREATE POLICY entry_sel_staff ON entry FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY entry_ins_staff ON entry FOR INSERT TO staff_role
  WITH CHECK (clinic_id = app_clinic_id()
              AND author_role = 'staff'
              AND entry_type = 'staff_note'
              AND section_role_of(section) = 'staff');
CREATE POLICY entry_upd_staff ON entry FOR UPDATE TO staff_role
  USING (clinic_id = app_clinic_id() AND author_role = 'staff')          -- cannot touch clinician rows
  WITH CHECK (clinic_id = app_clinic_id()
              AND author_role = 'staff'
              AND section_role_of(section) = 'staff');

-- CLINICIAN: clinic scope read; WRITE only clinician_sections (plan, meds, dx, ...)
CREATE POLICY entry_sel_clin ON entry FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY entry_ins_clin ON entry FOR INSERT TO clinician_role
  WITH CHECK (clinic_id = app_clinic_id()
              AND author_role = 'clinician'
              AND section_role_of(section) = 'clinician');
CREATE POLICY entry_upd_clin ON entry FOR UPDATE TO clinician_role
  USING (clinic_id = app_clinic_id() AND author_role = 'clinician')
  WITH CHECK (clinic_id = app_clinic_id()
              AND author_role = 'clinician'
              AND section_role_of(section) = 'clinician');

-- ADMIN: clinic-scoped oversight (read/write all within clinic)
CREATE POLICY entry_all_admin ON entry FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id())
  WITH CHECK (clinic_id = app_clinic_id());

-- SYSTEM: AI ingestion writes author_role='system'
CREATE POLICY entry_ins_system ON entry FOR INSERT TO system_pipeline
  WITH CHECK (clinic_id = app_clinic_id() AND author_role = 'system');
```

This **is** the `test_rbac_scope.py` guarantee: a staff's `UPDATE entry ... SET body=... WHERE id=<clinician_note_id>` matches 0 rows under `entry_upd_staff` (USING requires `author_role='staff'`), and even if it matched, the WITH CHECK section rule would reject it. Clinician over staff is symmetric.

### 4.3 comment

```sql
-- patient: only patient_visible comments on own patient; may post own comments
CREATE POLICY comment_sel_patient ON comment FOR SELECT TO patient_role
  USING (patient_id = app_patient_id() AND visibility = 'patient_visible');
CREATE POLICY comment_ins_patient ON comment FOR INSERT TO patient_role
  WITH CHECK (patient_id = app_patient_id() AND author_role='patient' AND visibility='patient_visible');

-- staff/clinician: clinic scope read; may post as their own role; resolve own-clinic comments
CREATE POLICY comment_sel_staff ON comment FOR SELECT TO staff_role USING (clinic_id = app_clinic_id());
CREATE POLICY comment_sel_clin  ON comment FOR SELECT TO clinician_role USING (clinic_id = app_clinic_id());
CREATE POLICY comment_ins_staff ON comment FOR INSERT TO staff_role
  WITH CHECK (clinic_id = app_clinic_id() AND author_role='staff');
CREATE POLICY comment_ins_clin  ON comment FOR INSERT TO clinician_role
  WITH CHECK (clinic_id = app_clinic_id() AND author_role='clinician');
-- resolve/unresolve allowed for staff+clinician within clinic (status, resolved_at, resolved_by)
CREATE POLICY comment_upd_staff ON comment FOR UPDATE TO staff_role
  USING (clinic_id = app_clinic_id() AND author_role <> 'clinician');
CREATE POLICY comment_upd_clin  ON comment FOR UPDATE TO clinician_role
  USING (clinic_id = app_clinic_id() AND author_role <> 'staff');

CREATE POLICY comment_all_admin ON comment FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());
```

### 4.4 highlight (internal; patient gets no policy → invisible)

```sql
CREATE POLICY hl_sel_staff ON highlight FOR SELECT TO staff_role USING (clinic_id = app_clinic_id());
CREATE POLICY hl_sel_clin  ON highlight FOR SELECT TO clinician_role USING (clinic_id = app_clinic_id());
CREATE POLICY hl_all_admin ON highlight FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());
-- manual highlights by staff/clinician (source='manual'); AI/rule suggestions by pipeline
CREATE POLICY hl_ins_staff ON highlight FOR INSERT TO staff_role
  WITH CHECK (clinic_id = app_clinic_id() AND source='manual' AND created_by = app_user_id());
CREATE POLICY hl_ins_clin  ON highlight FOR INSERT TO clinician_role
  WITH CHECK (clinic_id = app_clinic_id() AND source='manual' AND created_by = app_user_id());
CREATE POLICY hl_ins_system ON highlight FOR INSERT TO system_pipeline
  WITH CHECK (clinic_id = app_clinic_id() AND source IN ('ai','rule'));
-- accept/reject: staff & clinician may update status only; content is protected below
CREATE POLICY hl_upd_staff ON highlight FOR UPDATE TO staff_role
  USING (clinic_id = app_clinic_id() AND status='suggested');
CREATE POLICY hl_upd_clin  ON highlight FOR UPDATE TO clinician_role
  USING (clinic_id = app_clinic_id() AND status='suggested');
```
**Content protection (defense in depth, since RLS is row-level):**
```sql
REVOKE UPDATE ON highlight FROM patient_role;
GRANT  UPDATE (status, resolved_by, resolved_at) ON highlight TO staff_role, clinician_role;
-- trigger blocks any attempt to change immutable highlight fields
CREATE OR REPLACE FUNCTION trg_highlight_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.entry_id     IS DISTINCT FROM OLD.entry_id
     OR NEW.risk_reason   IS DISTINCT FROM OLD.risk_reason
     OR NEW.provenance_id IS DISTINCT FROM OLD.provenance_id
     OR NEW.offset_start  <> OLD.offset_start
     OR NEW.offset_end    <> OLD.offset_end
     OR NEW.quoted_text   IS DISTINCT FROM OLD.quoted_text THEN
    RAISE EXCEPTION 'highlight provenance/risk fields are immutable';
  END IF;
  RETURN NEW;
END $$;
```

### 4.5 entry_version, provenance, ai_scribed_note, entry_entity, task, mention

Pattern (write-once by pipeline or via SECURITY DEFINER functions; reads clinic-scoped):

```sql
CREATE POLICY ev_sel_staff ON entry_version FOR SELECT TO staff_role USING (clinic_id = app_clinic_id());
CREATE POLICY ev_sel_clin  ON entry_version FOR SELECT TO clinician_role USING (clinic_id = app_clinic_id());
CREATE POLICY ev_all_admin ON entry_version FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());
-- INSERT of new versions goes through SECURITY DEFINER save_entry() below, not direct INSERT
```

- `provenance`: SELECT staff/clinician/admin (clinic scope); INSERT `system_pipeline`.
- `ai_scribed_note`: SELECT staff/clinician/admin (clinic scope); INSERT `system_pipeline`. **No patient policy → patients can never read raw AI notes.**
- `entry_entity`: SELECT staff/clinician/admin; INSERT pipeline + system_pipeline.
- `task`: SELECT staff/clinician/admin (clinic scope); INSERT/UPDATE by staff/clinician within clinic (WITH CHECK `clinic_id=app_clinic_id()`); `assignee_in_clinic` trigger; admin all.
- `mention`: SELECT staff/clinician/admin (clinic scope) and target user themselves (`mentioned_user_id = app_user_id()`); INSERT by staff/clinician within clinic.

### 4.6 learning tables + audit_log + glance (system-write, admin-read)

```sql
-- learning: readable by clinical roles, writable ONLY by SECURITY DEFINER functions
CREATE POLICY lw_sel_staff ON learning_weight FOR SELECT TO staff_role USING (clinic_id = app_clinic_id());
CREATE POLICY lw_sel_clin  ON learning_weight FOR SELECT TO clinician_role USING (clinic_id = app_clinic_id());
CREATE POLICY li_sel_staff ON learning_interaction FOR SELECT TO staff_role USING (clinic_id = app_clinic_id());
CREATE POLICY li_sel_clin  ON learning_interaction FOR SELECT TO clinician_role USING (clinic_id = app_clinic_id());
-- no INSERT/UPDATE grants to any user role

-- audit_log: admin-only reads; writes via SECURITY DEFINER log_audit()
CREATE POLICY audit_sel_admin ON audit_log FOR SELECT TO admin_role USING (clinic_id = app_clinic_id());

-- glance: read by clinical roles; recompute via SECURITY DEFINER
CREATE POLICY glance_sel_staff ON patient_glance FOR SELECT TO staff_role USING (clinic_id = app_clinic_id());
CREATE POLICY glance_sel_clin  ON patient_glance FOR SELECT TO clinician_role USING (clinic_id = app_clinic_id());
CREATE POLICY glance_sel_admin ON patient_glance FOR SELECT TO admin_role USING (clinic_id = app_clinic_id());
```

The `system_pipeline` role and the SECURITY DEFINER functions (`save_entry`, `revert_entry`, `log_audit`, `record_interaction`, `update_weights`, `recompute_glance`, `decay_old_entries`, `restore_entry`) are owned by a `superuser`/owner that is not a login role; RLS `FORCE` still applies to them, so they validate clinic_id/patient_id/author_role explicitly.

---

## 5. Versioning approach (hybrid snapshot + delta)

**Decision: full snapshot per version + stored char-level delta.**
- Revert: **copy the target snapshot forward as a NEW version** (never mutate history), set `entry.body` to it, bump `entry.version`, write `audit_log(action='revert')`. This satisfies "reverting returns content to a prior state" while preserving an immutable audit trail.
- "View changes since X": with full snapshots it is a two-snapshot diff — exact and O(1) in history depth:
```sql
SELECT body FROM entry_version WHERE entry_id = $1 AND version = $2;  -- state at X
SELECT body FROM entry WHERE id = $1;                                 -- current
```
  The app diffs the two markdown bodies (or uses the accumulated `delta_from_prev` for a cheap side-by-side without recomputing large diffs).
- Storage cost is bounded by the decay job (§8), which archives old version bodies.

**Save path (single transactional function):**
```sql
CREATE FUNCTION save_entry(entry_id uuid, new_body text, base_version int,
                           user_id uuid, user_role entry_author_role)
  RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v int; delta jsonb;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('section:'|| (SELECT patient_id FROM entry WHERE id=entry_id)
                                         ||':'|| COALESCE((SELECT section FROM entry WHERE id=entry_id),'standalone')));
  SELECT version INTO v FROM entry WHERE id = entry_id FOR UPDATE;
  IF v IS NULL THEN RAISE EXCEPTION 'entry not found'; END IF;
  IF v <> base_version THEN PERFORM resolve_conflict(entry_id, user_id, user_role, new_body, base_version); RETURN; END IF;
  delta := char_diff(OLD_body := (SELECT body FROM entry WHERE id=entry_id), new_body); -- computed app/plpgsql
  INSERT INTO entry_version(entry_id, version, body, delta_from_prev, author_id, author_role)
    VALUES (entry_id, v+1, new_body, delta, user_id, user_role);
  UPDATE entry SET body=new_body, version=v+1, updated_at=now() WHERE id=entry_id;
  PERFORM log_audit(entry_id, 'update', user_id, user_role, v+1, jsonb_build_object('version',v+1));
END $$;
```
RLS does not protect `entry` UPDATE here (SECURITY DEFINER), so `save_entry` re-checks `author_role`/`section_role_of` against `user_role` before writing — mirroring the policy clauses.

---

## 6. Index strategy (longitudinal timeline + glance)

| Query | Index | Why |
|---|---|---|
| Timeline: all entries for a patient, newest first | `entry(patient_id, created_at DESC)` | primary feed scan |
| Timeline filtered by type (AI vs manual) | `entry(patient_id, entry_type, created_at DESC)` | mixed-feed filter |
| Patient-facing feed | `entry(patient_id, visibility) WHERE visibility='patient_visible'` | partial index, tiny |
| Glance: open actions | `task(patient_id, status, due_at) WHERE status IN ('open','in_progress')` | partial, only open rows |
| Glance: suggested highlights | `highlight(patient_id) WHERE status='suggested'` | partial |
| Highlight per entry (span resolution) | `highlight(entry_id)` | click → span |
| Resolve/unresolve open threads | `comment(patient_id, created_at DESC) WHERE status='open'` | partial |
| Version diff / since-X | `entry_version(entry_id, version DESC)` | unique pair nav |
| Learning feature lookup | `learning_weight(feature_key)` + `learning_weight(clinic_id)` | feature match on new AI notes |
| Unread mentions | `mention(mentioned_user_id, read_at) WHERE read_at IS NULL` | notification queue |
| Audit trail | `audit_log(patient_id, occurred_at DESC)` / `audit_log(clinic_id, occurred_at DESC)` | who/what/when |
| Entity risk boost | `entry_entity(entity_type, entity_value)` | allergy/med lookup |
| Tenant filter everywhere | `clinic_id` btree on every tenant table | RLS predicates hit the index |

**Glance P95 ≤ 300ms (warm):** read path is `SELECT top_items, open_actions, risk_flags FROM patient_glance WHERE patient_id=$1` — a single PK lookup on a precomputed JSONB row (already includes open-task and suggested-highlight counts via partial indexes during recompute). Measured with `pgbench` (warm = prewarmed page cache + non-invalidated glance) and reported in the technical brief.

---

## 7. Concurrency control at the DB level

**Different sections never collide:** each section is its own `entry` row (`section` column); a staff editing `staff_handoff` and a clinician editing `plan` touch different rows — PostgreSQL rows lock independently, so there is no lost update. The WebSocket layer provides Google-Docs-style live presence, but the DB is the source of truth.

**Same-section conflicts — deterministic strategy:**
1. **Optimistic lock:** `save_entry` requires `base_version` to equal `entry.version`; it takes a row lock (`FOR UPDATE`) and a **section advisory lock** (`pg_advisory_xact_lock` keyed on patient+section) so the check-and-write is atomic.
2. **If versions diverge → `resolve_conflict`:**
   - Deterministic winner: **later `updated_at` wins; tie → higher `version` wins; final tie → higher `entry_version.id` wins.** (Clinician precedence is already guaranteed structurally because clinical sections are clinician-owned, so the only same-section races are same-role or patient-vs-clinical on shared sections.)
   - The **loser is never lost**: its content is written as a new child `entry_version` with `conflict_flag=true` and `conflict_of = <winner_version_id>`; `entry.body` becomes the winner's content; `audit_log(action='conflict_flagged')`.
   - The conflict is surfaced as an open Glance action ("flag for review") so a human reconciles — implementing the brief's second path ("OR the system must flag the conflict for review").
3. **Isolation level:** `READ COMMITTED` (default); the advisory lock serializes concurrent same-section commits so last-writer-wins is applied atomically rather than last-commit-wins.

```sql
-- deterministic winner helper used by resolve_conflict()
-- winner = GREATEST(updated_at, then version, then id) of the two competing rows
```

This satisfies `test_concurrent_edits.py` (different sections never overwrite; same-section has a documented deterministic resolution and no data loss).

---

## 8. Hybrid storage / data decay design

- **Hot path:** `entry.body` / `entry_version.body` are `text` columns with `ALTER TABLE ... ALTER COLUMN body SET COMPRESSION zstd;` (PG14+) — cheap win for large AI-scribed bodies.
- **Cold path:** nightly `decay_old_entries()` (SECURITY DEFINER, run by app scheduler or `pg_cron`):
  1. Select entries older than `decay_threshold` (configurable per clinic, default 730 days) **with `is_decayed=false`** and **no open references** — excluded if any `task.status IN ('open','in_progress')`, `highlight.status='suggested'|'accepted'`, or `comment.status='open'` references them.
  2. Copy `entry.body` (+ all `entry_version` bodies) into `entry_archive` / `entry_version_archive`.
  3. Set `entry.is_decayed=true`, `entry.body=''` (stub keeps all metadata: author, type, section, risk_level, provenance_id, timestamps, version).
  4. `log_audit(action='decay_archive')`.
- **Timeline rendering:** stubs render as "… (archived — click to expand)" with metadata; `restore_entry(entry_id)` (SECURITY DEFINER) pulls the body back from the archive on demand (also used by revert of a decayed entry).
- **Alternative at scale:** RANGE-partition `entry` by `created_at` (monthly/yearly) and `DETACH` old partitions to a cold tablespace; the archive-table approach is chosen for the 72-hour build because it needs no partition management while delivering the same schema + logic story.

---

## 9. Persistence of the self-learning importance mechanism

**Scoring (Glance priority)** — combined core + learned terms:
```
score(item) = w_recency · recency_factor(created_at)
            + w_risk    · risk_weight(risk_level)          -- critical=10 … low=1
            + w_entity  · max entity_importance(entry_entity)  -- allergy=critical boost
            + w_task    · open_action_bonus(entry_id)      -- unresolved task/comment on this entry
            + w_learn   · learned_boost(item)              -- <-- self-learning term
```
`learned_boost(item) = Σ_{f ∈ features(item)} clamp(decayed_weight(f))`, where `decayed_weight(f) = learning_weight.weight[f] · exp(-λ·age(last_seen_at))` (time decay so stale lessons fade).

**Persistence loop:**
1. Clinician/staff acts on an AI note — accept/reject/pin a highlight, comment, edit, expand.
2. `record_interaction(user, entry, type)` (SECURITY DEFINER) extracts normalized features from that content (via `entry_entity` + keyword/section features), writes a `learning_interaction` row (`weight_delta` = +1 accept / −1 reject), and upserts each feature into `learning_weight`:
   - accept/comment/edit/pin → `weight += 1`, `positive_count += 1`
   - reject → `weight -= 1`, `negative_count += 1`
   - `total_interactions += 1`, `last_seen_at = now()`, weight clamped to `[-1, 2]`.
3. When the AI/rule pipeline generates a new suggestion, it computes `learned_boost` from `learning_weight` and adds it to the candidate score before inserting `highlight(status='suggested')`.

**test_self_learning_importance.py:** pin an accepted highlight from an AI-scribed note → `record_interaction(highlight_accept, features)` persists +1 weight on `keyword/entity/section` keys → a later AI note with overlapping features is scored higher → assertion passes. Fully persisted; survives restart; explainable via `positive_count`/`negative_count`.

---

## 10. How this schema satisfies the required micro-tests

| Test | Mechanism |
|---|---|
| `test_rbac_scope.py` | RLS write-isolation: staff/clinician UPDATE policies require matching `author_role` + `section_role_of`; patient has no policy on `comment`/`ai_scribed_note`/`highlight` → inaccessible; cross-clinic filtered by `app_clinic_id()`. |
| `test_revision_history.py` | `save_entry` inserts `entry_version` with `version+1`; `revert_entry` writes the target snapshot as a new version; `audit_log` rows record actor/action/version with metadata only. |
| `test_highlight_provenance.py` | `highlight.provenance_id` + `entry_id` + `offset_start/end` are NOT NULL; resolution query joins `provenance` and slices `entry.body`. |
| `test_concurrent_edits.py` | Section rows + optimistic lock + advisory lock; same-section conflict → `resolve_conflict` deterministic winner, loser preserved with `conflict_flag`, flagged for review. |
| `test_self_learning_importance.py` | `learning_interaction`/`learning_weight` persisted via SECURITY DEFINER; accepted-pin increments weight; next suggestion scores higher. |

---

## 11. Assumptions, trade-offs, scope

- **One user row per clinician/staff/admin/patient**; role is single-valued (a user who is both staff and clinician is modeled with the higher privilege in practice, or a `user_roles` join table later — out of scope for 72h).
- **`entry_version` full snapshots** chosen over pure diff for bulletproof revert + exact "since X"; storage growth mitigated by zstd + decay archive.
- **Single-owner sections** make cross-role same-section conflicts structurally impossible for clinical sections; the conflict engine therefore targets same-role and patient-vs-clinical races on shared sections — documented deterministic rule.
- **Patient** may contribute notes/comments (`patient_visible`) and read only `patient_visible` entries/comments — patient-contributed insights are visible to staff/clinician; raw AI notes and internal comments are not.
- **Audit "metadata only"** is enforced by convention + a trigger rejecting `body`/`content` JSON keys; content is reconstructable from `entry_version` when needed.
- **Glance latency** depends on the precomputed `patient_glance` snapshot; recompute runs synchronously post-mutation within the same transaction to keep the cache valid, and `pgbench`-measured P95 is reported per the brief.
- RLS is the server-side enforcement; the FastAPI middleware re-validates role+clinic as a second line (defense in depth), never as the only line.
