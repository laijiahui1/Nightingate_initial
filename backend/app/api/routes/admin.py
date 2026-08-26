"""Admin-only maintenance routes (data decay)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.api.schemas import DecayRequest
from app.db.session import get_db

router = APIRouter()


@router.post("/admin/decay")
def decay_entries(
    payload: DecayRequest,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    if actor.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")

    result = (
        db.execute(
            text("SELECT decay_old_entries(:days) AS decayed"),
            {"days": payload.days},
        )
        .mappings()
        .first()
    )
    db.commit()
    return {"decayed": result["decayed"]}
