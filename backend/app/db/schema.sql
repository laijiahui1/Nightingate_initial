-- =============================================================================
-- Nightingale — schema.sql
--
-- Full DDL for the Nightingale 72-hour build data layer (PostgreSQL 16).
-- Source of truth: docs/DATA_SCHEMA.md (authoritative schema design).
--
-- Contents
--   1. pgcrypto extension (gen_random_uuid)
--   2. All enum types (guarded so the file is idempotent)
--   3. Core + supporting tables (clinic, users, patient, provenance, entry,
--      comment, entry_version, highlight, ai_scribed_note, task, mention,
--      entry_entity, learning_interaction, learning_weight, audit_log,
--      patient_glance, entry_archive, entry_version_archive)
--   4. Indexes for the longitudinal-timeline + glance query paths
--   5. zstd column compression on cold body columns (PG14+)
--   6. Data-integrity triggers (entry/ai-note consistency, assignee clinic
--      scoping, immutable highlight fields, learning-weight clamp, audit
--      metadata-only + append-only guard)
--
-- Ownership: this file is executed with `SET ROLE nightingale_owner` so that
-- every object is owned by the non-login schema owner (created by roles.sql).
-- RLS enablement + policies live in rls.sql; role definitions live in roles.sql.
-- -----------------------------------------------------------------------------

SET ROLE nightingale_owner;

-- =============================================================================
-- 1. Extension
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =============================================================================
-- 2. Enum types (idempotent — CREATE TYPE has no IF NOT EXISTS)
-- =============================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
    CREATE TYPE user_role AS ENUM ('patient','staff','clinician','admin');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'entry_author_role') THEN
    CREATE TYPE entry_author_role AS ENUM ('patient','staff','clinician','system');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'entry_type') THEN
    CREATE TYPE entry_type AS ENUM (
      'patient_note','staff_note','clinician_note','instruction',
      'ai_doctor_consult_summary','ai_nurse_consult_summary','ai_patient_session_summary',
      'system_event');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'entry_visibility') THEN
    CREATE TYPE entry_visibility AS ENUM ('patient_visible','internal');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'section_key') THEN
    CREATE TYPE section_key AS ENUM (
      'chief_complaint','history','medications','allergies','vitals',
      'plan','instructions','diagnosis','lab_orders','staff_handoff','general');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'comment_status') THEN
    CREATE TYPE comment_status AS ENUM ('open','resolved');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'task_status') THEN
    CREATE TYPE task_status AS ENUM ('open','in_progress','done','cancelled');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'risk_level') THEN
    CREATE TYPE risk_level AS ENUM ('low','medium','high','critical');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'highlight_status') THEN
    CREATE TYPE highlight_status AS ENUM ('suggested','accepted','rejected');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'highlight_source') THEN
    CREATE TYPE highlight_source AS ENUM ('ai','rule','manual');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ai_note_type') THEN
    CREATE TYPE ai_note_type AS ENUM (
      'ai_doctor_consult_summary','ai_nurse_consult_summary','ai_patient_session_summary');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'provenance_source') THEN
    CREATE TYPE provenance_source AS ENUM (
      'ai_patient_session','ai_doctor_consult','ai_nurse_consult',
      'manual_note','voice_capture','system');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'audit_action') THEN
    CREATE TYPE audit_action AS ENUM (
      'create','update','revert','delete','comment','resolve_comment','unresolve_comment',
      'assign_task','update_task','highlight_suggest','highlight_accept','highlight_reject',
      'mention','conflict_flagged','decay_archive','system');
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'interaction_type') THEN
    CREATE TYPE interaction_type AS ENUM (
      'highlight_accept','highlight_reject','manual_highlight','edit','comment',
      'pin','unpin','view_expand','task_complete');
  END IF;
END $$;

-- =============================================================================
-- 3. Core + supporting tables
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 3.1 clinic, users, patient  (DATA_SCHEMA §3.1)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS clinic (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  email text NOT NULL UNIQUE,
  full_name text NOT NULL,                    -- synthetic; a redacted copy is what ever reaches the LLM
  role user_role NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_users_clinic ON users(clinic_id);

CREATE TABLE IF NOT EXISTS patient (
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
CREATE INDEX IF NOT EXISTS idx_patient_clinic ON patient(clinic_id);

-- -----------------------------------------------------------------------------
-- 3.2 provenance (registry for the provenance_pointer)  (DATA_SCHEMA §3.2)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS provenance (
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
CREATE INDEX IF NOT EXISTS idx_prov_clinic ON provenance(clinic_id);

-- -----------------------------------------------------------------------------
-- 3.3 entry (the timeline)  (DATA_SCHEMA §3.3)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS entry (
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
CREATE INDEX IF NOT EXISTS idx_entry_patient_time      ON entry(patient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entry_patient_type      ON entry(patient_id, entry_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entry_clinic            ON entry(clinic_id);
CREATE INDEX IF NOT EXISTS idx_entry_risk              ON entry(patient_id, risk_level) WHERE risk_level IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entry_vis               ON entry(patient_id, visibility) WHERE visibility='patient_visible';

-- -----------------------------------------------------------------------------
-- 3.4 comment (thread + resolve state)  (DATA_SCHEMA §3.4)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS comment (
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
CREATE INDEX IF NOT EXISTS idx_comment_entry  ON comment(entry_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comment_patient_open ON comment(patient_id, created_at DESC) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_comment_clinic ON comment(clinic_id);

-- -----------------------------------------------------------------------------
-- 3.5 entry_version (hybrid full snapshot + delta)  (DATA_SCHEMA §3.5)
--
-- NOTE (deviation): DATA_SCHEMA's §3.5 DDL omits `clinic_id`, but its own RLS
-- policies (§4.5 `ev_sel_*`) predicate on `clinic_id = app_clinic_id()` and
-- guiding decision #1 says every patient-scoped table carries clinic_id. We add
-- `clinic_id` here so RLS can scope child versions exactly as documented.
--
-- NOTE (deviation): `id` is `bigint GENERATED ALWAYS AS IDENTITY` rather than
-- the `uuid` shown in DATA_SCHEMA §3.5. This keeps `conflict_of` (a self-FK that
-- points at the winning version) cheap and monotonic; `conflict_of` and the
-- resolve_conflict()/save_entry() code paths agree on bigint.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS entry_version (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_id uuid NOT NULL REFERENCES entry(id) ON DELETE CASCADE,
  clinic_id uuid NOT NULL REFERENCES clinic(id),           -- added for RLS scoping (see note)
  version int NOT NULL,
  body text NOT NULL,                          -- FULL SNAPSHOT of that version (zstd)
  delta_from_prev jsonb,                       -- char-level diff vs previous version; NULL for v1
  author_id uuid REFERENCES users(id),
  author_role entry_author_role NOT NULL,
  change_summary text,
  conflict_flag boolean NOT NULL DEFAULT false,
  conflict_of bigint REFERENCES entry_version(id),  -- the version this one superseded under a conflict
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entry_id, version)
);
CREATE INDEX IF NOT EXISTS idx_ev_entry_version ON entry_version(entry_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_ev_clinic        ON entry_version(clinic_id);

-- -----------------------------------------------------------------------------
-- 3.6 highlight (risk_reason + accept/reject + provenance + span)  (DATA_SCHEMA §3.6)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS highlight (
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
CREATE INDEX IF NOT EXISTS idx_hl_patient_status    ON highlight(patient_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hl_patient_suggested ON highlight(patient_id) WHERE status='suggested';
CREATE INDEX IF NOT EXISTS idx_hl_entry             ON highlight(entry_id);
CREATE INDEX IF NOT EXISTS idx_hl_clinic            ON highlight(clinic_id);

-- -----------------------------------------------------------------------------
-- 3.7 ai_scribed_note (1:1 with entry, author_role='system')  (DATA_SCHEMA §3.7)
--
-- NOTE (deviation): `clinic_id` added for the same RLS reason as entry_version —
-- ai_scribed_note is patient-scoped and must be filtered by app_clinic_id().
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ai_scribed_note (
  entry_id uuid PRIMARY KEY REFERENCES entry(id) ON DELETE CASCADE,
  clinic_id uuid NOT NULL REFERENCES clinic(id),           -- added for RLS scoping (see note)
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
CREATE INDEX IF NOT EXISTS idx_ais_type   ON ai_scribed_note(ai_note_type);
CREATE INDEX IF NOT EXISTS idx_ais_clinic ON ai_scribed_note(clinic_id);

-- -----------------------------------------------------------------------------
-- 3.8 task (assignment)  (DATA_SCHEMA §3.8)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS task (
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
CREATE INDEX IF NOT EXISTS idx_task_patient_open ON task(patient_id, status, due_at) WHERE status IN ('open','in_progress');
CREATE INDEX IF NOT EXISTS idx_task_assignee     ON task(assignee_id, status) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_task_clinic       ON task(clinic_id);

-- -----------------------------------------------------------------------------
-- 3.9 mention  (DATA_SCHEMA §3.9)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mention (
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
CREATE INDEX IF NOT EXISTS idx_mention_unread ON mention(mentioned_user_id, read_at) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mention_clinic ON mention(clinic_id);

-- -----------------------------------------------------------------------------
-- 3.10 entry_entity (tagged clinical entities → risk + learning)  (DATA_SCHEMA §3.10)
--
-- NOTE (deviation): `clinic_id` added for RLS scoping (same reason as
-- entry_version / ai_scribed_note — guiding decision #1 requires clinic_id on
-- every patient-scoped table so RLS can filter by app_clinic_id()).
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS entry_entity (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_id uuid NOT NULL REFERENCES entry(id) ON DELETE CASCADE,
  clinic_id uuid NOT NULL REFERENCES clinic(id),           -- added for RLS scoping (see note)
  entity_type text NOT NULL,                     -- medication | chief_complaint | allergy | symptom | ...
  entity_value text NOT NULL,                    -- normalized (lowercased/canonical)
  offset_start int, offset_end int,
  created_by text NOT NULL DEFAULT 'ai' CHECK (created_by IN ('ai','rule','manual')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entry_id, entity_type, entity_value)
);
CREATE INDEX IF NOT EXISTS idx_ee_value ON entry_entity(entity_type, entity_value);
CREATE INDEX IF NOT EXISTS idx_ee_clinic ON entry_entity(clinic_id);

-- -----------------------------------------------------------------------------
-- 3.11 learning_interaction + learning_weight (persisted self-learning)  (DATA_SCHEMA §3.11)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS learning_interaction (
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
CREATE INDEX IF NOT EXISTS idx_li_time ON learning_interaction(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_li_clinic ON learning_interaction(clinic_id);

CREATE TABLE IF NOT EXISTS learning_weight (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  clinic_id uuid NOT NULL REFERENCES clinic(id) ON DELETE CASCADE,
  feature_key text NOT NULL,        -- e.g. 'entity:medication:amoxicillin', 'keyword:asthma', 'section:plan'
  weight numeric NOT NULL DEFAULT 0, -- learned boost, clamped to [-1, +2] via trigger
  positive_count int NOT NULL DEFAULT 0,
  negative_count int NOT NULL DEFAULT 0,
  total_interactions int NOT NULL DEFAULT 0,
  last_seen_at timestamptz NOT NULL DEFAULT now(), -- used for time-decay of weight
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (clinic_id, feature_key)
);
CREATE INDEX IF NOT EXISTS idx_lw_key    ON learning_weight(feature_key);
CREATE INDEX IF NOT EXISTS idx_lw_clinic ON learning_weight(clinic_id);

-- -----------------------------------------------------------------------------
-- 3.12 audit_log (metadata ONLY)  (DATA_SCHEMA §3.12)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_log (
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
CREATE INDEX IF NOT EXISTS idx_audit_patient_time ON audit_log(patient_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_clinic_time  ON audit_log(clinic_id, occurred_at DESC);

-- -----------------------------------------------------------------------------
-- 3.13 patient_glance (precomputed top-card for ≤300ms)  (DATA_SCHEMA §3.13)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS patient_glance (
  patient_id uuid PRIMARY KEY REFERENCES patient(id) ON DELETE CASCADE,
  clinic_id uuid NOT NULL REFERENCES clinic(id),
  top_items jsonb NOT NULL DEFAULT '[]',    -- [{item_type,item_id,preview,score,risk_reason}]
  open_actions jsonb NOT NULL DEFAULT '[]', -- [{kind:'task'|'comment'|'highlight'|'conflict', id, label}]
  risk_flags jsonb NOT NULL DEFAULT '[]',
  computed_at timestamptz NOT NULL DEFAULT now(),
  invalidated boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_glance_clinic ON patient_glance(clinic_id);

-- -----------------------------------------------------------------------------
-- 3.14 data-decay archives  (DATA_SCHEMA §3.14)
--
-- NOTE (deviation): `entry_version_archive` also gains `clinic_id` so RLS can
-- scope archived versions. Archive rows are cold copies — deliberately NO FK to
-- clinic/entry so decay never blocks on referential cleanup.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS entry_archive (                    -- cold copies of decayed entry bodies
  entry_id uuid PRIMARY KEY,                    -- same id as the stub in entry
  patient_id uuid NOT NULL,
  clinic_id uuid NOT NULL,
  body text NOT NULL,                           -- zstd-compressed column
  section section_key,
  entry_type entry_type,
  provenance_id uuid,
  archived_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS entry_version_archive (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_id uuid NOT NULL,
  clinic_id uuid NOT NULL,                      -- added for RLS scoping (see note)
  version int NOT NULL,
  body text NOT NULL,
  author_role entry_author_role NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_arch_patient ON entry_archive(patient_id);
CREATE INDEX IF NOT EXISTS idx_arch_entry   ON entry_archive(entry_id);
CREATE INDEX IF NOT EXISTS idx_archv_entry  ON entry_version_archive(entry_id, version);
CREATE INDEX IF NOT EXISTS idx_archv_clinic ON entry_version_archive(clinic_id);

-- =============================================================================
-- 4. Column compression on cold body columns
--    pglz is Postgres' built-in default and works on EVERY build (the alpine
--    official image does not ship zstd). For a 72-hour build, portability wins
--    over zstd's better ratio on large text blobs.
-- =============================================================================

ALTER TABLE entry               ALTER COLUMN body SET COMPRESSION pglz;
ALTER TABLE entry_version       ALTER COLUMN body SET COMPRESSION pglz;
ALTER TABLE entry_archive       ALTER COLUMN body SET COMPRESSION pglz;
ALTER TABLE entry_version_archive ALTER COLUMN body SET COMPRESSION pglz;

-- =============================================================================
-- 5. Data-integrity triggers
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 5.1 ai_scribed_note must be 1:1 with an entry whose author_role='system' and
-- whose entry_type matches the ai_note_type.  (DATA_SCHEMA §3.7)
--
-- SECURITY DEFINER because the trigger reads `entry`, which the mutating
-- system_pipeline role has no SELECT grant on (and RLS would scope anyway).
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION trg_ai_note_type_matches_entry()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  e_author entry_author_role;
  e_type entry_type;
BEGIN
  SELECT author_role, entry_type INTO e_author, e_type
    FROM entry WHERE id = NEW.entry_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'ai_scribed_note.entry_id % does not reference an entry', NEW.entry_id;
  END IF;
  IF e_author <> 'system' THEN
    RAISE EXCEPTION 'ai_scribed_note requires entry.author_role=system (got %)', e_author;
  END IF;
  IF e_type::text <> NEW.ai_note_type::text THEN
    RAISE EXCEPTION 'ai_note_type % does not match entry.entry_type %', NEW.ai_note_type, e_type;
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS ai_note_type_matches_entry ON ai_scribed_note;
CREATE TRIGGER ai_note_type_matches_entry
  BEFORE INSERT OR UPDATE OF ai_note_type ON ai_scribed_note
  FOR EACH ROW EXECUTE FUNCTION trg_ai_note_type_matches_entry();

-- -----------------------------------------------------------------------------
-- 5.2 task assignee/assigner must belong to the task's clinic.  (DATA_SCHEMA §3.8)
--
-- SECURITY DEFINER because it reads `users`, which RLS would otherwise scope to
-- the mutating role's own clinic — silently hiding a cross-clinic assignee and
-- defeating the check.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION trg_task_assignee_in_clinic()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM users u WHERE u.id = NEW.assignee_id AND u.clinic_id <> NEW.clinic_id) THEN
    RAISE EXCEPTION 'task assignee must belong to the task clinic';
  END IF;
  IF NEW.assigner_id IS NOT NULL
     AND EXISTS (SELECT 1 FROM users u WHERE u.id = NEW.assigner_id AND u.clinic_id <> NEW.clinic_id) THEN
    RAISE EXCEPTION 'task assigner must belong to the task clinic';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS task_assignee_in_clinic ON task;
CREATE TRIGGER task_assignee_in_clinic
  BEFORE INSERT OR UPDATE OF assignee_id, assigner_id, clinic_id ON task
  FOR EACH ROW EXECUTE FUNCTION trg_task_assignee_in_clinic();

-- -----------------------------------------------------------------------------
-- 5.3 highlight provenance/risk fields are immutable.  (DATA_SCHEMA §4.4)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION trg_highlight_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.entry_id        IS DISTINCT FROM OLD.entry_id
     OR NEW.risk_reason   IS DISTINCT FROM OLD.risk_reason
     OR NEW.provenance_id IS DISTINCT FROM OLD.provenance_id
     OR NEW.offset_start  <> OLD.offset_start
     OR NEW.offset_end    <> OLD.offset_end
     OR NEW.quoted_text   IS DISTINCT FROM OLD.quoted_text THEN
    RAISE EXCEPTION 'highlight provenance/risk fields are immutable';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS highlight_immutable ON highlight;
CREATE TRIGGER highlight_immutable
  BEFORE UPDATE ON highlight
  FOR EACH ROW EXECUTE FUNCTION trg_highlight_immutable();

-- -----------------------------------------------------------------------------
-- 5.4 learning_weight.weight is clamped to [-1, +2].  (DATA_SCHEMA §3.11)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION trg_clamp_learning_weight()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.weight := GREATEST(-1.0, LEAST(2.0, NEW.weight));
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS clamp_learning_weight ON learning_weight;
CREATE TRIGGER clamp_learning_weight
  BEFORE INSERT OR UPDATE OF weight ON learning_weight
  FOR EACH ROW EXECUTE FUNCTION trg_clamp_learning_weight();

-- -----------------------------------------------------------------------------
-- 5.5 audit_log is metadata-only: reject any JSON key named body/content.  (DATA_SCHEMA §3.12)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION trg_audit_no_content()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.metadata ? 'body' OR NEW.metadata ? 'content'
     OR NEW.metadata ? 'text' OR NEW.metadata ? 'note' THEN
    RAISE EXCEPTION 'audit_log.metadata must hold ids/state transitions only, never content';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS audit_no_content ON audit_log;
CREATE TRIGGER audit_no_content
  BEFORE INSERT OR UPDATE OF metadata ON audit_log
  FOR EACH ROW EXECUTE FUNCTION trg_audit_no_content();

-- -----------------------------------------------------------------------------
-- 5.6 audit_log is append-only: no UPDATE/DELETE, not even for admins/superusers.
--     (SECURITY.md §g — no UPDATE/DELETE policies, hash-chain tamper evidence)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION trg_audit_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'audit_log is append-only (DELETE forbidden)';
  ELSIF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION 'audit_log is append-only (UPDATE forbidden)';
  END IF;
  RETURN NULL; -- unreachable for INSERT (this trigger is not created for INSERT)
END $$;

DROP TRIGGER IF EXISTS audit_append_only ON audit_log;
CREATE TRIGGER audit_append_only
  BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION trg_audit_append_only();

RESET ROLE;
