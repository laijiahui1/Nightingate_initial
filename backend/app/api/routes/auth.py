"""Dev demo login (gated to APP_ENV=development).

The JWT is a thin token carrier for the demo; production auth is OIDC
(docs/ARCHITECTURE.md §10). The schema has no password column, so this route is
a documented dev shortcut, NOT a real auth scheme.

RLS note: ``users`` is FORCE RLS with no policy that matches before a clinic GUC
exists, and the restricted ``app_nightingale`` role has no SELECT grant on it.
The pre-auth email→identity lookup is therefore the one bootstrap read that runs
on a short-lived superuser connection (``MIGRATION_DATABASE_URL`` — the same
bootstrap credential ``app.seed`` uses), reading only identity metadata (never
PHI). All patient-data reads go through :func:`get_current_actor` under RLS.
"""

from __future__ import annotations

import os

import psycopg
from fastapi import APIRouter, HTTPException

from app.api.schemas import LoginRequest
from app.core.config import get_settings
from app.core.security import create_access_token

router = APIRouter()

_IDENTITY_COLUMNS = ("id", "clinic_id", "full_name", "email", "role", "is_active")


def _normalize(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def _lookup_identity(email: str) -> dict | None:
    url = _normalize(
        os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("SEED_DATABASE_URL") or ""
    )
    if not url:
        raise HTTPException(status_code=503, detail="login unavailable (no bootstrap DB URL)")
    try:
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT id, clinic_id, full_name, email, role, is_active "
                "FROM users WHERE email = %s",
                (email,),
            ).fetchone()
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="login unavailable") from exc
    if row is None:
        return None
    return dict(zip(_IDENTITY_COLUMNS, row))


@router.post("/auth/login")
def login(payload: LoginRequest) -> dict:
    settings = get_settings()
    if settings.app_env != "development":
        raise HTTPException(status_code=403, detail="login disabled outside development")

    identity = _lookup_identity(payload.email)
    if identity is None or not identity["is_active"]:
        raise HTTPException(status_code=401, detail="invalid credentials")

    token = create_access_token(
        subject=str(identity["id"]),
        role=identity["role"],
        clinic_id=str(identity["clinic_id"]),
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": identity["role"],
        "clinic_id": identity["clinic_id"],
        "full_name": identity["full_name"],
        "email": identity["email"],
    }
