"""Shared audit-writing helper.

``log_audit`` (rls.sql) is EXECUTE-granted only to ``system_pipeline``, so any
human-role write to ``audit_log`` must SET LOCAL ROLE into the pipeline first —
the same dance the ai-scribe route uses (M2). The dance is transaction-scoped and
must run inside the caller's request transaction BEFORE ``db.commit()``.

Patients are skipped entirely: they cannot ``SET ROLE system_pipeline`` and have
no audit path (documented in the M3 spec).
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Actor
from app.db.session import ROLE_CLASS_BY_NAME


def audit(
    db: Session,
    actor: Actor,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    patient_id: uuid.UUID | None,
    version: int | None = None,
    metadata: dict | None = None,
) -> None:
    """Write one ``audit_log`` row via the system_pipeline role dance."""
    if actor.role == "patient":
        return  # patient has no audit path
    role_arg = "staff" if actor.role == "admin" else actor.role
    db.execute(text("SET LOCAL ROLE system_pipeline"))
    db.execute(text("SELECT set_config('app.role', 'system', true)"))
    db.execute(
        text(
            "SELECT log_audit(:actor_id, :actor_role, :action, :target_type, "
            ":target_id, :patient_id, :clinic_id, :version, CAST(:metadata AS jsonb))"
        ),
        {
            "actor_id": str(actor.user_id),
            "actor_role": role_arg,
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id),
            "patient_id": str(patient_id) if patient_id is not None else None,
            "clinic_id": str(actor.clinic_id),
            "version": version,
            "metadata": json.dumps(metadata or {}),
        },
    )
    db.execute(text(f"SET LOCAL ROLE {ROLE_CLASS_BY_NAME[actor.role]}"))
    db.execute(
        text("SELECT set_config('app.role', :role, true)"), {"role": actor.role}
    )
