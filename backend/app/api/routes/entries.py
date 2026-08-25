"""Entry edit/revert/history routes, wired to the SECURITY DEFINER save/revert paths.

``save_entry`` / ``revert_entry`` (rls.sql) run as the owner and bypass RLS, so
they validate clinic/author_role/section explicitly. The routes call them only
after :func:`get_current_actor` has SET ROLE'd the session and set the GUCs, and
map their Postgres exceptions to 403/404/422 without leaking existence.

The M3 fix resolves ``role_arg`` explicitly (never passing ``actor.role`` raw):
``admin`` is not a valid ``entry_author_role`` value, so an admin edit/revert
passes the entry's own author_role instead; a cross-role writer is rejected 403
before it can smuggle a role into the SECURITY DEFINER function.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.api.schemas import EntryEditRequest, RevertRequest
from app.db.session import get_db, set_app_context

router = APIRouter()


def _error_text(exc: Exception) -> str:
    parts = [str(exc)]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts.append(str(orig))
    return "\n".join(parts)


def _raise_for_entry_error(exc: Exception) -> None:
    """Map save_entry/revert_entry Postgres errors to HTTP statuses."""
    msg = _error_text(exc)
    if "entry not found" in msg or "is not in the entry clinic" in msg:
        raise HTTPException(status_code=404, detail="entry not found")
    if "cannot edit an entry authored by" in msg or "cannot revert an entry authored by" in msg:
        raise HTTPException(status_code=403, detail="forbidden")
    if "cannot write section" in msg or "cannot revert section" in msg:
        raise HTTPException(status_code=403, detail="forbidden")
    raise HTTPException(status_code=422, detail="entry operation failed")


def _resolve_role_arg(db: Session, entry_id: uuid.UUID, actor: Actor) -> str:
    """Resolve the ``entry_author_role`` to pass to save/revert.

    Raises 404 when the entry is out of scope (cross-clinic / nonexistent) and
    403 when the caller's role cannot write the entry (write isolation).
    """
    row = db.execute(
        text(
            "SELECT author_role FROM entry WHERE id = :id AND clinic_id = app_clinic_id()"
        ),
        {"id": str(entry_id)},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="entry not found")

    entry_author_role = row["author_role"]
    if actor.role == "admin":
        return entry_author_role  # admin may edit any in-clinic entry
    if actor.role == entry_author_role:
        return actor.role
    raise HTTPException(status_code=403, detail="forbidden")


@router.get("/entries/{entry_id}/history")
def entry_history(
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> list[dict]:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="entry history is clinical-only")
    rows = db.execute(
        text(
            """
            SELECT id, version, body, delta_from_prev, author_role, author_id,
                   change_summary, conflict_flag, conflict_of, created_at
            FROM entry_version
            WHERE entry_id = :eid
            ORDER BY version DESC
            """
        ),
        {"eid": str(entry_id)},
    ).mappings().all()
    return [dict(r) for r in rows]


@router.put("/entries/{entry_id}")
def edit_entry(
    entry_id: uuid.UUID,
    payload: EntryEditRequest,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    role_arg = _resolve_role_arg(db, entry_id, actor)
    try:
        db.execute(
            text(
                "SELECT save_entry(:entry_id, :body, :base_version, :user_id, :role)"
            ),
            {
                "entry_id": str(entry_id),
                "body": payload.body,
                "base_version": payload.base_version,
                "user_id": str(actor.user_id),
                "role": role_arg,
            },
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        _raise_for_entry_error(exc)

    # db.commit() ended the transaction, which reset SET LOCAL ROLE. Re-apply the
    # caller's role context so the re-SELECT runs under RLS as the caller (not as
    # the base app_nightingale role, which has no SELECT on entry).
    set_app_context(db, role=actor.role, user_id=actor.user_id, clinic_id=actor.clinic_id)
    row = db.execute(
        text("SELECT id, version, body, updated_at FROM entry WHERE id = :id"),
        {"id": str(entry_id)},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return dict(row)


@router.post("/entries/{entry_id}/revert")
def revert_entry(
    entry_id: uuid.UUID,
    payload: RevertRequest,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    role_arg = _resolve_role_arg(db, entry_id, actor)
    try:
        db.execute(
            text(
                "SELECT revert_entry(:entry_id, :target_version, :user_id, :role)"
            ),
            {
                "entry_id": str(entry_id),
                "target_version": payload.target_version,
                "user_id": str(actor.user_id),
                "role": role_arg,
            },
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        _raise_for_entry_error(exc)

    # Re-apply the caller's role context after db.commit() reset SET LOCAL ROLE.
    set_app_context(db, role=actor.role, user_id=actor.user_id, clinic_id=actor.clinic_id)
    row = db.execute(
        text("SELECT id, version, body FROM entry WHERE id = :id"),
        {"id": str(entry_id)},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return dict(row)


@router.post("/entries/{entry_id}/restore")
def restore_entry_route(
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="restore is clinical-only")

    # Verify the entry is visible to the caller (RLS hides cross-clinic/missing).
    visible = db.execute(
        text("SELECT id FROM entry WHERE id = :id AND clinic_id = app_clinic_id()"),
        {"id": str(entry_id)},
    ).first()
    if visible is None:
        raise HTTPException(status_code=404, detail="entry not found")

    try:
        result = db.execute(
            text("SELECT restore_entry(:eid) AS body"),
            {"eid": str(entry_id)},
        ).mappings().first()
        db.commit()
    except Exception as exc:
        db.rollback()
        msg = _error_text(exc)
        if "no archived body" in msg:
            raise HTTPException(status_code=404, detail="no archived body")
        if "cross-clinic" in msg:
            raise HTTPException(status_code=404, detail="entry not found")
        raise HTTPException(status_code=422, detail="restore failed") from exc
    return {"body": result["body"]}
