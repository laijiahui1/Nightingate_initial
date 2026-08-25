"""FastAPI auth dependency: decode the Bearer JWT and set the RLS session context.

Every endpoint that can read patient data depends on :func:`get_current_actor`.
It decodes the ``Authorization: Bearer <token>`` header, validates the identity
claims, then calls :func:`app.db.session.set_app_context` so the request runs as
the verified role class under Row-Level Security (Pattern A, docs/SECURITY.md
§a). The decoded identity is returned as a frozen :class:`Actor`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.session import ROLE_CLASS_BY_NAME, get_db, set_app_context

# The four caller roles a JWT may carry. ``system`` is a key in
# ROLE_CLASS_BY_NAME but is an internal ingestion identity, never a caller.
_CALLER_ROLES: frozenset[str] = frozenset({"patient", "staff", "clinician", "admin"})


@dataclass(frozen=True)
class Actor:
    """The verified identity for the current request."""

    user_id: uuid.UUID
    role: str  # one of patient|staff|clinician|admin (never 'system')
    clinic_id: uuid.UUID


def get_current_actor(
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
) -> Actor:
    """Resolve the request identity from a Bearer JWT and set the RLS context.

    Raises ``401`` for a missing/malformed token, an undecodable token, or a
    role claim outside the four caller roles; ``403`` when the claim is the
    internal ``system`` role (which must never call the API directly).
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("Bearer ") :].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")

    try:
        claims = decode_access_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid token") from None

    role = claims.get("role")
    sub = claims.get("sub")
    clinic_id = claims.get("clinic_id")

    if role == "system":
        raise HTTPException(status_code=403, detail="system role cannot call the API")
    if role not in _CALLER_ROLES or role not in ROLE_CLASS_BY_NAME:
        raise HTTPException(status_code=401, detail="invalid role claim")

    try:
        user_id = uuid.UUID(str(sub))
        clinic_uuid = uuid.UUID(str(clinic_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=401, detail="invalid identity claims") from None

    set_app_context(db, role=role, user_id=user_id, clinic_id=clinic_uuid)
    return Actor(user_id=user_id, role=role, clinic_id=clinic_uuid)
