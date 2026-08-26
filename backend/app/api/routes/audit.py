"""Admin-only audit-log query (metadata only — never note content)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.db.session import get_db

router = APIRouter()

_TARGET_TYPES: frozenset[str] = frozenset({"entry", "comment", "highlight", "task", "patient"})


@router.get("/audit")
def get_audit(
    target_type: str,
    target_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    if actor.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    if target_type not in _TARGET_TYPES:
        raise HTTPException(status_code=422, detail="invalid target_type")

    rows = (
        db.execute(
            text(
                """
            SELECT id, target_type, target_id, version, actor_id, actor_role,
                   action, occurred_at
            FROM audit_log
            WHERE target_type = :tt AND target_id = :tid
            ORDER BY occurred_at DESC
            """
            ),
            {"tt": target_type, "tid": str(target_id)},
        )
        .mappings()
        .all()
    )
    return {"rows": [dict(r) for r in rows]}
