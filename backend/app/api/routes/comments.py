"""Comment posting (threaded) + @mention + resolve/unresolve routes.

All writes are RLS-enforced (rls.sql ``comment_*`` policies). The route binds
``author_id = actor.user_id`` as defense in depth (the staff/clinician INSERT
policies do not require it) and resolves @mentions against the caller's clinic.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.api.routes._audit import audit
from app.api.schemas import CommentCreate
from app.db.session import get_db

router = APIRouter()

_MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9._-]*)")


def _author_role_for(actor: Actor) -> str:
    if actor.role == "admin":
        return "staff"  # documented approximation; author_id is the admin's real id
    return actor.role


def _comment_summary(
    returned: dict,
    *,
    entry_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    patient_id: uuid.UUID,
    author_id: uuid.UUID,
    author_role: str,
    body: str,
    visibility: str,
) -> dict:
    return {
        "id": returned["id"],
        "entry_id": entry_id,
        "parent_id": parent_id,
        "author_role": author_role,
        "body": body,
        "status": "open",
        "visibility": visibility,
        "created_at": returned["created_at"],
        "updated_at": returned["updated_at"],
        "patient_id": patient_id,
        "author_id": author_id,
        "resolved_by": None,
        "resolved_at": None,
    }


@router.post("/entries/{entry_id}/comments", status_code=201)
def create_comment(
    entry_id: uuid.UUID,
    payload: CommentCreate,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    # 1. Pre-check the entry is visible to the caller (404, not a leak).
    row = (
        db.execute(
            text(
                "SELECT id, patient_id FROM entry WHERE id = :eid AND clinic_id = app_clinic_id()"
            ),
            {"eid": str(entry_id)},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="entry not found")

    patient_id = row["patient_id"]
    author_role = _author_role_for(actor)
    visibility = (
        "patient_visible" if actor.role == "patient" else (payload.visibility or "internal")
    )

    # 2. Resolve @mentions (clinical roles only). Never run for patients.
    mentioned_ids: list[uuid.UUID] = []
    if actor.role != "patient":
        tokens = _MENTION_RE.findall(payload.body)
        for token in tokens:
            match = (
                db.execute(
                    text(
                        """
                    SELECT id FROM users
                    WHERE clinic_id = app_clinic_id() AND is_active AND id <> :me
                      AND (lower(email) LIKE lower(:email_prefix)
                           OR lower(full_name) LIKE lower(:name_prefix))
                    """
                    ),
                    {
                        "me": str(actor.user_id),
                        "email_prefix": f"{token}@%",
                        "name_prefix": f"{token}%",
                    },
                )
                .mappings()
                .all()
            )
            for m in match:
                if m["id"] not in mentioned_ids:
                    mentioned_ids.append(m["id"])

    try:
        # 3. Insert the comment (RETURNING works for all four roles).
        inserted = (
            db.execute(
                text(
                    """
                INSERT INTO comment(clinic_id, patient_id, entry_id, parent_id,
                                    author_id, author_role, body, visibility)
                VALUES (:clinic, :pid, :eid, :parent, :uid, :author_role, :body, :vis)
                RETURNING id, created_at, updated_at
                """
                ),
                {
                    "clinic": str(actor.clinic_id),
                    "pid": str(patient_id),
                    "eid": str(entry_id),
                    "parent": str(payload.parent_id) if payload.parent_id else None,
                    "uid": str(actor.user_id),
                    "author_role": author_role,
                    "body": payload.body,
                    "vis": visibility,
                },
            )
            .mappings()
            .first()
        )

        # 4. Insert mention rows (one notification per @per comment).
        for mentioned_id in mentioned_ids:
            db.execute(
                text(
                    """
                    INSERT INTO mention(clinic_id, comment_id, mentioned_user_id, created_by)
                    VALUES (:clinic, :comment_id, :mentioned_user_id, :created_by)
                    ON CONFLICT (comment_id, mentioned_user_id) DO NOTHING
                    """
                ),
                {
                    "clinic": str(actor.clinic_id),
                    "comment_id": str(inserted["id"]),
                    "mentioned_user_id": str(mentioned_id),
                    "created_by": str(actor.user_id),
                },
            )

        # 5. Audit (patient is skipped inside the helper).
        audit(
            db,
            actor,
            action="comment",
            target_type="comment",
            target_id=inserted["id"],
            patient_id=patient_id,
            metadata={"mention_count": len(mentioned_ids)},
        )

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:  # RLS violation, trigger, enum mismatch, etc.
        db.rollback()
        raise HTTPException(status_code=422, detail="comment operation failed") from exc

    return _comment_summary(
        inserted,
        entry_id=entry_id,
        parent_id=payload.parent_id,
        patient_id=patient_id,
        author_id=actor.user_id,
        author_role=author_role,
        body=payload.body,
        visibility=visibility,
    )


def _set_resolved(db: Session, actor: Actor, comment_id: uuid.UUID, resolved: bool) -> dict:
    row = (
        db.execute(
            text(
                """
            SELECT author_role, patient_id
            FROM comment
            WHERE id = :cid AND clinic_id = app_clinic_id()
            """
            ),
            {"cid": str(comment_id)},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="comment not found")

    author_role = row["author_role"]
    if actor.role != "admin":
        allowed = {
            "staff": {"patient", "staff"},
            "clinician": {"patient", "clinician"},
        }
        if author_role not in allowed.get(actor.role, frozenset()):
            raise HTTPException(
                status_code=403,
                detail=f"cannot resolve a comment authored by {author_role}",
            )

    try:
        if resolved:
            updated = (
                db.execute(
                    text(
                        """
                    UPDATE comment
                    SET status = 'resolved', resolved_by = :resolved_by, resolved_at = now()
                    WHERE id = :cid
                    RETURNING id, status, resolved_by, resolved_at
                    """
                    ),
                    {"resolved_by": str(actor.user_id), "cid": str(comment_id)},
                )
                .mappings()
                .first()
            )
        else:
            updated = (
                db.execute(
                    text(
                        """
                    UPDATE comment
                    SET status = 'open', resolved_by = NULL, resolved_at = NULL
                    WHERE id = :cid
                    RETURNING id, status, resolved_by, resolved_at
                    """
                    ),
                    {"cid": str(comment_id)},
                )
                .mappings()
                .first()
            )

        audit(
            db,
            actor,
            action="resolve_comment" if resolved else "unresolve_comment",
            target_type="comment",
            target_id=comment_id,
            patient_id=row["patient_id"],
        )

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail="comment operation failed") from exc

    if updated is None:
        raise HTTPException(status_code=404, detail="comment not found")
    return dict(updated)


@router.post("/comments/{comment_id}/resolve")
def resolve_comment(
    comment_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    return _set_resolved(db, actor, comment_id, resolved=True)


@router.post("/comments/{comment_id}/unresolve")
def unresolve_comment(
    comment_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    return _set_resolved(db, actor, comment_id, resolved=False)
