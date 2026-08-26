"""Shared fixtures for the Nightingale backend micro-test suite.

Fixture contract (docs/PLAN.md, docs/SECURITY.md §a, docs/DATA_SCHEMA.md §10):

- ``test_database`` (session)   creates an ISOLATED fixture PostgreSQL database
                                (``<basename>_test``, dropped on teardown), runs
                                migrations via ``app.db.apply_migrations``, and
                                points ``MIGRATION_DATABASE_URL`` /
                                ``SEED_DATABASE_URL`` / ``DATABASE_URL`` at it.
- ``seed_state`` (session)      loads the deterministic synthetic seed
                                (``app.seed``) and builds id maps for clinics,
                                users, patients, entries, comments, highlights.
- ``seed_db`` (function)        resets the fixture database to a clean seed
                                before EVERY test (isolation), also clearing the
                                append-only ``audit_log`` (via TRUNCATE, which
                                the append-only trigger does not fire on).
- ``pool`` (session)            a connection pool connected AS the restricted
                                role ``app_nightingale`` (NOINHERIT member of
                                every role class) — never the owner/superuser.
- ``db`` (function)             one app_nightingale connection from the pool.
- ``role_conn`` (function)      context manager: an app_nightingale connection
                                with the RLS session GUCs set for a role
                                (``SET LOCAL app.user_id / role / clinic_id``),
                                so a test can exercise RLS as that role.
- ``admin_conn`` (function)     a superuser connection (setup / schema checks).
- ``auth_headers`` (function)   JWT auth headers for a role (uses the same
                                ``app.core.security.create_access_token`` the
                                API will verify).
- ``test_client`` (async)       httpx ASGI client against the FastAPI app.
- ``role_client`` (async)       factory returning an httpx client carrying a JWT
                                for a given role.
- ``staff_client`` / ``clinician_client`` / ``patient_client`` / ``admin_client``
                                (async) ready-to-use role-scoped clients.

API contract assumed by the RED test skeletons (finalize during GREEN — routes
currently do not exist, which is exactly why the suite is RED):

- ``PUT /api/entries/{id}``            body {"body", "base_version"} -> 200 / 403
- ``POST /api/entries/{id}/revert``    body {"target_version"}     -> 200
- ``GET /api/patients/{id}``           page bundle {"entries": [...], "comments": [...]}
- ``POST /api/patients/{id}/highlights/generate``  body {"entry_id"} -> highlight
- ``GET /api/patients/{id}/highlights``            -> {"highlights": [...]}
- ``GET /api/patients/{id}/highlights/suggestions?q=...`` -> {"suggestions": [...]}
- ``POST /api/patients/{id}/highlights/{hl_id}/accept``  -> 200
- ``GET /api/audit?target_type=...&target_id=...``  (admin-only) -> {"rows": [...]}

Environment contract:

- ``TEST_DATABASE_URL``    admin (superuser) URL for the fixture DB. Falls back
                           to ``MIGRATION_DATABASE_URL`` then ``DATABASE_URL``.
- ``TEST_APP_DATABASE_USER`` / ``TEST_APP_DATABASE_PASSWORD``  credentials for
                           the restricted ``app_nightingale`` role (defaults match
                           backend/db/roles.sql dev placeholders).
- When no database is reachable every test that depends on the fixtures is
  skipped, so ``pytest -q`` stays importable and runnable anywhere.
"""

from __future__ import annotations

import contextlib
import os
import threading
import urllib.parse
from typing import TYPE_CHECKING

import psycopg
import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from httpx import AsyncClient

# ---------------------------------------------------------------------------
# URL plumbing
# ---------------------------------------------------------------------------


def _normalize(url: str) -> str:
    """Normalise SQLAlchemy dialect markers psycopg v3 does not understand."""
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def _admin_url() -> str:
    """Return the superuser URL used to manage the fixture database."""
    url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("MIGRATION_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        url = "postgresql://nightingale:dev_password_change_me@localhost:5432/nightingale"
    return _normalize(url)


def _db_name(url: str) -> str:
    """Fixture database name = base database name + ``_test``."""
    path = urllib.parse.urlsplit(url).path
    base = path.strip("/") or "nightingale"
    return f"{base}_test"


def _with_db(url: str, dbname: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment)
    )


def _app_url(admin_url: str, *, scheme: str | None = None) -> str:
    """Derive the restricted ``app_nightingale`` URL from the admin URL.

    ``scheme`` overrides the URL scheme. psycopg (v3) only accepts plain
    ``postgresql://``, while SQLAlchemy selects the psycopg2 dialect for a bare
    ``postgresql://`` URL — and psycopg2 is not installed. The app engine
    therefore needs ``postgresql+psycopg://``, while the raw-psycopg pool keeps
    the plain form.
    """
    user = os.environ.get("TEST_APP_DATABASE_USER", "app_nightingale")
    password = os.environ.get("TEST_APP_DATABASE_PASSWORD", "dev_app_password_change_me")
    parts = urllib.parse.urlsplit(admin_url)
    host = parts.hostname or "localhost"
    netloc = f"{user}:{password}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit(
        (scheme or parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


def _ident(name: str) -> str:
    """Quote a PostgreSQL identifier safely (only used with fixed test names)."""
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Tiny dependency-free psycopg connection pool (connected as app_nightingale)
# ---------------------------------------------------------------------------


class _ConnectionPool:
    """Thread-safe, minimal psycopg connection pool (no extra dependency)."""

    def __init__(self, dsn: str, maxsize: int = 5) -> None:
        self._dsn = dsn
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._idle: list[psycopg.Connection] = []

    def checkout(self) -> psycopg.Connection:
        with self._lock:
            conn = self._idle.pop() if self._idle else psycopg.connect(self._dsn)
        try:
            conn.execute("SELECT 1")  # pre-ping: drop stale pooled connections
        except psycopg.OperationalError:
            conn.close()
            conn = psycopg.connect(self._dsn)
        return conn

    def checkin(self, conn: psycopg.Connection) -> None:
        try:
            conn.execute("ROLLBACK")  # never let a test transaction leak into the pool
        except psycopg.Error:
            pass
        with self._lock:
            self._idle.append(conn)

    def close_all(self) -> None:
        with self._lock:
            for conn in self._idle:
                conn.close()
            self._idle.clear()


# ---------------------------------------------------------------------------
# Seed-state helpers (deterministic synthetic data -> id maps)
# ---------------------------------------------------------------------------

_ENTRY_TITLE_FRAGMENTS: dict[str, str] = {
    "e1_intake": "Intake",
    "e2_ai_doctor": "AI Doctor Consult Summary — 2025-04-15",
    "e3_care_plan": "Care Plan",
    "e_patient_discharge": "Care Instructions",
    "e4_ai_nurse": "AI Nurse Consult Summary",
    "e5_ai_session": "AI Patient Session Summary — 2025-06-10",
    "e6_lab_handoff": "Handoff — lab results pending",
    "e7_med_review": "Medication Review",
    "e8_session_dizziness": "AI Patient Session Summary — 2026-01-05",
    "e9_review": "Review — 2026-02-06",
    "e10_handoff_q1": "Handoff — Q1 2026",
}

_COMMENT_BODY_FRAGMENTS: dict[str, str] = {
    "c1_resolved": "Patient contacted by phone",
    "c2_open_mention": "Nurse to coordinate the cardiology referral",
    "c3_reply": "Agreed — referring",
    "c4_patient": "I feel fine now",
}

_HIGHLIGHT_QUOTE_FRAGMENTS: dict[str, str] = {
    "h1_high_bp": "Elevated BP and dizziness",
    "h2_penicillin": "Penicillin",
    "h3_pharm_review": "Consider adding losartan",
    "h4_dizziness": "Episodes of dizziness",
}


def _scalar(conn: psycopg.Connection, sql: str, params: tuple = ()) -> object:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise RuntimeError(f"expected a row for {sql!r}")
    return row[0]


def _build_state(admin_url: str) -> dict:
    """Query the seeded fixture DB and build the id maps used by the tests."""
    with psycopg.connect(admin_url) as conn:
        clinic = _scalar(conn, "SELECT id FROM clinic WHERE name = %s", ("Meridian Family Clinic",))
        clinic_b = _scalar(
            conn, "SELECT id FROM clinic WHERE name = %s", ("Harbourview Medical Centre",)
        )

        def _roles(cid: object) -> dict[str, tuple[object, object]]:
            out: dict[str, tuple[object, object]] = {}
            for role in ("patient", "staff", "clinician", "admin"):
                out[role] = (
                    _scalar(
                        conn,
                        "SELECT id FROM users WHERE clinic_id = %s AND role = %s "
                        "ORDER BY email LIMIT 1",
                        (cid, role),
                    ),
                    cid,
                )
            return out

        patients = dict(
            conn.execute(
                "SELECT display_name, id FROM patient WHERE clinic_id = %s ORDER BY display_name",
                (clinic,),
            ).fetchall()
        )
        patients_b = dict(
            conn.execute(
                "SELECT display_name, id FROM patient WHERE clinic_id = %s ORDER BY display_name",
                (clinic_b,),
            ).fetchall()
        )

        entries: dict[str, object] = {}
        for key, frag in _ENTRY_TITLE_FRAGMENTS.items():
            entries[key] = _scalar(
                conn,
                "SELECT id FROM entry WHERE clinic_id = %s AND title LIKE %s LIMIT 1",
                (clinic, f"%{frag}%"),
            )
        comments: dict[str, object] = {}
        for key, frag in _COMMENT_BODY_FRAGMENTS.items():
            comments[key] = _scalar(
                conn,
                "SELECT id FROM comment WHERE clinic_id = %s AND body LIKE %s LIMIT 1",
                (clinic, f"%{frag}%"),
            )
        highlights: dict[str, object] = {}
        for key, frag in _HIGHLIGHT_QUOTE_FRAGMENTS.items():
            highlights[key] = _scalar(
                conn,
                "SELECT id FROM highlight WHERE clinic_id = %s AND quoted_text LIKE %s LIMIT 1",
                (clinic, f"%{frag}%"),
            )

        # Resolve the role maps inside the ``with`` block: `_roles` closes over
        # `conn`, which is closed once the ``with`` exits.
        roles = _roles(clinic)
        roles_b = _roles(clinic_b)

    return {
        "clinic_id": clinic,
        "clinic_b_id": clinic_b,
        "roles": roles,
        "roles_b": roles_b,
        "patients": patients,
        "patients_b": patients_b,
        "entries": entries,
        "comments": comments,
        "highlights": highlights,
    }


# ---------------------------------------------------------------------------
# Session fixtures: isolated database + migrations + seed
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_database() -> dict:
    """Create an isolated fixture PostgreSQL database, migrate it, then drop it.

    Raises ``pytest.skip`` when no PostgreSQL is reachable so the RED suite
    stays importable and runnable everywhere (CI spins a real database).
    """
    admin_url = _admin_url()
    dbname = _db_name(admin_url)
    admin_test_url = _with_db(admin_url, dbname)
    app_url = _app_url(admin_test_url)
    # The FastAPI app engine (app.db.session) uses SQLAlchemy, which needs the
    # ``+psycopg`` dialect marker to pick psycopg (v3) rather than psycopg2.
    app_sqlalchemy_url = _app_url(admin_test_url, scheme="postgresql+psycopg")
    maintenance_url = _with_db(admin_url, "postgres")

    saved_env = {
        k: os.environ.get(k)
        for k in ("DATABASE_URL", "MIGRATION_DATABASE_URL", "SEED_DATABASE_URL")
    }
    try:
        try:
            with psycopg.connect(maintenance_url, autocommit=True) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {_ident(dbname)} WITH (FORCE)")
                conn.execute(f"CREATE DATABASE {_ident(dbname)}")
        except (psycopg.Error, OSError) as exc:
            pytest.skip(f"no test PostgreSQL reachable at {maintenance_url!r}: {exc}")

        # Point every runner at the isolated fixture database.
        os.environ["MIGRATION_DATABASE_URL"] = admin_test_url
        os.environ["SEED_DATABASE_URL"] = admin_test_url
        # The app itself connects as the RESTRICTED app_nightingale role
        # (SQLAlchemy needs the +psycopg dialect marker; the raw-psycopg pool
        # uses the plain ``app_url``).
        os.environ["DATABASE_URL"] = app_sqlalchemy_url

        # Apply migrations (roles.sql -> schema.sql -> rls.sql).
        from app.db import apply_migrations

        rc = apply_migrations.main()
        if rc != 0:
            pytest.fail("migration runner failed against the fixture database")

        yield {
            "admin": admin_test_url,
            "app": app_url,
            "maintenance": maintenance_url,
            "dbname": dbname,
        }
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            with psycopg.connect(maintenance_url, autocommit=True) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {_ident(dbname)} WITH (FORCE)")
        except psycopg.Error:
            pass


@pytest.fixture(scope="session")
def seed_state(test_database: dict) -> dict:
    """Seed the fixture DB with the deterministic synthetic dataset + id maps."""
    from app import seed as seed_module

    rc = seed_module.main()
    assert rc == 0, "synthetic seed failed against the fixture database"
    return _build_state(test_database["admin"])


@pytest.fixture(scope="session")
def pool(test_database: dict) -> _ConnectionPool:
    """Connection pool connected AS the restricted role ``app_nightingale``."""
    pool_ = _ConnectionPool(test_database["app"])
    try:
        yield pool_
    finally:
        pool_.close_all()


@pytest.fixture
def seed_db(test_database: dict) -> dict:
    """Reset the fixture DB to a clean deterministic seed before every test.

    Also clears the append-only ``audit_log`` (via TRUNCATE, which the
    append-only BEFORE UPDATE OR DELETE trigger does not fire on) so version /
    audit assertions are isolated between tests.
    """
    from app import seed as seed_module

    with psycopg.connect(test_database["admin"]) as conn:
        conn.execute("TRUNCATE audit_log")
        seed_module._seed(conn)  # mirrors seed.main() minus the summary print
        conn.commit()
    return test_database


# ---------------------------------------------------------------------------
# Data fixtures (fresh per test, deterministic ids shared with seed_state)
# ---------------------------------------------------------------------------


@pytest.fixture
def clinic_id(seed_db: dict, seed_state: dict) -> object:
    return seed_state["clinic_id"]


@pytest.fixture
def clinic_b_id(seed_db: dict, seed_state: dict) -> object:
    return seed_state["clinic_b_id"]


@pytest.fixture
def roles(seed_db: dict, seed_state: dict) -> dict[str, tuple[object, object]]:
    return seed_state["roles"]


@pytest.fixture
def roles_b(seed_db: dict, seed_state: dict) -> dict[str, tuple[object, object]]:
    return seed_state["roles_b"]


@pytest.fixture
def patients(seed_db: dict, seed_state: dict) -> dict[str, object]:
    return seed_state["patients"]


@pytest.fixture
def patients_b(seed_db: dict, seed_state: dict) -> dict[str, object]:
    return seed_state["patients_b"]


@pytest.fixture
def entries(seed_db: dict, seed_state: dict) -> dict[str, object]:
    return seed_state["entries"]


@pytest.fixture
def comments(seed_db: dict, seed_state: dict) -> dict[str, object]:
    return seed_state["comments"]


@pytest.fixture
def highlights(seed_db: dict, seed_state: dict) -> dict[str, object]:
    return seed_state["highlights"]


# ---------------------------------------------------------------------------
# Database access fixtures (all connected as the restricted app role)
# ---------------------------------------------------------------------------


@pytest.fixture
def db(pool: _ConnectionPool, seed_db: dict, seed_state: dict) -> psycopg.Connection:
    """A read-back connection from the app_nightingale pool, SET ROLE'd admin_role.

    The base ``app_nightingale`` role has no table privileges; tests use ``db`` to
    read rows back (proving persistence beyond the API response), so it SETs LOCAL
    ROLE admin_role + the seed clinic GUCs. admin_role has SELECT on every timeline
    table (rls.sql) and is scoped to the Meridian clinic, matching the ``entries``
    / ``comments`` fixtures. READ COMMITTED means committed API writes (on a
    separate connection) become visible across statements within this transaction.
    """
    conn = pool.checkout()
    admin_uid = seed_state["roles"]["admin"][0]
    clinic = seed_state["clinic_id"]
    try:
        conn.execute("BEGIN")
        conn.execute("SET LOCAL ROLE admin_role")
        conn.execute("SELECT set_config('app.user_id', %s, true)", (str(admin_uid),))
        conn.execute("SELECT set_config('app.role', 'admin', true)")
        conn.execute("SELECT set_config('app.clinic_id', %s, true)", (str(clinic),))
        yield conn
    finally:
        try:
            conn.execute("ROLLBACK")
        except psycopg.Error:
            pass
        pool.checkin(conn)


@pytest.fixture
def role_conn(pool: _ConnectionPool, roles: dict[str, tuple[object, object]]):
    """Context manager: app_nightingale connection with RLS GUCs for a role.

    Usage::

        with role_conn("staff") as conn:
            conn.execute("UPDATE entry ...")  # runs as staff under RLS

    Pass ``user_id``/``clinic_id`` to act as a user in another clinic
    (e.g. ``role_conn("staff", *roles_b["staff"])``).
    """

    # Role name -> PostgreSQL role class (mirrors app.db.session.ROLE_CLASS_BY_NAME,
    # but kept local so this module never imports app.db.session and builds its engine).
    _role_class = {
        "patient": "patient_role",
        "staff": "staff_role",
        "clinician": "clinician_role",
        "admin": "admin_role",
    }

    @contextlib.contextmanager
    def _factory(role: str, user_id: object | None = None, clinic_id: object | None = None):
        uid, cid = roles[role] if (user_id is None or clinic_id is None) else (user_id, clinic_id)
        conn = pool.checkout()
        try:
            conn.execute("BEGIN")
            # The base app_nightingale role has NO table privileges; SET LOCAL ROLE
            # into the role class so the connection acts as that class under RLS.
            conn.execute(f"SET LOCAL ROLE {_role_class[role]}")
            conn.execute("SELECT set_config('app.user_id', %s, true)", (str(uid),))
            conn.execute(
                "SELECT set_config('app.role', %s, true), set_config('app.clinic_id', %s, true)",
                (role, str(cid)),
            )
            yield conn
        finally:
            try:
                conn.execute("ROLLBACK")  # GUCs + role are transaction-local: reset here
            except psycopg.Error:
                pass
            pool.checkin(conn)

    return _factory


@pytest.fixture
def admin_conn(test_database: dict, seed_db: dict) -> psycopg.Connection:
    """Superuser connection for setup / schema-level checks (bypasses RLS)."""
    conn = psycopg.connect(test_database["admin"], autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# JWT + httpx test-client fixtures (JWT auth for each role)
# ---------------------------------------------------------------------------


def _jwt_for(user_id: object, role: str, clinic_id: object) -> str:
    """Issue the same JWT the API will verify (app.core.security)."""
    from app.core.security import create_access_token

    return create_access_token(subject=str(user_id), role=role, clinic_id=str(clinic_id))


@pytest.fixture
def auth_headers(seed_db: dict, roles: dict[str, tuple[object, object]]):
    """Return JWT auth headers for a given role (factory)."""

    def _headers(role: str) -> dict[str, str]:
        user_id, clinic_id = roles[role]
        return {"Authorization": f"Bearer {_jwt_for(user_id, role, clinic_id)}"}

    return _headers


@pytest_asyncio.fixture
async def test_client(test_database: dict):
    """Bare httpx ASGI client against the FastAPI app (no auth header)."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def role_client(test_database: dict, roles: dict[str, tuple[object, object]]):
    """Factory returning an httpx client carrying a JWT for a given role."""

    async def _factory(role: str) -> AsyncClient:
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        user_id, clinic_id = roles[role]
        headers = {"Authorization": f"Bearer {_jwt_for(user_id, role, clinic_id)}"}
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        )

    return _factory


@pytest_asyncio.fixture
async def staff_client(role_client):
    client = await role_client("staff")
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def clinician_client(role_client):
    client = await role_client("clinician")
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def patient_client(role_client):
    client = await role_client("patient")
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def admin_client(role_client):
    client = await role_client("admin")
    try:
        yield client
    finally:
        await client.aclose()
