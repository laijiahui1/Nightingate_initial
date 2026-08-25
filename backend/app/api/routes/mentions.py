"""Mention routes: unread inbox + mark-read. Clinical-only.

The unread query joins the mention to its comment/entry/patient for a compact
context object (the comment body is truncated to 200 chars to keep the payload
small). Patients have no SELECT grant on ``mention``, so they are rejected up
front.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.db.session import get_db

router = APIRouter()


@router.get("/mentions/unread")
def unread_mentions(
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> list[dict]:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="mentions are clinical-only")

    rows = db.execute(
        text(
            """
            SELECT m.id, m.comment_id, m.mentioned_user_id, m.created_by,
                   m.read_at, m.created_at,
                   c.body AS comment_body, c.author_role AS comment_author_role,
                   e.title AS entry_title, e.id AS entry_id, e.patient_id AS patient_id,
                   p.display_name AS patient_name
            FROM mention m
            LEFT JOIN comment c ON c.id = m.comment_id
            LEFT JOIN entry e ON e.id = COALESCE(m.entry_id, c.entry_id)
            LEFT JOIN patient p ON p.id = e.patient_id
            WHERE m.mentioned_user_id = app_user_id() AND m.read_at IS NULL
            ORDER BY m.created_at DESC
            """
        )
    ).mappings().all()

    result = []
    for r in rows:
        body = r["comment_body"]
        result.append(
            {
                "id": r["id"],
                "comment_id": r["comment_id"],
                "entry_id": r["entry_id"],
                "mentioned_user_id": r["mentioned_user_id"],
                "created_by": r["created_by"],
                "read_at": r["read_at"],
                "created_at": r["created_at"],
                "context": {
                    "comment_body": body[:200] if body else None,
                    "comment_author_role": r["comment_author_role"],
                    "entry_title": r["entry_title"],
                    "entry_id": r["entry_id"],
                    "patient_name": r["patient_name"],
                    "patient_id": r["patient_id"],
                },
            }
        )
    return result


@router.post("/mentions/{mention_id}/read")
def mark_mention_read(
    mention_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="mentions are clinical-only")

    row = db.execute(
        text(
            """
            UPDATE mention
            SET read_at = now()
            WHERE id = :mid AND mentioned_user_id = app_user_id()
            RETURNING id, read_at
            """
        ),
        {"mid": str(mention_id)},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="mention not found")
    db.commit()
    return dict(row)
