"""Entry edit/revert routes, wired to the SECURITY DEFINER save/revert paths.

``save_entry`` / ``revert_entry`` (rls.sql) run as the owner and bypass RLS, so
they validate clinic/author_role/section explicitly. The routes call them only
after :func:`get_current_actor` has SET ROLE'd the session and set the GUCs, and
map their Postgres exceptions to 403/404/422 without leaking existence.
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


@router.put("/entries/{entry_id}")
def edit_entry(
    entry_id: uuid.UUID,
    payload: EntryEditRequest,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
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
                "role": actor.role,
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
    try:
        db.execute(
            text(
                "SELECT revert_entry(:entry_id, :target_version, :user_id, :role)"
            ),
            {
                "entry_id": str(entry_id),
                "target_version": payload.target_version,
                "user_id": str(actor.user_id),
                "role": actor.role,
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
