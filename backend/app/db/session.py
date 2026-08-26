"""SQLAlchemy engine + session factory + per-request role context.

The engine reads `DATABASE_URL` from application settings (see
`app/core/config.py`) and connects as the restricted application role
`app_nightingale` (docs/SECURITY.md §a, roles.sql). That role has no table
privileges of its own: it is a NOINHERIT member of every RBAC role class and
must `SET ROLE` into one before it can touch data.

Row-Level Security is enforced server-side in `app/db/rls.sql` on the session
GUCs `app.user_id` / `app.role` / `app.clinic_id` (docs/DATA_SCHEMA.md §4.1).
`set_app_context` is the per-request entry point: it SETs the role class and
the three GUCs transaction-locally, so a pooled connection never leaks one
request's identity into the next.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# Application actor role (the verified JWT `role` claim / `app.role` GUC) ->
# the PostgreSQL role class the connection must `SET ROLE` into before running
# queries (roles.sql). `system` maps to the AI-ingestion pipeline role
# (author_role='system').
ROLE_CLASS_BY_NAME: dict[str, str] = {
    "patient": "patient_role",
    "staff": "staff_role",
    "clinician": "clinician_role",
    "admin": "admin_role",
    "system": "system_pipeline",
}

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,  # transparently drop stale pooled connections
)


@event.listens_for(engine, "connect")
def _reset_connection_role(dbapi_connection, connection_record) -> None:
    """Drop any lingering role state when a raw connection is created.

    `set_app_context` uses `SET LOCAL ROLE` (transaction-scoped), which the
    pool's rollback-on-return already clears. This `RESET ROLE` is
    belt-and-suspenders against a future non-LOCAL `SET ROLE` leaking across
    pooled check-outs.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("RESET ROLE")
    finally:
        cursor.close()


SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


def _assert_uuid(value: object, name: str) -> str:
    """Return `value` as a canonical UUID string, or raise on a malformed id.

    Session context comes from the verified token, but we still validate the
    shape here so a bug can never smuggle a non-UUID into a SET / RLS policy.
    """
    if value is None:
        return ""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{name} must be a UUID, got {value!r}") from exc


def set_app_context(
    db: Session,
    *,
    role: str,
    user_id: uuid.UUID | str | None,
    clinic_id: uuid.UUID | str | None,
) -> None:
    """Set the RLS session context for one request/transaction.

    Runs inside the caller's transaction:

      * ``SET LOCAL ROLE <role_class>`` — the restricted role acts as the class
      * ``set_config('app.user_id',  ..., true)`` — the RLS GUCs (DATA_SCHEMA §4.1)

    ``SET LOCAL`` scopes all of it to the current transaction, so the pool's
    rollback-on-return clears it automatically. ``role`` must be one of the
    keys in ``ROLE_CLASS_BY_NAME`` (or a raw role-class name from that mapping).

    This is the M2 API-dependency hook: after JWT verification, the dependency
    calls this with the decoded ``role`` / ``sub`` / ``clinic_id`` and then the
    session runs as that role class.
    """
    role_class = ROLE_CLASS_BY_NAME.get(role, role)
    if role_class not in ROLE_CLASS_BY_NAME.values():
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(ROLE_CLASS_BY_NAME)}")

    user_id_str = _assert_uuid(user_id, "user_id")
    clinic_id_str = _assert_uuid(clinic_id, "clinic_id")

    # `SET ROLE` takes an identifier, not a bind parameter; `role_class` is a
    # whitelisted literal from ROLE_CLASS_BY_NAME, never client input.
    db.execute(text(f"SET LOCAL ROLE {role_class}"))
    db.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": user_id_str})
    db.execute(text("SELECT set_config('app.role', :role, true)"), {"role": role})
    db.execute(text("SELECT set_config('app.clinic_id', :cid, true)"), {"cid": clinic_id_str})


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a session and always close it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
