-- =============================================================================
-- Nightingale — roles.sql
--
-- Role definitions + base grants. Run as the migration SUPERUSER (this is the
-- only file that creates roles). Idempotent: every CREATE ROLE is guarded so
-- re-running is a no-op.
--
-- Role model (matches docs/DATA_SCHEMA.md §4 and docs/SECURITY.md §a):
--
--   nightingale_migrator  LOGIN SUPERUSER    migration admin (apply_migrations.py)
--   nightingale_owner     NOLOGIN SUPERUSER  non-login owner of every schema
--                                             object + every SECURITY DEFINER
--                                             function (functions run as it and
--                                             therefore bypass RLS — they must
--                                             validate clinic/patient/author_role
--                                             explicitly, which they do)
--   patient_role          NOLOGIN            RBAC role class (SET ROLE target)
--   staff_role            NOLOGIN            RBAC role class
--   clinician_role        NOLOGIN            RBAC role class
--   admin_role            NOLOGIN            RBAC role class (clinic-scoped)
--   system_pipeline       NOLOGIN            AI ingestion role (author_role='system')
--   app_nightingale       LOGIN              RESTRICTED application role — the ONLY
--                                             role the API connects as. It is a
--                                             NOINHERIT member of every role class
--                                             so it must `SET ROLE` to act as one;
--                                             on its own it has no table access.
--
-- Dev-only passwords below are placeholders — override in production. The
-- RLS policies (rls.sql) and the table grants (rls.sql) enforce all row-level
-- access; roles.sql only defines the identities and schema/database grants.
-- -----------------------------------------------------------------------------

-- -----------------------------------------------------------------------------
-- Migration + owner roles
-- -----------------------------------------------------------------------------

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nightingale_migrator') THEN
    CREATE ROLE nightingale_migrator LOGIN SUPERUSER PASSWORD 'dev_migrator_password_change_me';
    COMMENT ON ROLE nightingale_migrator IS
      'Migration admin. Connect as this (or the bootstrap superuser) to run apply_migrations.py. Dev password only.';
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nightingale_owner') THEN
    CREATE ROLE nightingale_owner NOLOGIN SUPERUSER;
    COMMENT ON ROLE nightingale_owner IS
      'Non-login owner of all schema objects and SECURITY DEFINER functions.';
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- RBAC role classes (SET ROLE targets — never connect directly)
-- -----------------------------------------------------------------------------

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'patient_role') THEN
    CREATE ROLE patient_role NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'staff_role') THEN
    CREATE ROLE staff_role NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clinician_role') THEN
    CREATE ROLE clinician_role NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'admin_role') THEN
    CREATE ROLE admin_role NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'system_pipeline') THEN
    CREATE ROLE system_pipeline NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    COMMENT ON ROLE system_pipeline IS
      'AI ingestion role. Writes author_role=system entries/highlights/provenance.';
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- Restricted application role
-- -----------------------------------------------------------------------------

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_nightingale') THEN
    CREATE ROLE app_nightingale LOGIN PASSWORD 'dev_app_password_change_me';
    COMMENT ON ROLE app_nightingale IS
      'Restricted application role — the API connects as this. Dev password only.';
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- Memberships: app_nightingale may SET ROLE to any role class, but INHERITS
-- NOTHING from them (NOINHERIT), so the union of privileges is never active at
-- once. (PG16 `WITH INHERIT FALSE`; `SET` defaults to true.)
-- -----------------------------------------------------------------------------

GRANT patient_role   TO app_nightingale WITH INHERIT FALSE;
GRANT staff_role     TO app_nightingale WITH INHERIT FALSE;
GRANT clinician_role TO app_nightingale WITH INHERIT FALSE;
GRANT admin_role     TO app_nightingale WITH INHERIT FALSE;
GRANT system_pipeline TO app_nightingale WITH INHERIT FALSE;

-- -----------------------------------------------------------------------------
-- Base grants
-- -----------------------------------------------------------------------------

-- CONNECT on the current database (robust to the actual DB name).
DO $$
BEGIN
  EXECUTE 'GRANT CONNECT ON DATABASE ' || quote_ident(current_database())
          || ' TO app_nightingale, nightingale_migrator';
END $$;

-- Schema USAGE for every role class + the restricted app role.
GRANT USAGE ON SCHEMA public TO patient_role, staff_role, clinician_role, admin_role,
  system_pipeline, app_nightingale;

-- Table-level DML grants are issued in rls.sql (the tables must exist first).
-- This keeps every access-control decision in one place, next to the policies.
