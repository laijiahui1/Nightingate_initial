"""User directory route (assignee dropdown). Clinical-only, clinic-scoped."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.db.session import get_db

router = APIRouter()


@router.get("/users")
def list_users(
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> list[dict]:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="user directory is clinical-only")

    rows = db.execute(
        text(
            """
            SELECT id, full_name, email, role
            FROM users
            WHERE clinic_id = app_clinic_id() AND is_active
            ORDER BY full_name
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]
