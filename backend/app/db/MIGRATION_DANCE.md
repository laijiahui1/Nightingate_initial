# Migration Dance — the one sharp edge of the migration tooling

Operational notes for anyone touching `roles.sql`, `schema.sql`, or `rls.sql`.
The runner is `app/db/apply_migrations.py` (invoked as `python -m
app.db.apply_migrations`). It is deliberately conservative: it will never mutate
a live schema silently, which is also what makes a careless edit dangerous.

## 1. Idempotency model — `schema_migrations` + SHA-256

Each file (`roles.sql` → `schema.sql` → `rls.sql`, applied in that order, each in
its own transaction) is recorded in the `schema_migrations` bookkeeping table as
`(filename, checksum, applied_at)`, where `checksum` is the SHA-256 of the file's
bytes.

On every run the runner compares the on-disk checksum to the recorded one:

- **No row** → the file is applied, then recorded.
- **Checksum matches** → skipped ("already applied").
- **Checksum differs** → **REFUSED** (exit code 1) with the message:

  > `!! <file> already applied but content changed; refusing to re-run (reset the row to force a re-apply)`

This fail-safe protects production from half-applied DDL. It is the *correct*
default behaviour; the "reset the row" dance below is how you opt back in.

## 2. Why `rls.sql` is NOT re-appliable

`rls.sql` issues **87 `CREATE POLICY`** statements and **zero** `DROP POLICY IF
EXISTS` guards. `CREATE POLICY` fails with `policy "... already exists"` if the
policy is already present, so re-running a *changed* `rls.sql` against an
existing database (i.e. after you reset the `schema_migrations` row to force a
re-apply) aborts mid-file. The DDL up to the failure is left applied, the rest is
not — a partially-migrated RLS layer.

The same is *not* true of `roles.sql` (idempotent `DO $$ ... IF NOT EXISTS` /
`CREATE ... IF NOT EXISTS` blocks) or `schema.sql` (`CREATE TABLE IF NOT EXISTS`).
Only `rls.sql` has the raw, unguarded `CREATE POLICY` statements.

## 3. Safe procedures

Pick the procedure that matches the change you actually made:

1. **New SQL file** — add a *new* migration file (and register it in
   `_MIGRATIONS`). Re-run the runner: a file with no recorded row applies cleanly
   on top of the existing database. No dance required.

2. **Changed file whose content is idempotent** — when the change is all
   `CREATE OR REPLACE FUNCTION`, `ALTER ... IF EXISTS`, or policy guards that
   self-check, do:

   ```sql
   DELETE FROM schema_migrations WHERE filename = '<file>';
   ```

   then re-run the runner. It re-applies the file in full and records the new
   checksum. (Keep every statement in the file idempotent, or prefer procedure 1
   and put the change in a *new* file.)

3. **Changed `rls.sql`** — do **NOT** use the reset-the-row dance. Instead:

   a. **Recreate the database** (dev / fixture). The pytest fixture already does
      this every run (see §4), so a fresh `rls.sql` is validated from scratch with
      no dance; or

   b. **Add `DROP POLICY IF EXISTS` guards** as a dedicated, separately-reviewed
      migration in its own file (a new `CREATE POLICY` after the guard), never by
      editing the existing `rls.sql` in place.

## 4. How the fixture validates SQL

`tests/conftest.py` builds an isolated `<basename>_test` database each pytest
session (`DROP DATABASE IF EXISTS ... WITH (FORCE)` then `CREATE DATABASE`), then
runs `apply_migrations.main()` against it from scratch, then seeds it. Because
the database is created empty on every run, every statement in `roles.sql`,
`schema.sql`, and `rls.sql` is exercised on a clean DB — so any SQL change is
proven by the suite even without ever performing a live "dance" against an
existing database. A broken `rls.sql` fails the whole suite at fixture setup.
