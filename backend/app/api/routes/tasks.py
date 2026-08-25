"""Task routes: list / create / update, clinical-only.

Writes are RLS-enforced (rls.sql ``task_*`` policies). The route keeps an
explicit in-clinic assignee check (a clean 422) in addition to the
``trg_task_assignee_in_clinic`` trigger (see spec §6).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.api.routes._audit import audit
from app.api.schemas import TaskCreate, TaskUpdate
from app.db.session import get_db

router = APIRouter()

_TASK_COLUMNS = (
    "id, patient_id, entry_id, assignee_id, assigner_id, title, description, "
    "status, priority, due_at, created_at, updated_at, completed_at"
)


def _patient_visible(db: Session, patient_id: uuid.UUID) -> None:
    row = db.execute(
        text("SELECT id FROM patient WHERE id = :pid"), {"pid": str(patient_id)}
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="patient not found")


def list_tasks(db: Session, patient_id: uuid.UUID) -> list[dict]:
    rows = db.execute(
        text(
            f"""
            SELECT {_TASK_COLUMNS}
            FROM task
            WHERE patient_id = :pid
            ORDER BY CASE status
                       WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2
                     END, created_at DESC
            """
        ),
        {"pid": str(patient_id)},
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/patients/{patient_id}/tasks")
def get_tasks(
    patient_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> list[dict]:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="task list is clinical-only")
    _patient_visible(db, patient_id)
    return list_tasks(db, patient_id)


@router.post("/patients/{patient_id}/tasks", status_code=201)
def create_task(
    patient_id: uuid.UUID,
    payload: TaskCreate,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="task list is clinical-only")
    _patient_visible(db, patient_id)

    # Explicit in-clinic assignee check (defense in depth alongside the trigger).
    assignee = db.execute(
        text("SELECT id FROM users WHERE id = :aid AND clinic_id = app_clinic_id()"),
        {"aid": str(payload.assignee_id)},
    ).first()
    if assignee is None:
        raise HTTPException(
            status_code=422, detail="task assignee must belong to the task clinic"
        )

    try:
        inserted = db.execute(
            text(
                f"""
                INSERT INTO task(clinic_id, patient_id, entry_id, assignee_id,
                                 assigner_id, title, description, status, priority, due_at)
                VALUES (:clinic, :pid, :entry_id, :assignee_id, :uid, :title, :desc,
                        'open', :priority, :due_at)
                RETURNING {_TASK_COLUMNS}
                """
            ),
            {
                "clinic": str(actor.clinic_id),
                "pid": str(patient_id),
                "entry_id": str(payload.entry_id) if payload.entry_id else None,
                "assignee_id": str(payload.assignee_id),
                "uid": str(actor.user_id),
                "title": payload.title,
                "desc": payload.description,
                "priority": payload.priority,
                "due_at": payload.due_at,
            },
        ).mappings().first()

        audit(
            db,
            actor,
            action="assign_task",
            target_type="task",
            target_id=inserted["id"],
            patient_id=patient_id,
            metadata={"assignee_id": str(payload.assignee_id)},
        )

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail="task operation failed") from exc

    return dict(inserted)


@router.patch("/tasks/{task_id}")
def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="task list is clinical-only")

    pre = db.execute(
        text(
            "SELECT patient_id, status FROM task WHERE id = :tid AND clinic_id = app_clinic_id()"
        ),
        {"tid": str(task_id)},
    ).mappings().first()
    if pre is None:
        raise HTTPException(status_code=404, detail="task not found")

    updates = payload.model_dump(exclude_unset=True)
    sets: list[str] = []
    params: dict = {"tid": str(task_id)}

    if "title" in updates:
        sets.append("title = :title")
        params["title"] = updates["title"]
    if "description" in updates:
        sets.append("description = :description")
        params["description"] = updates["description"]
    if "assignee_id" in updates:
        aid = db.execute(
            text("SELECT id FROM users WHERE id = :aid AND clinic_id = app_clinic_id()"),
            {"aid": str(updates["assignee_id"])},
        ).first()
        if aid is None:
            raise HTTPException(
                status_code=422, detail="task assignee must belong to the task clinic"
            )
        sets.append("assignee_id = :assignee_id")
        params["assignee_id"] = str(updates["assignee_id"])
    if "priority" in updates:
        sets.append("priority = :priority")
        params["priority"] = updates["priority"]
    if "due_at" in updates:
        sets.append("due_at = :due_at")
        params["due_at"] = updates["due_at"]
    if "status" in updates:
        sets.append("status = :status")
        params["status"] = updates["status"]
        if updates["status"] in ("done", "cancelled"):
            sets.append("completed_at = now()")
        else:
            sets.append("completed_at = NULL")

    if not sets:
        return dict(pre)

    try:
        updated = db.execute(
            text(f"UPDATE task SET {', '.join(sets)} WHERE id = :tid RETURNING {_TASK_COLUMNS}"),
            params,
        ).mappings().first()

        audit(
            db,
            actor,
            action="update_task",
            target_type="task",
            target_id=task_id,
            patient_id=pre["patient_id"],
            metadata={"status": updates.get("status")},
        )

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail="task operation failed") from exc

    return dict(updated)
