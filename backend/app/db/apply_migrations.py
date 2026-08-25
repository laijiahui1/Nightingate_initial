#!/usr/bin/env python3
"""Idempotent migration runner for the Nightingale data layer.

Applies, in order and each in its own transaction:

    1. roles.sql   — role definitions (migration/owner roles, RBAC role classes,
                     the restricted `app_nightingale` login role)
    2. schema.sql  — full DDL (enums, tables, indexes, compression, integrity
                     triggers)
    3. rls.sql     — row-level security (grants, FORCE RLS, policies, SECURITY
                     DEFINER functions, glance-invalidation triggers)

Every file is idempotent on its own, and each successfully-applied file is
recorded (filename + SHA-256 checksum) in the `schema_migrations` table, so
re-running the runner is a no-op. If a file was already applied but its content
changed, the runner REFUSES to re-apply it (fail-safe: never mutate a live
schema silently).

Connection:
  - Connect as the migration superuser (`nightingale_migrator`, or the bootstrap
    `POSTGRES_USER` from docker-compose) — creating roles requires CREATEROLE.
  - URL: `MIGRATION_DATABASE_URL` overrides `DATABASE_URL`. The SQLAlchemy
    `postgresql+psycopg://` dialect marker is stripped because psycopg (v3)
    accepts plain `postgresql://` / `postgres://` URLs.

Usage:
    MIGRATION_DATABASE_URL=postgresql://nightingale:...@localhost:5432/nightingale \
        python -m app.db.apply_migrations

Exits non-zero on any failure (bad URL, missing file, checksum drift, SQL error).
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import psycopg

_DB_DIR = Path(__file__).resolve().parent
_MIGRATION_TABLE = "schema_migrations"

# Order matters: roles must exist before schema.sql does `SET ROLE
# nightingale_owner`, and both must exist before rls.sql references roles.
_MIGRATIONS: list[tuple[str, Path]] = [
    ("roles.sql", _DB_DIR / "roles.sql"),
    ("schema.sql", _DB_DIR / "schema.sql"),
    ("rls.sql", _DB_DIR / "rls.sql"),
]


def database_url() -> str:
    """Return a psycopg v3 connection URL for the migration superuser."""
    url = os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "no database URL: set MIGRATION_DATABASE_URL or DATABASE_URL, "
            "e.g. postgresql+psycopg://nightingale:...@db:5432/nightingale"
        )
    # Normalise SQLAlchemy dialect markers psycopg v3 does not understand.
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        return url
    raise SystemExit(f"unrecognised database URL scheme: {url!r}")


def file_sha256(path: Path) -> str:
    """Checksum used to detect content drift between runs."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_migration_table(conn: psycopg.Connection) -> None:
    """Create the bookkeeping table if it does not yet exist (own txn)."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_MIGRATION_TABLE} (
            filename    text PRIMARY KEY,
            checksum    text NOT NULL,
            applied_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()


def applied_checksum(conn: psycopg.Connection, filename: str) -> str | None:
    row = conn.execute(
        f"SELECT checksum FROM {_MIGRATION_TABLE} WHERE filename = %s",
        (filename,),
    ).fetchone()
    return row[0] if row else None


def apply_one(conn: psycopg.Connection, filename: str, path: Path, checksum: str) -> None:
    """Run one migration file and record it — all inside the caller's txn."""
    print(f"== applying {filename} ...", flush=True)
    conn.execute(path.read_text(encoding="utf-8"))
    conn.execute(
        f"""
        INSERT INTO {_MIGRATION_TABLE} (filename, checksum)
        VALUES (%s, %s)
        ON CONFLICT (filename) DO UPDATE SET checksum = EXCLUDED.checksum
        """,
        (filename, checksum),
    )
    print(f"== applied {filename}", flush=True)


def main() -> int:
    url = database_url()
    host = url.split("@")[-1]  # never echo credentials
    print(f"== migration runner connecting to {host}", flush=True)

    try:
        with psycopg.connect(url) as conn:
            ensure_migration_table(conn)
            for filename, path in _MIGRATIONS:
                if not path.exists():
                    print(f"!! missing migration file: {path}", file=sys.stderr)
                    return 1
                checksum = file_sha256(path)
                previous = applied_checksum(conn, filename)
                if previous == checksum:
                    print(f"== {filename} already applied (checksum match); skipping")
                    continue
                if previous is not None:
                    print(
                        f"!! {filename} already applied but content changed; "
                        "refusing to re-run (reset the row to force a re-apply)",
                        file=sys.stderr,
                    )
                    return 1
                try:
                    apply_one(conn, filename, path, checksum)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    print(
                        f"!! {filename} FAILED; transaction rolled back",
                        file=sys.stderr,
                    )
                    raise
    except psycopg.Error as exc:
        print(f"!! database error: {exc}", file=sys.stderr)
        return 1

    print("== all migrations applied", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
