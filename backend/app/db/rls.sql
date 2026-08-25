-- =============================================================================
-- Nightingale — rls.sql
--
-- Row-Level Security + server-side access control for the Nightingale data
-- layer. Source of truth: docs/DATA_SCHEMA.md §4 and docs/SECURITY.md §a.
--
-- What lives here:
--   1. Session-context helper functions (app.user_id / app.role / app.clinic_id
--      GUCs set per-transaction from the verified JWT)
--   2. Table-level DML grants (issued here because the tables only exist after
--      schema.sql runs)
--   3. ENABLE ROW LEVEL SECURITY + FORCE ROW LEVEL SECURITY on EVERY row table
--      (clinic, users and patient included). FORCE applies RLS even to the
--      non-superuser table owner; superusers and roles with BYPASSRLS still
--      bypass RLS. The defense is that the app never connects as the owner or
--      a superuser — only as the restricted app_nightingale role via SET ROLE.
--   4. USING / WITH CHECK policies for patient/staff/clinician/admin (+ the
--      system_pipeline AI-ingestion role), clinic-scoped via app_clinic_id(),
--      with the write-isolation rule: staff writes staff_notes only,
--      clinician writes clinician_sections only, and neither can overwrite the
--      other (row-level: UPDATE policies require matching author_role)
--   5. SECURITY DEFINER functions (log_audit, save_entry, resolve_conflict,
--      revert_entry, record_interaction, update_weights,
--      recompute_glance, decay_old_entries, restore_entry). These run as the
--      non-login owner nightingale_owner and therefore bypass RLS — they must
--      (and do) validate clinic_id / patient_id / author_role explicitly.
--   6. Glance invalidation trigger (marks patient_glance stale on mutation)
-- -----------------------------------------------------------------------------

SET ROLE nightingale_owner;

-- =============================================================================
-- 1. Session-context helper functions  (DATA_SCHEMA §4.1)
--
-- The middleware runs, after JWT verification and inside the request
-- transaction:
--   SET LOCAL app.user_id = '<verified user id>';
--   SET LOCAL app.role    = '<verified role>';
--   SET LOCAL app.clinic_id = '<verified clinic id>';
-- GUCs come only from the verified token — never from client input.
-- =============================================================================

CREATE OR REPLACE FUNCTION app_user_id() RETURNS uuid
LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.user_id', true),'')::uuid $$;

CREATE OR REPLACE FUNCTION app_role() RETURNS text
LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.role', true),'') $$;

CREATE OR REPLACE FUNCTION app_clinic_id() RETURNS uuid
LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.clinic_id', true),'')::uuid $$;

-- patient-scoped id: returns the patient row for the logged-in patient, else NULL
CREATE OR REPLACE FUNCTION app_patient_id() RETURNS uuid
LANGUAGE sql STABLE AS
$$ SELECT id FROM patient WHERE user_id = app_user_id() $$;

-- which role owns a section (drives write-isolation); NULL section → standalone
-- timeline items are clinician-owned by default
CREATE OR REPLACE FUNCTION section_role_of(s section_key) RETURNS user_role
LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE s WHEN 'staff_handoff' THEN 'staff'::user_role ELSE 'clinician'::user_role END $$;

-- helper for SECURITY DEFINER functions: the clinic a user belongs to
CREATE OR REPLACE FUNCTION user_clinic_id(p_user_id uuid) RETURNS uuid
LANGUAGE sql STABLE AS
$$ SELECT clinic_id FROM users WHERE id = p_user_id $$;

-- =============================================================================
-- 2. Table-level DML grants (the tables exist after schema.sql)
--
-- RLS filters ROWS; the grants gate TABLE access. Grants mirror the policy
-- matrix in docs/SECURITY.md §a. Note: no INSERT/UPDATE/DELETE is ever granted
-- on audit_log, learning_*, or patient_glance to any role — those writes flow
-- exclusively through SECURITY DEFINER functions below.
-- =============================================================================

-- ---- patient_role: own record + own patient-facing content
GRANT SELECT ON clinic, users, patient TO patient_role;
GRANT SELECT, INSERT ON entry, comment TO patient_role;

-- ---- staff_role: clinic-scope reads everywhere; writes only their own role rows
GRANT SELECT ON clinic, users, patient, provenance, entry, comment, entry_version,
  highlight, ai_scribed_note, task, mention, entry_entity, learning_interaction,
  learning_weight, patient_glance, entry_archive, entry_version_archive TO staff_role;
GRANT INSERT, UPDATE ON entry, comment, task, mention TO staff_role;
GRANT SELECT, INSERT ON highlight TO staff_role;
GRANT UPDATE (status, resolved_by, resolved_at) ON highlight TO staff_role;

-- ---- clinician_role: same shape as staff (clinician section ownership)
GRANT SELECT ON clinic, users, patient, provenance, entry, comment, entry_version,
  highlight, ai_scribed_note, task, mention, entry_entity, learning_interaction,
  learning_weight, patient_glance, entry_archive, entry_version_archive TO clinician_role;
GRANT INSERT, UPDATE ON entry, comment, task, mention TO clinician_role;
GRANT SELECT, INSERT ON highlight TO clinician_role;
GRANT UPDATE (status, resolved_by, resolved_at) ON highlight TO clinician_role;

-- ---- admin_role: clinic-scoped oversight (read all; write within the clinic).
-- NOTE: admin gets NO DML on learning_* / patient_glance — those are writable
-- only by SECURITY DEFINER functions (DATA_SCHEMA §4.6). audit_log is admin-read
-- only, written exclusively via log_audit().
GRANT SELECT ON clinic, users, patient, provenance, entry, comment, entry_version,
  highlight, ai_scribed_note, task, mention, entry_entity, learning_interaction,
  learning_weight, patient_glance, entry_archive, entry_version_archive, audit_log
  TO admin_role;
GRANT INSERT, UPDATE, DELETE ON clinic, users, patient, provenance, entry, comment,
  entry_version, highlight, task, mention, entry_entity, entry_archive,
  entry_version_archive TO admin_role;

-- ---- system_pipeline: AI ingestion only
GRANT SELECT ON clinic, users, patient, learning_weight TO system_pipeline;
GRANT INSERT ON entry, provenance, ai_scribed_note, highlight, entry_entity TO system_pipeline;

-- =============================================================================
-- 3. ENABLE + FORCE row-level security on EVERY row table
--
-- FORCE ROW LEVEL SECURITY applies RLS even to the (non-superuser) table owner.
-- It does NOT restrict superusers or roles with BYPASSRLS — the protection is
-- that the app never connects as the owner or a superuser (only as the
-- restricted app_nightingale role via SET ROLE), and migrations run as the
-- owner so DDL stays outside the runtime trust boundary.
-- =============================================================================

ALTER TABLE clinic              ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE users               ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE patient             ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE provenance          ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE entry               ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE comment             ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE entry_version       ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE highlight           ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE ai_scribed_note     ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE task                ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE mention             ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE entry_entity        ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE learning_interaction ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE learning_weight     ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_log           ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE patient_glance      ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE entry_archive       ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
ALTER TABLE entry_version_archive ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;

-- =============================================================================
-- 4. Policies  (DATA_SCHEMA §4.2–§4.6)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- clinic / users / patient — reference tables, RLS'd for clinic/self scoping
-- (stronger than DATA_SCHEMA §4.1, which omitted these three from the ALTER
-- list; the task instruction "RLS on every row table" + SYNTHESIS #4 win here)
-- -----------------------------------------------------------------------------

CREATE POLICY clinic_sel ON clinic FOR SELECT TO patient_role, staff_role, clinician_role
  USING (id = app_clinic_id());
CREATE POLICY clinic_sel_admin ON clinic FOR SELECT TO admin_role
  USING (id = app_clinic_id());
CREATE POLICY clinic_sel_pipeline ON clinic FOR SELECT TO system_pipeline
  USING (id = app_clinic_id());

CREATE POLICY users_sel_own ON users FOR SELECT TO patient_role
  USING (id = app_user_id());
CREATE POLICY users_sel_staff ON users FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY users_sel_clin ON users FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY users_sel_pipeline ON users FOR SELECT TO system_pipeline
  USING (clinic_id = app_clinic_id());
CREATE POLICY users_all_admin ON users FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());

CREATE POLICY patient_sel_own ON patient FOR SELECT TO patient_role
  USING (user_id = app_user_id());
CREATE POLICY patient_sel_staff ON patient FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY patient_sel_clin ON patient FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY patient_sel_pipeline ON patient FOR SELECT TO system_pipeline
  USING (clinic_id = app_clinic_id());
CREATE POLICY patient_all_admin ON patient FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());

-- -----------------------------------------------------------------------------
-- provenance — clinic-scope read for clinical roles; pipeline writes  (§4.5)
-- -----------------------------------------------------------------------------

CREATE POLICY prov_sel_staff ON provenance FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY prov_sel_clin ON provenance FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY prov_all_admin ON provenance FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());
CREATE POLICY prov_ins_system ON provenance FOR INSERT TO system_pipeline
  WITH CHECK (clinic_id = app_clinic_id());

-- -----------------------------------------------------------------------------
-- entry — the write-isolation core  (§4.2)
-- Staff: clinic-scope read of ALL entries; write ONLY staff_notes in the
-- staff-owned section. Clinician: clinic-scope read; write ONLY clinician
-- sections. Neither can overwrite the other: the UPDATE policy's USING requires
-- `author_role = <own role>`, so a clinician's UPDATE can never see a staff row
-- (0 rows matched) and vice versa; the WITH CHECK section rule blocks it even if
-- the row somehow matched.
-- -----------------------------------------------------------------------------

-- PATIENT: own patient only, patient-facing content only
CREATE POLICY entry_sel_patient ON entry FOR SELECT TO patient_role
  USING (patient_id = app_patient_id() AND visibility = 'patient_visible');
CREATE POLICY entry_ins_patient ON entry FOR INSERT TO patient_role
  WITH CHECK (patient_id = app_patient_id()
              AND author_role = 'patient'
              AND visibility = 'patient_visible');

-- STAFF
CREATE POLICY entry_sel_staff ON entry FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY entry_ins_staff ON entry FOR INSERT TO staff_role
  WITH CHECK (clinic_id = app_clinic_id()
              AND author_role = 'staff'
              AND entry_type = 'staff_note'
              AND section_role_of(section) = 'staff'
              AND EXISTS (SELECT 1 FROM patient p
                          WHERE p.id = patient_id AND p.clinic_id = app_clinic_id()));
CREATE POLICY entry_upd_staff ON entry FOR UPDATE TO staff_role
  USING (clinic_id = app_clinic_id() AND author_role = 'staff')
  WITH CHECK (clinic_id = app_clinic_id()
              AND author_role = 'staff'
              AND section_role_of(section) = 'staff');

-- CLINICIAN
CREATE POLICY entry_sel_clin ON entry FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY entry_ins_clin ON entry FOR INSERT TO clinician_role
  WITH CHECK (clinic_id = app_clinic_id()
              AND author_role = 'clinician'
              AND section_role_of(section) = 'clinician'
              AND EXISTS (SELECT 1 FROM patient p
                          WHERE p.id = patient_id AND p.clinic_id = app_clinic_id()));
CREATE POLICY entry_upd_clin ON entry FOR UPDATE TO clinician_role
  USING (clinic_id = app_clinic_id() AND author_role = 'clinician')
  WITH CHECK (clinic_id = app_clinic_id()
              AND author_role = 'clinician'
              AND section_role_of(section) = 'clinician');

-- ADMIN: clinic-scoped oversight
CREATE POLICY entry_all_admin ON entry FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id())
  WITH CHECK (clinic_id = app_clinic_id());

-- SYSTEM: AI ingestion writes author_role='system'
CREATE POLICY entry_ins_system ON entry FOR INSERT TO system_pipeline
  WITH CHECK (clinic_id = app_clinic_id()
              AND author_role = 'system'
              AND EXISTS (SELECT 1 FROM patient p
                          WHERE p.id = patient_id AND p.clinic_id = app_clinic_id()));

-- -----------------------------------------------------------------------------
-- comment — thread + resolve state  (§4.3)
-- -----------------------------------------------------------------------------

-- patient: only patient_visible comments on own patient; may post own comments
CREATE POLICY comment_sel_patient ON comment FOR SELECT TO patient_role
  USING (patient_id = app_patient_id() AND visibility = 'patient_visible');
CREATE POLICY comment_ins_patient ON comment FOR INSERT TO patient_role
  WITH CHECK (patient_id = app_patient_id() AND author_role='patient' AND visibility='patient_visible');

-- staff/clinician: clinic scope read; may post as their own role; resolve own-clinic comments
CREATE POLICY comment_sel_staff ON comment FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY comment_sel_clin  ON comment FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY comment_ins_staff ON comment FOR INSERT TO staff_role
  WITH CHECK (clinic_id = app_clinic_id()
              AND author_role='staff'
              AND EXISTS (SELECT 1 FROM patient p WHERE p.id = patient_id AND p.clinic_id = app_clinic_id())
              AND EXISTS (SELECT 1 FROM entry e WHERE e.id = entry_id AND e.clinic_id = app_clinic_id()));
CREATE POLICY comment_ins_clin  ON comment FOR INSERT TO clinician_role
  WITH CHECK (clinic_id = app_clinic_id()
              AND author_role='clinician'
              AND EXISTS (SELECT 1 FROM patient p WHERE p.id = patient_id AND p.clinic_id = app_clinic_id())
              AND EXISTS (SELECT 1 FROM entry e WHERE e.id = entry_id AND e.clinic_id = app_clinic_id()));
-- resolve/unresolve allowed for staff+clinician within clinic (status, resolved_at, resolved_by).
-- WITH CHECK mirrors USING so a resolver cannot rewrite a comment's author_id /
-- author_role across roles while resolving it (resolve-by-other is still allowed:
-- the guard only blocks setting the author_role to the OTHER clinical role).
CREATE POLICY comment_upd_staff ON comment FOR UPDATE TO staff_role
  USING (clinic_id = app_clinic_id() AND author_role <> 'clinician')
  WITH CHECK (clinic_id = app_clinic_id() AND author_role <> 'clinician');
CREATE POLICY comment_upd_clin  ON comment FOR UPDATE TO clinician_role
  USING (clinic_id = app_clinic_id() AND author_role <> 'staff')
  WITH CHECK (clinic_id = app_clinic_id() AND author_role <> 'staff');

CREATE POLICY comment_all_admin ON comment FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());

-- -----------------------------------------------------------------------------
-- entry_version — version history is clinical-only (patient gets no policy)  (§4.5)
-- INSERTs flow through the SECURITY DEFINER save_entry()/revert_entry(), never
-- a direct INSERT by a user role.
-- -----------------------------------------------------------------------------

CREATE POLICY ev_sel_staff ON entry_version FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY ev_sel_clin  ON entry_version FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY ev_all_admin ON entry_version FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());

-- -----------------------------------------------------------------------------
-- highlight — internal; patient gets no policy → invisible  (§4.4)
-- -----------------------------------------------------------------------------

CREATE POLICY hl_sel_staff ON highlight FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY hl_sel_clin  ON highlight FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY hl_all_admin ON highlight FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());
-- manual highlights by staff/clinician (source='manual'); AI/rule suggestions by pipeline
CREATE POLICY hl_ins_staff ON highlight FOR INSERT TO staff_role
  WITH CHECK (clinic_id = app_clinic_id() AND source='manual' AND created_by = app_user_id()
              AND EXISTS (SELECT 1 FROM patient p WHERE p.id = patient_id AND p.clinic_id = app_clinic_id())
              AND EXISTS (SELECT 1 FROM entry e WHERE e.id = entry_id AND e.clinic_id = app_clinic_id()));
CREATE POLICY hl_ins_clin  ON highlight FOR INSERT TO clinician_role
  WITH CHECK (clinic_id = app_clinic_id() AND source='manual' AND created_by = app_user_id()
              AND EXISTS (SELECT 1 FROM patient p WHERE p.id = patient_id AND p.clinic_id = app_clinic_id())
              AND EXISTS (SELECT 1 FROM entry e WHERE e.id = entry_id AND e.clinic_id = app_clinic_id()));
CREATE POLICY hl_ins_system ON highlight FOR INSERT TO system_pipeline
  WITH CHECK (clinic_id = app_clinic_id() AND source IN ('ai','rule')
              AND EXISTS (SELECT 1 FROM patient p WHERE p.id = patient_id AND p.clinic_id = app_clinic_id())
              AND EXISTS (SELECT 1 FROM entry e WHERE e.id = entry_id AND e.clinic_id = app_clinic_id()));
-- accept/reject: staff & clinician may update status only; content is protected
-- below via column grants + the trg_highlight_immutable trigger
CREATE POLICY hl_upd_staff ON highlight FOR UPDATE TO staff_role
  USING (clinic_id = app_clinic_id() AND status='suggested');
CREATE POLICY hl_upd_clin  ON highlight FOR UPDATE TO clinician_role
  USING (clinic_id = app_clinic_id() AND status='suggested');

-- Content protection (defense in depth, since RLS is row-level):
REVOKE UPDATE ON highlight FROM patient_role;
GRANT  UPDATE (status, resolved_by, resolved_at) ON highlight TO staff_role, clinician_role;

-- -----------------------------------------------------------------------------
-- ai_scribed_note — clinical-only; NO patient policy → patients never read raw
-- AI notes  (§4.5). Writes are by the pipeline.
-- -----------------------------------------------------------------------------

CREATE POLICY ais_sel_staff ON ai_scribed_note FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY ais_sel_clin  ON ai_scribed_note FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY ais_all_admin ON ai_scribed_note FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());
CREATE POLICY ais_ins_system ON ai_scribed_note FOR INSERT TO system_pipeline
  WITH CHECK (clinic_id = app_clinic_id());

-- -----------------------------------------------------------------------------
-- task — clinical roles read/write within clinic; assignee_in_clinic trigger  (§4.5)
-- -----------------------------------------------------------------------------

CREATE POLICY task_sel_staff ON task FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY task_sel_clin  ON task FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY task_ins_staff ON task FOR INSERT TO staff_role
  WITH CHECK (clinic_id = app_clinic_id()
              AND EXISTS (SELECT 1 FROM patient p WHERE p.id = patient_id AND p.clinic_id = app_clinic_id())
              AND (entry_id IS NULL OR EXISTS (SELECT 1 FROM entry e WHERE e.id = entry_id AND e.clinic_id = app_clinic_id())));
CREATE POLICY task_ins_clin  ON task FOR INSERT TO clinician_role
  WITH CHECK (clinic_id = app_clinic_id()
              AND EXISTS (SELECT 1 FROM patient p WHERE p.id = patient_id AND p.clinic_id = app_clinic_id())
              AND (entry_id IS NULL OR EXISTS (SELECT 1 FROM entry e WHERE e.id = entry_id AND e.clinic_id = app_clinic_id())));
CREATE POLICY task_upd_staff ON task FOR UPDATE TO staff_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());
CREATE POLICY task_upd_clin  ON task FOR UPDATE TO clinician_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());
CREATE POLICY task_all_admin ON task FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());

-- -----------------------------------------------------------------------------
-- mention — clinic-scope read + the mentioned user's own row; INSERT within clinic
-- -----------------------------------------------------------------------------

CREATE POLICY mention_sel_staff ON mention FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY mention_sel_clin  ON mention FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY mention_sel_admin ON mention FOR SELECT TO admin_role
  USING (clinic_id = app_clinic_id());
-- a user can always see (and mark read) mentions addressed to themselves
CREATE POLICY mention_sel_own ON mention FOR SELECT TO patient_role, staff_role, clinician_role, admin_role
  USING (mentioned_user_id = app_user_id());
CREATE POLICY mention_upd_own ON mention FOR UPDATE TO patient_role, staff_role, clinician_role, admin_role
  USING (mentioned_user_id = app_user_id()) WITH CHECK (mentioned_user_id = app_user_id());
CREATE POLICY mention_ins_staff ON mention FOR INSERT TO staff_role
  WITH CHECK (clinic_id = app_clinic_id()
              AND EXISTS (SELECT 1 FROM users u WHERE u.id = mentioned_user_id AND u.clinic_id = app_clinic_id())
              AND (comment_id IS NULL OR EXISTS (SELECT 1 FROM comment c WHERE c.id = comment_id AND c.clinic_id = app_clinic_id()))
              AND (entry_id IS NULL OR EXISTS (SELECT 1 FROM entry e WHERE e.id = entry_id AND e.clinic_id = app_clinic_id())));
CREATE POLICY mention_ins_clin  ON mention FOR INSERT TO clinician_role
  WITH CHECK (clinic_id = app_clinic_id()
              AND EXISTS (SELECT 1 FROM users u WHERE u.id = mentioned_user_id AND u.clinic_id = app_clinic_id())
              AND (comment_id IS NULL OR EXISTS (SELECT 1 FROM comment c WHERE c.id = comment_id AND c.clinic_id = app_clinic_id()))
              AND (entry_id IS NULL OR EXISTS (SELECT 1 FROM entry e WHERE e.id = entry_id AND e.clinic_id = app_clinic_id())));

-- -----------------------------------------------------------------------------
-- entry_entity — clinical read; pipeline writes  (§4.5)
-- -----------------------------------------------------------------------------

CREATE POLICY ee_sel_staff ON entry_entity FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY ee_sel_clin  ON entry_entity FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY ee_all_admin ON entry_entity FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());
CREATE POLICY ee_ins_system ON entry_entity FOR INSERT TO system_pipeline
  WITH CHECK (clinic_id = app_clinic_id());

-- -----------------------------------------------------------------------------
-- learning tables — readable by clinical roles, writable ONLY by SECURITY
-- DEFINER functions (record_interaction / update_weights). No
-- INSERT/UPDATE/DELETE grant or policy exists for any user role  (§4.6)
-- -----------------------------------------------------------------------------

CREATE POLICY lw_sel_staff ON learning_weight FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY lw_sel_clin  ON learning_weight FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY lw_sel_admin ON learning_weight FOR SELECT TO admin_role
  USING (clinic_id = app_clinic_id());
-- The pipeline reads learning_weight (via record_interaction/update_weights, which
-- run SECURITY DEFINER and bypass RLS anyway), so give it a clinic-scoped SELECT
-- policy for any non-SECURITY-DEFINER read it might perform.
CREATE POLICY lw_sel_pipeline ON learning_weight FOR SELECT TO system_pipeline
  USING (clinic_id = app_clinic_id());
CREATE POLICY li_sel_staff ON learning_interaction FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY li_sel_clin  ON learning_interaction FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY li_sel_admin ON learning_interaction FOR SELECT TO admin_role
  USING (clinic_id = app_clinic_id());

-- -----------------------------------------------------------------------------
-- audit_log — admin-only reads; writes via SECURITY DEFINER log_audit(); the
-- schema.sql triggers make it metadata-only + append-only  (§4.6)
-- -----------------------------------------------------------------------------

CREATE POLICY audit_sel_admin ON audit_log FOR SELECT TO admin_role
  USING (clinic_id = app_clinic_id());

-- -----------------------------------------------------------------------------
-- patient_glance — clinical read; recomputed via SECURITY DEFINER  (§4.6)
-- -----------------------------------------------------------------------------

CREATE POLICY glance_sel_staff ON patient_glance FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY glance_sel_clin  ON patient_glance FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY glance_sel_admin ON patient_glance FOR SELECT TO admin_role
  USING (clinic_id = app_clinic_id());

-- -----------------------------------------------------------------------------
-- data-decay archives — clinical read; written via SECURITY DEFINER decay_old_entries()
-- -----------------------------------------------------------------------------

CREATE POLICY arch_sel_staff ON entry_archive FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY arch_sel_clin  ON entry_archive FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY arch_all_admin ON entry_archive FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());
CREATE POLICY archv_sel_staff ON entry_version_archive FOR SELECT TO staff_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY archv_sel_clin  ON entry_version_archive FOR SELECT TO clinician_role
  USING (clinic_id = app_clinic_id());
CREATE POLICY archv_all_admin ON entry_version_archive FOR ALL TO admin_role
  USING (clinic_id = app_clinic_id()) WITH CHECK (clinic_id = app_clinic_id());

-- =============================================================================
-- 5. SECURITY DEFINER functions
--
-- Owned by nightingale_owner (a NOLOGIN superuser) → they bypass RLS, so each
-- one explicitly validates clinic_id / patient_id / author_role before writing.
-- EXECUTE is revoked from PUBLIC and granted only to the roles that may call
-- each function (patients, for example, can never invoke save_entry).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 5.1 log_audit — the only way audit_log rows are created
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION log_audit(
  p_actor_id uuid,
  p_actor_role entry_author_role,
  p_action audit_action,
  p_target_type text,
  p_target_id uuid,
  p_patient_id uuid,
  p_clinic_id uuid,
  p_version int DEFAULT NULL,
  p_metadata jsonb DEFAULT '{}'
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_clinic uuid;
BEGIN
  v_clinic := COALESCE(p_clinic_id, app_clinic_id());
  IF v_clinic IS NULL THEN
    RAISE EXCEPTION 'log_audit requires a clinic_id (caller or GUC)';
  END IF;
  INSERT INTO audit_log(clinic_id, patient_id, actor_id, actor_role, action,
                        target_type, target_id, version, metadata, occurred_at)
  VALUES (v_clinic, p_patient_id, p_actor_id, p_actor_role, p_action,
          p_target_type, p_target_id, p_version, COALESCE(p_metadata, '{}'::jsonb), now());
END $$;

-- -----------------------------------------------------------------------------
-- 5.2 char_diff — minimal char-level diff (optional delta optimization; the
-- two-snapshot diff is always available and is the source of truth)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION char_diff(p_old_body text, p_new_body text) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
  o text := COALESCE(p_old_body, '');
  n text := COALESCE(p_new_body, '');
  i int := 1;
  j int := 0;
  lo int;
  ln int;
  removed text;
  added text;
BEGIN
  IF o = n THEN RETURN '{"equal": true}'::jsonb; END IF;
  lo := char_length(o);
  ln := char_length(n);
  -- longest common prefix
  WHILE i <= lo AND i <= ln AND substring(o FROM i FOR 1) = substring(n FROM i FOR 1) LOOP
    i := i + 1;
  END LOOP;
  -- longest common suffix (must not overlap the prefix)
  WHILE j < (lo - i + 1) AND j < (ln - i + 1)
        AND substring(o FROM lo - j FOR 1) = substring(n FROM ln - j FOR 1) LOOP
    j := j + 1;
  END LOOP;
  removed := substring(o FROM i FOR (lo - i + 1) - j);
  added   := substring(n FROM i FOR (ln - i + 1) - j);
  RETURN jsonb_build_object(
    'unchanged_prefix', i - 1,
    'unchanged_suffix', j,
    'removed', removed,
    'added', added,
    'old_length', lo,
    'new_length', ln
  );
END $$;

-- -----------------------------------------------------------------------------
-- 5.3 save_entry — the single transactional save path  (DATA_SCHEMA §5)
-- Optimistic lock on entry.version + section advisory lock; deterministic
-- conflict handling via resolve_conflict. Mirrors the RLS policy clauses
-- (author_role + section ownership) because RLS does NOT protect SECURITY
-- DEFINER. New current versions are allocated as max(entry_version)+1 so a
-- flagged conflict child never collides with the next legitimate edit.
--
-- M2 integration point: this function trusts caller-passed p_user_id /
-- p_user_role (it validates them against the row, but does not read the GUCs),
-- so the API must invoke it only from a SET ROLE'd session whose GUCs match the
-- verified token — see app/db/session.py set_app_context.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION save_entry(
  p_entry_id uuid,
  p_new_body text,
  p_base_version int,
  p_user_id uuid,
  p_user_role entry_author_role
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  r record;
  v_new int;
  v_delta jsonb;
  v_writer_clinic uuid;
BEGIN
  IF p_user_role = 'system' THEN
    RAISE EXCEPTION 'save_entry is not for system-authored entries (use the pipeline insert path)';
  END IF;
  SELECT clinic_id INTO v_writer_clinic FROM users WHERE id = p_user_id;
  IF v_writer_clinic IS NULL THEN RAISE EXCEPTION 'unknown user %', p_user_id; END IF;

  -- Serialize same-section commits with a section-level advisory lock, then lock
  -- the row so the check-and-write is atomic.
  PERFORM pg_advisory_xact_lock(hashtext('section:' ||
    (SELECT patient_id FROM entry WHERE id = p_entry_id)::text || ':' ||
    COALESCE((SELECT section FROM entry WHERE id = p_entry_id)::text, 'standalone')));

  SELECT id, patient_id, clinic_id, section, author_role, body, version, updated_at
    INTO r FROM entry WHERE id = p_entry_id FOR UPDATE;
  IF r.id IS NULL THEN RAISE EXCEPTION 'entry not found'; END IF;

  -- Write-isolation re-check (mirrors entry_upd_* policy clauses):
  IF r.author_role <> p_user_role THEN
    RAISE EXCEPTION 'user role % cannot edit an entry authored by %', p_user_role, r.author_role;
  END IF;
  -- (enum-to-enum cast is illegal in PG; compare labels as text)
  IF section_role_of(r.section)::text <> p_user_role::text THEN
    RAISE EXCEPTION 'user role % cannot write section %', p_user_role, r.section;
  END IF;
  IF v_writer_clinic <> r.clinic_id THEN
    RAISE EXCEPTION 'user % is not in the entry clinic', p_user_id;
  END IF;

  -- Optimistic lock: if the checkout version is stale, the incoming edit loses
  -- deterministically and is preserved (never lost) as a flagged child version.
  IF r.version <> p_base_version THEN
    PERFORM resolve_conflict(p_entry_id, p_user_id, p_user_role, p_new_body, p_base_version);
    RETURN;
  END IF;

  v_new := (SELECT COALESCE(MAX(version), 0) + 1 FROM entry_version WHERE entry_id = p_entry_id);
  v_delta := char_diff(r.body, p_new_body);

  INSERT INTO entry_version(entry_id, clinic_id, version, body, delta_from_prev,
                            author_id, author_role, change_summary, conflict_flag)
  VALUES (p_entry_id, r.clinic_id, v_new, p_new_body, v_delta, p_user_id, p_user_role, NULL, false);

  UPDATE entry SET body = p_new_body, version = v_new, updated_at = now()
    WHERE id = p_entry_id;

  PERFORM log_audit(p_user_id, p_user_role, 'update', 'entry', p_entry_id,
                    r.patient_id, r.clinic_id, v_new,
                    jsonb_build_object('version', v_new,
                                       'field_changed', COALESCE(r.section::text, 'standalone')));
END $$;

-- -----------------------------------------------------------------------------
-- 5.4 resolve_conflict — deterministic loser preservation  (DATA_SCHEMA §7)
-- Winner = the already-committed current state (later updated_at). The incoming
-- (losing) edit is preserved as a flagged child version pointing at the winner
-- version, and is surfaced in the Glance "flag for review" open action.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION resolve_conflict(
  p_entry_id uuid,
  p_user_id uuid,
  p_user_role entry_author_role,
  p_loser_body text,
  p_base_version int
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  r record;
  v_winner_version_id bigint;
  v_new int;
BEGIN
  SELECT id, patient_id, clinic_id, version, updated_at INTO r
    FROM entry WHERE id = p_entry_id;
  IF r.id IS NULL THEN RAISE EXCEPTION 'entry not found'; END IF;

  SELECT id INTO v_winner_version_id FROM entry_version
    WHERE entry_id = p_entry_id AND version = r.version;

  v_new := (SELECT COALESCE(MAX(version), 0) + 1 FROM entry_version WHERE entry_id = p_entry_id);

  INSERT INTO entry_version(entry_id, clinic_id, version, body, delta_from_prev,
                            author_id, author_role, change_summary, conflict_flag,
                            conflict_of, created_at)
  VALUES (p_entry_id, r.clinic_id, v_new, p_loser_body, NULL,
          p_user_id, p_user_role, 'lost optimistic lock; preserved for review', true,
          v_winner_version_id, now());

  PERFORM log_audit(p_user_id, p_user_role, 'conflict_flagged', 'entry', p_entry_id,
                    r.patient_id, r.clinic_id, v_new,
                    jsonb_build_object('winner_version', r.version,
                                       'loser_version', v_new,
                                       'base_version', p_base_version,
                                       'conflict_of', v_winner_version_id));
END $$;

-- -----------------------------------------------------------------------------
-- 5.5 revert_entry — non-destructive revert: copy the target snapshot forward as
-- a NEW version; history stays immutable  (DATA_SCHEMA §5)
--
-- M2 integration point: trusts caller-passed p_user_id / p_user_role; call only
-- from a SET ROLE'd session (app/db/session.py set_app_context).
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION revert_entry(
  p_entry_id uuid,
  p_target_version int,
  p_user_id uuid,
  p_user_role entry_author_role
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  r record;
  v_body text;
  v_new int;
  v_cur int;
  v_writer_clinic uuid;
BEGIN
  SELECT clinic_id INTO v_writer_clinic FROM users WHERE id = p_user_id;
  SELECT patient_id, clinic_id, section, author_role, version, is_decayed
    INTO r FROM entry WHERE id = p_entry_id FOR UPDATE;
  IF r.author_role IS NULL THEN RAISE EXCEPTION 'entry not found'; END IF;
  IF r.author_role <> p_user_role THEN
    RAISE EXCEPTION 'user role % cannot revert an entry authored by %', p_user_role, r.author_role;
  END IF;
  -- (enum-to-enum cast is illegal in PG; compare labels as text)
  IF section_role_of(r.section)::text <> p_user_role::text THEN
    RAISE EXCEPTION 'user role % cannot revert section %', p_user_role, r.section;
  END IF;
  IF v_writer_clinic <> r.clinic_id THEN
    RAISE EXCEPTION 'user % is not in the entry clinic', p_user_id;
  END IF;

  IF r.is_decayed THEN
    PERFORM restore_entry(p_entry_id);
  END IF;

  SELECT body INTO v_body FROM entry_version
    WHERE entry_id = p_entry_id AND version = p_target_version;
  IF v_body IS NULL THEN
    RAISE EXCEPTION 'version % not found for entry %', p_target_version, p_entry_id;
  END IF;

  v_cur := r.version;
  v_new := (SELECT COALESCE(MAX(version), 0) + 1 FROM entry_version WHERE entry_id = p_entry_id);

  INSERT INTO entry_version(entry_id, clinic_id, version, body, delta_from_prev,
                            author_id, author_role, change_summary, conflict_flag)
  VALUES (p_entry_id, r.clinic_id, v_new, v_body,
          char_diff((SELECT body FROM entry WHERE id = p_entry_id), v_body),
          p_user_id, p_user_role, format('reverted to version %s', p_target_version), false);

  UPDATE entry SET body = v_body, version = v_new, updated_at = now()
    WHERE id = p_entry_id;

  PERFORM log_audit(p_user_id, p_user_role, 'revert', 'entry', p_entry_id,
                    r.patient_id, r.clinic_id, v_new,
                    jsonb_build_object('version_from', v_cur, 'version_to', v_new));
END $$;

-- -----------------------------------------------------------------------------
-- 5.6 update_weights — single row per (clinic, feature_key); weight
-- clamped to [-1, +2] (the schema.sql clamp trigger is the backstop)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION update_weights(
  p_clinic_id uuid,
  p_feature_key text,
  p_delta numeric,
  p_positive boolean
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  INSERT INTO learning_weight(clinic_id, feature_key, weight,
                              positive_count, negative_count, total_interactions,
                              last_seen_at)
  VALUES (p_clinic_id, p_feature_key,
          CASE WHEN p_positive THEN GREATEST(-1.0, LEAST(2.0, p_delta))
               ELSE GREATEST(-1.0, LEAST(2.0, -p_delta)) END,
          CASE WHEN p_positive THEN 1 ELSE 0 END,
          CASE WHEN p_positive THEN 0 ELSE 1 END,
          1, now())
  ON CONFLICT (clinic_id, feature_key) DO UPDATE SET
    weight = GREATEST(-1.0, LEAST(2.0, learning_weight.weight + EXCLUDED.weight)),
    positive_count = learning_weight.positive_count + EXCLUDED.positive_count,
    negative_count = learning_weight.negative_count + EXCLUDED.negative_count,
    total_interactions = learning_weight.total_interactions + 1,
    last_seen_at = now();
END $$;

-- -----------------------------------------------------------------------------
-- 5.7 record_interaction — persisted self-learning loop  (DATA_SCHEMA §9)
-- Logs one learning_interaction and upserts a weight for every extracted feature
-- (positive on accept/edit/comment/pin, negative on reject).
--
-- M2 integration point: trusts caller-passed p_user_id; call only from a
-- SET ROLE'd session (app/db/session.py set_app_context).
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION record_interaction(
  p_user_id uuid,
  p_entry_id uuid,
  p_interaction_type interaction_type,
  p_features jsonb DEFAULT '[]'
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_clinic uuid;
  v_patient uuid;
  v_positive boolean;
  v_delta numeric;
  v_feature text;
  v_features jsonb := COALESCE(p_features, '[]'::jsonb);
BEGIN
  SELECT clinic_id, patient_id INTO v_clinic, v_patient FROM entry WHERE id = p_entry_id;
  IF v_clinic IS NULL THEN RAISE EXCEPTION 'entry % not found', p_entry_id; END IF;
  IF user_clinic_id(p_user_id) <> v_clinic THEN
    RAISE EXCEPTION 'user % is not in the entry clinic', p_user_id;
  END IF;

  v_positive := (p_interaction_type <> 'highlight_reject');
  v_delta := CASE WHEN v_positive THEN 1.0 ELSE -1.0 END;

  INSERT INTO learning_interaction(clinic_id, patient_id, entry_id, user_id,
                                   interaction_type, feature_keys, weight_delta)
  VALUES (v_clinic, v_patient, p_entry_id, p_user_id, p_interaction_type, v_features, v_delta);

  IF jsonb_typeof(v_features) = 'array' THEN
    FOR v_feature IN SELECT jsonb_array_elements_text(v_features) LOOP
      PERFORM update_weights(v_clinic, v_feature, v_delta, v_positive);
    END LOOP;
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 5.8 recompute_glance — precompute the top-card  (DATA_SCHEMA §3.13, §9)
-- Scoring: recency + risk_weight + entity importance (allergy critical boost)
-- + open-action bonus + learned_boost from persisted learning_weight.
-- Patient-facing summary support: this SECURITY DEFINER function is the
-- required "patient-facing summaries" recompute entry point.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION recompute_glance(p_patient_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_clinic uuid;
  v_top jsonb;
  v_actions jsonb;
  v_risk jsonb;
BEGIN
  SELECT clinic_id INTO v_clinic FROM patient WHERE id = p_patient_id;
  IF v_clinic IS NULL THEN RAISE EXCEPTION 'patient % not found', p_patient_id; END IF;
  IF app_clinic_id() IS NOT NULL AND app_clinic_id() <> v_clinic THEN
    RAISE EXCEPTION 'cross-clinic glance access denied';
  END IF;

  -- top_items: recent, risk-weighted, entity-boosted, open-action-bonused,
  -- learned-boosted entries.
  WITH entity_boost AS (
    SELECT entry_id, MAX(CASE WHEN entity_type = 'allergy' THEN 5.0 ELSE 1.0 END) AS boost
      FROM entry_entity
     GROUP BY entry_id
  ),
  learned AS (
    SELECT eb.entry_id, SUM(lw.weight) AS learned
      FROM entry_entity eb
      JOIN learning_weight lw ON lw.clinic_id = v_clinic
         AND lw.feature_key = 'entity:' || eb.entity_type || ':' || eb.entity_value
     GROUP BY eb.entry_id
  ),
  open_ref AS (
    SELECT DISTINCT entry_id FROM task WHERE patient_id = p_patient_id AND status IN ('open','in_progress')
    UNION SELECT DISTINCT entry_id FROM comment WHERE patient_id = p_patient_id AND status = 'open'
    UNION SELECT DISTINCT entry_id FROM highlight WHERE patient_id = p_patient_id AND status IN ('suggested','accepted')
  ),
  scored AS (
    SELECT e.id, e.entry_type, e.title, left(e.body, 200) AS preview, e.risk_level,
           e.created_at,
           COALESCE(eb.boost, 0.0) AS boost,
           COALESCE(ln.learned, 0.0) AS learned,
           CASE e.risk_level WHEN 'critical' THEN 10 WHEN 'high' THEN 6
                             WHEN 'medium' THEN 3 WHEN 'low' THEN 1 ELSE 0 END AS risk_score,
           CASE WHEN o.entry_id IS NOT NULL THEN 3 ELSE 0 END AS action_bonus,
           EXTRACT(EPOCH FROM (now() - e.created_at)) AS age_secs
      FROM entry e
      LEFT JOIN entity_boost eb ON eb.entry_id = e.id
      LEFT JOIN learned ln ON ln.entry_id = e.id
      LEFT JOIN open_ref o ON o.entry_id = e.id
     WHERE e.patient_id = p_patient_id AND e.is_decayed = false
  ),
  final AS (
    SELECT id, entry_type, title, preview, risk_level, created_at,
           (2.0 * exp(-(age_secs / 86400.0) / 30.0)   -- recency, half-life ~30 days
            + 2.0 * risk_score
            + 1.5 * boost
            + 2.0 * action_bonus
            + 1.0 * learned) AS score
      FROM scored
  )
  SELECT jsonb_agg(jsonb_build_object('item_type', entry_type, 'item_id', id,
                                      'preview', preview, 'risk_level', risk_level,
                                      'score', round(score::numeric, 2),
                                      'created_at', created_at)
                   ORDER BY score DESC, created_at DESC)
    INTO v_top
    FROM final;

  IF v_top IS NULL THEN v_top := '[]'::jsonb; END IF;

  -- open_actions: unresolved tasks, open comments, suggested highlights, conflicts
  SELECT jsonb_agg(jsonb_build_object('kind', kind, 'id', id, 'label', label))
    INTO v_actions
  FROM (
    SELECT 'task'::text AS kind, t.id::text AS id, t.title AS label
      FROM task t WHERE t.patient_id = p_patient_id AND t.status IN ('open','in_progress')
    UNION ALL
    SELECT 'comment', c.id::text, left(c.body, 80)
      FROM comment c WHERE c.patient_id = p_patient_id AND c.status = 'open'
    UNION ALL
    SELECT 'highlight', h.id::text, h.risk_reason
      FROM highlight h WHERE h.patient_id = p_patient_id AND h.status = 'suggested'
    UNION ALL
    SELECT 'conflict', ev.id::text, 'conflicting version on entry ' || ev.entry_id::text
      FROM entry_version ev
      WHERE ev.entry_id IN (SELECT id FROM entry WHERE patient_id = p_patient_id)
        AND ev.conflict_flag
  ) a;

  IF v_actions IS NULL THEN v_actions := '[]'::jsonb; END IF;

  -- risk_flags: high/critical entries + allergy entity tags (allergy forces critical)
  WITH risk_rows AS (
    SELECT e.id AS entry_id, e.risk_level, e.title
      FROM entry e
     WHERE e.patient_id = p_patient_id AND e.risk_level IN ('high','critical')
    UNION ALL
    SELECT DISTINCT ee.entry_id, 'critical'::risk_level,
           'allergy: ' || ee.entity_value
      FROM entry_entity ee
      JOIN entry e ON e.id = ee.entry_id
     WHERE e.patient_id = p_patient_id AND ee.entity_type = 'allergy'
  )
  SELECT jsonb_agg(jsonb_build_object('entry_id', entry_id, 'risk_level', risk_level,
                                      'title', title))
    INTO v_risk
    FROM risk_rows;

  IF v_risk IS NULL THEN v_risk := '[]'::jsonb; END IF;

  INSERT INTO patient_glance(patient_id, clinic_id, top_items, open_actions, risk_flags,
                             computed_at, invalidated)
  VALUES (p_patient_id, v_clinic, v_top, v_actions, v_risk, now(), false)
  ON CONFLICT (patient_id) DO UPDATE SET
    clinic_id = EXCLUDED.clinic_id,
    top_items = EXCLUDED.top_items,
    open_actions = EXCLUDED.open_actions,
    risk_flags = EXCLUDED.risk_flags,
    computed_at = EXCLUDED.computed_at,
    invalidated = false;
END $$;

-- -----------------------------------------------------------------------------
-- 5.9 decay_old_entries — data-decay job  (DATA_SCHEMA §8)
-- Archives bodies of old, unreferenced entries to the cold archive tables and
-- leaves metadata stubs. Refuses entries referenced by open tasks/highlights/comments.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION decay_old_entries(p_threshold_days int DEFAULT 730) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_count int := 0;
  c record;
BEGIN
  FOR c IN
    SELECT e.id, e.patient_id, e.clinic_id, e.section, e.entry_type, e.provenance_id, e.body
      FROM entry e
     WHERE e.is_decayed = false
       AND e.created_at < now() - make_interval(days => p_threshold_days)
       AND NOT EXISTS (SELECT 1 FROM task t WHERE t.entry_id = e.id AND t.status IN ('open','in_progress'))
       AND NOT EXISTS (SELECT 1 FROM highlight h WHERE h.entry_id = e.id AND h.status IN ('suggested','accepted'))
       AND NOT EXISTS (SELECT 1 FROM comment c2 WHERE c2.entry_id = e.id AND c2.status = 'open')
  LOOP
    INSERT INTO entry_archive(entry_id, patient_id, clinic_id, body, section,
                              entry_type, provenance_id, archived_at)
    VALUES (c.id, c.patient_id, c.clinic_id, c.body, c.section,
            c.entry_type, c.provenance_id, now());

    INSERT INTO entry_version_archive(entry_id, clinic_id, version, body, author_role, created_at)
    SELECT ev.entry_id, c.clinic_id, ev.version, ev.body, ev.author_role, ev.created_at
      FROM entry_version ev WHERE ev.entry_id = c.id;

    UPDATE entry SET is_decayed = true, body = '' WHERE id = c.id;

    PERFORM log_audit(NULL, 'system', 'decay_archive', 'entry', c.id,
                      c.patient_id, c.clinic_id, NULL,
                      jsonb_build_object('archived_to', 'entry_archive'));
    v_count := v_count + 1;
  END LOOP;
  RETURN v_count;
END $$;

-- -----------------------------------------------------------------------------
-- 5.10 restore_entry — pull a decayed body back from the archive on demand
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION restore_entry(p_entry_id uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_body text;
  v_patient uuid;
  v_clinic uuid;
BEGIN
  SELECT body INTO v_body FROM entry_archive WHERE entry_id = p_entry_id;
  IF v_body IS NULL THEN RAISE EXCEPTION 'no archived body for entry %', p_entry_id; END IF;
  SELECT patient_id, clinic_id INTO v_patient, v_clinic FROM entry WHERE id = p_entry_id;
  IF v_clinic IS NULL THEN RAISE EXCEPTION 'entry % not found', p_entry_id; END IF;
  IF app_clinic_id() IS NOT NULL AND app_clinic_id() <> v_clinic THEN
    RAISE EXCEPTION 'cross-clinic restore denied';
  END IF;
  UPDATE entry SET body = v_body, is_decayed = false, updated_at = now()
    WHERE id = p_entry_id;
  DELETE FROM entry_archive WHERE entry_id = p_entry_id;
  PERFORM log_audit(NULL, 'system', 'system', 'entry', p_entry_id,
                    v_patient, v_clinic, NULL,
                    jsonb_build_object('restored_from', 'entry_archive'));
  RETURN v_body;
END $$;

-- =============================================================================
-- 6. EXECUTE grants for SECURITY DEFINER / internal functions
-- =============================================================================

REVOKE ALL ON FUNCTION log_audit(uuid, entry_author_role, audit_action, text, uuid, uuid, uuid, int, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION char_diff(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION save_entry(uuid, text, int, uuid, entry_author_role) FROM PUBLIC;
REVOKE ALL ON FUNCTION resolve_conflict(uuid, uuid, entry_author_role, text, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION revert_entry(uuid, int, uuid, entry_author_role) FROM PUBLIC;
REVOKE ALL ON FUNCTION update_weights(uuid, text, numeric, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION record_interaction(uuid, uuid, interaction_type, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION recompute_glance(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION decay_old_entries(int) FROM PUBLIC;
REVOKE ALL ON FUNCTION restore_entry(uuid) FROM PUBLIC;

-- EXECUTE is granted only to the role classes that may call each function
-- DIRECTLY (after SET ROLE). app_nightingale is deliberately excluded: it is the
-- NOINHERIT base role a connection starts as, and must never be able to call a
-- SECURITY DEFINER function before it has SET ROLE into a class (doing so would
-- run as nightingale_owner with no RLS context — a privilege-escalation vector).
-- Internal calls between these functions run as the owner (nightingale_owner),
-- so they need no EXECUTE grant. log_audit / update_weights are pipeline-only;
-- human roles reach them indirectly through save_entry / record_interaction.
GRANT EXECUTE ON FUNCTION log_audit(uuid, entry_author_role, audit_action, text, uuid, uuid, uuid, int, jsonb)
  TO system_pipeline;
GRANT EXECUTE ON FUNCTION save_entry(uuid, text, int, uuid, entry_author_role)
  TO staff_role, clinician_role, admin_role;
GRANT EXECUTE ON FUNCTION revert_entry(uuid, int, uuid, entry_author_role)
  TO staff_role, clinician_role, admin_role;
GRANT EXECUTE ON FUNCTION record_interaction(uuid, uuid, interaction_type, jsonb)
  TO staff_role, clinician_role, admin_role;
GRANT EXECUTE ON FUNCTION update_weights(uuid, text, numeric, boolean)
  TO system_pipeline;
GRANT EXECUTE ON FUNCTION recompute_glance(uuid)
  TO staff_role, clinician_role, admin_role;
GRANT EXECUTE ON FUNCTION decay_old_entries(int)
  TO admin_role;
GRANT EXECUTE ON FUNCTION restore_entry(uuid)
  TO staff_role, clinician_role, admin_role;

-- The session-context helpers are harmless and are used inside RLS policies
-- (which evaluate as the querying role), so keep them executable by PUBLIC.

-- =============================================================================
-- 7. Glance invalidation trigger — marks patient_glance stale on any mutation
-- to that patient's timeline data (DATA_SCHEMA §3.13). SECURITY DEFINER because
-- the trigger fires as the mutating role, which has no UPDATE grant on the
-- glance cache table.
-- =============================================================================

CREATE OR REPLACE FUNCTION trg_invalidate_glance() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_patient uuid;
BEGIN
  IF TG_TABLE_NAME = 'entry_version' THEN
    -- entry_version has no patient_id column; resolve the patient via its entry
    v_patient := (SELECT patient_id FROM entry WHERE id = COALESCE(NEW.entry_id, OLD.entry_id));
  ELSE
    v_patient := COALESCE(NEW.patient_id, OLD.patient_id);
  END IF;
  IF v_patient IS NOT NULL THEN
    UPDATE patient_glance SET invalidated = true, computed_at = now()
      WHERE patient_id = v_patient;
  END IF;
  RETURN COALESCE(NEW, OLD);
END $$;

DROP TRIGGER IF EXISTS glance_invalidate ON entry;
CREATE TRIGGER glance_invalidate AFTER INSERT OR UPDATE OR DELETE ON entry
  FOR EACH ROW EXECUTE FUNCTION trg_invalidate_glance();
DROP TRIGGER IF EXISTS glance_invalidate ON comment;
CREATE TRIGGER glance_invalidate AFTER INSERT OR UPDATE OR DELETE ON comment
  FOR EACH ROW EXECUTE FUNCTION trg_invalidate_glance();
DROP TRIGGER IF EXISTS glance_invalidate ON entry_version;
CREATE TRIGGER glance_invalidate AFTER INSERT OR UPDATE OR DELETE ON entry_version
  FOR EACH ROW EXECUTE FUNCTION trg_invalidate_glance();
DROP TRIGGER IF EXISTS glance_invalidate ON highlight;
CREATE TRIGGER glance_invalidate AFTER INSERT OR UPDATE OR DELETE ON highlight
  FOR EACH ROW EXECUTE FUNCTION trg_invalidate_glance();
DROP TRIGGER IF EXISTS glance_invalidate ON task;
CREATE TRIGGER glance_invalidate AFTER INSERT OR UPDATE OR DELETE ON task
  FOR EACH ROW EXECUTE FUNCTION trg_invalidate_glance();

RESET ROLE;
