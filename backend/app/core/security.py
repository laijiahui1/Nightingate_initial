"""JWT issuance/verification and password hashing primitives.

Auth here is deliberately minimal (per docs/ARCHITECTURE.md §10): the JWT is
a thin token carrier for the demo. The real security controls are PostgreSQL
RLS (`app/db/rls.sql`) plus the API dependency guards that decode these
tokens and set the session GUCs (`SET LOCAL app.user_id/role/clinic_id`).

Password hashing is kept as a stub for M1 login work. passlib + bcrypt>=4
have a known version-detection incompatibility, so the foundation defaults to
the pure-Python `pbkdf2_sha256` scheme (no C extension); swap to `["bcrypt"]`
for a production deployment.
"""

import datetime as dt
import secrets
from typing import Any

import jwt
from passlib.context import CryptContext

from app.core.config import get_settings

# Role precedence used for cross-stream conflict tie-breaks (M3/M4): higher
# wins when a clinician edit conflicts with an AI-scribed or patient entry.
ROLE_PRECEDENCE: dict[str, int] = {
    "patient": 0,
    "staff": 1,
    "clinician": 2,
    "admin": 3,
}

_pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def create_access_token(
    *,
    subject: str,
    role: str,
    clinic_id: str,
    expires_delta: dt.timedelta | None = None,
) -> str:
    """Create a signed JWT carrying the role claims the API depends on."""
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    if expires_delta is None:
        expires_delta = dt.timedelta(minutes=settings.jwt_expire_minutes)

    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "clinic_id": clinic_id,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify and decode a JWT. Raises `jwt.PyJWTError` on any failure."""
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def role_outranks(actor_role: str, other_role: str) -> bool:
    """True when `actor_role` has strictly higher precedence than `other_role`."""
    return ROLE_PRECEDENCE.get(actor_role, 0) > ROLE_PRECEDENCE.get(other_role, 0)


def hash_password(password: str) -> str:
    """Hash a plaintext password (used by the M1 login flow)."""
    return _pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored hash."""
    return _pwd_context.verify(plain, hashed)


def random_secret(nbytes: int = 32) -> str:
    """Generate a strong URL-safe secret for populating `.env` at setup time."""
    return secrets.token_urlsafe(nbytes)
