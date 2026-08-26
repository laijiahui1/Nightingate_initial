"""Patient-facing routes: roster, page bundle, glance top-card, AI scribe.

All queries are raw ``text()`` against the restricted connection; Row-Level
Security (rls.sql) scopes rows to the caller's role class and clinic. The
explicit ``SELECT id FROM patient WHERE id=:pid`` gives a clean 404 for
out-of-scope ids without leaking existence.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.api.routes.tasks import list_tasks
from app.api.schemas import AiScribeRequest
from app.db.session import ROLE_CLASS_BY_NAME, get_db
from app.services.llm_gateway import LLMGatewayError, build_prompt, chat

router = APIRouter()

# Display labels used in the generated entry title (spec §1.3 step 5).
_SCRIBE_DISPLAY: dict[str, str] = {
    "ai_doctor_consult_summary": "AI Doctor Consult Summary",
    "ai_nurse_consult_summary": "AI Nurse Consult Summary",
    "ai_patient_session_summary": "AI Patient Session Summary",
}

# provenance.source_type is the `provenance_source` enum (schema.sql), which has
# no 'transcript' value. Map each scribe type to its matching provenance source.
_SCRIBE_TO_PROV_SOURCE: dict[str, str] = {
    "ai_doctor_consult_summary": "ai_doctor_consult",
    "ai_nurse_consult_summary": "ai_nurse_consult",
    "ai_patient_session_summary": "ai_patient_session",
}

_PROV_SOURCES: frozenset[str] = frozenset(
    {
        "ai_patient_session",
        "ai_doctor_consult",
        "ai_nurse_consult",
        "manual_note",
        "voice_capture",
        "system",
    }
)

_PIPELINE_VERSION = "m2-1.0"


def _patient_visible(db: Session, patient_id: uuid.UUID) -> None:
    """404 when the caller's role cannot see this patient (RLS already hides it)."""
    row = db.execute(
        text("SELECT id FROM patient WHERE id = :pid"), {"pid": str(patient_id)}
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="patient not found")


def _list_entries(db: Session, patient_id: uuid.UUID, actor: Actor) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            SELECT id, entry_type, title, body, author_role, section, visibility,
                   risk_level, version, status, created_at, updated_at
            FROM entry
            WHERE patient_id = :pid
            ORDER BY created_at DESC
            """
            ),
            {"pid": str(patient_id)},
        )
        .mappings()
        .all()
    )
    entries = [dict(r) for r in rows]

    # Attach AI-scribe metadata for system-authored entries. patient_role has no
    # SELECT grant on ai_scribed_note (rls.sql) and never sees internal AI notes,
    # so skip the join for patients entirely.
    system_ids = [str(e["id"]) for e in entries if e["author_role"] == "system"]
    ai_map: dict[str, dict] = {}
    if system_ids and actor.role != "patient":
        placeholders = ", ".join(f":id{i}" for i in range(len(system_ids)))
        params = {f"id{i}": system_ids[i] for i in range(len(system_ids))}
        ai_rows = (
            db.execute(
                text(
                    f"""
                SELECT entry_id, ai_note_type, model_name, redaction_confirmed
                FROM ai_scribed_note
                WHERE entry_id IN ({placeholders})
                """
                ),
                params,
            )
            .mappings()
            .all()
        )
        ai_map = {str(r["entry_id"]): dict(r) for r in ai_rows}

    for e in entries:
        ai = ai_map.get(str(e["id"]))
        e["ai"] = (
            {
                "ai_note_type": ai["ai_note_type"],
                "model_name": ai["model_name"],
                "redaction_confirmed": ai["redaction_confirmed"],
            }
            if ai
            else None
        )
    return entries


def _list_comments(db: Session, patient_id: uuid.UUID) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            SELECT id, entry_id, parent_id, author_role, body, status, created_at,
                   patient_id, author_id, resolved_by, resolved_at, updated_at
            FROM comment
            WHERE patient_id = :pid
            ORDER BY created_at ASC
            """
            ),
            {"pid": str(patient_id)},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@router.get("/patients")
def list_patients(
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
            SELECT id, mrn, display_name, date_of_birth, gender
            FROM patient
            ORDER BY display_name
            """
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@router.get("/patients/{patient_id}")
def get_patient_bundle(
    patient_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    _patient_visible(db, patient_id)
    # Patients have no SELECT grant on `task` — never query it for them.
    tasks = [] if actor.role == "patient" else list_tasks(db, patient_id)
    return {
        "entries": _list_entries(db, patient_id, actor),
        "comments": _list_comments(db, patient_id),
        "tasks": tasks,
    }


@router.get("/patients/{patient_id}/glance")
def get_glance(
    patient_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="glance is clinical-only")
    _patient_visible(db, patient_id)

    glance = _read_glance(db, patient_id)
    if glance is None or glance["invalidated"]:
        db.execute(text("SELECT recompute_glance(:pid)"), {"pid": str(patient_id)})
        glance = _read_glance(db, patient_id)

    if glance is None:
        return {
            "patient_id": patient_id,
            "top_items": [],
            "open_actions": [],
            "risk_flags": [],
            "computed_at": None,
            "invalidated": False,
        }
    return glance


def _read_glance(db: Session, patient_id: uuid.UUID) -> dict | None:
    row = (
        db.execute(
            text(
                """
            SELECT patient_id, top_items, open_actions, risk_flags,
                   computed_at, invalidated
            FROM patient_glance
            WHERE patient_id = :pid
            """
            ),
            {"pid": str(patient_id)},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


@router.post("/patients/{patient_id}/ai-scribe", status_code=201)
def ai_scribe(
    patient_id: uuid.UUID,
    payload: AiScribeRequest,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    if actor.role not in ("staff", "clinician", "admin"):
        raise HTTPException(status_code=403, detail="ai-scribe requires a clinical role")
    _patient_visible(db, patient_id)

    # 1. Build the prompt and run it through the redacting LLM chokepoint.
    try:
        messages = build_prompt(
            payload.scribe_type,
            source_text=payload.transcript,
            source_type=payload.source_type,
            external_ref=payload.external_ref,
        )
        result = chat(messages, purpose=payload.scribe_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except LLMGatewayError:
        raise HTTPException(status_code=422, detail="llm gateway error") from None

    visibility = payload.visibility or (
        "patient_visible" if payload.scribe_type == "ai_patient_session_summary" else "internal"
    )
    title = (
        f"{_SCRIBE_DISPLAY[payload.scribe_type]} — {dt.datetime.now(dt.UTC).strftime('%Y-%m-%d')}"
    )
    source_type = payload.source_type or _SCRIBE_TO_PROV_SOURCE[payload.scribe_type]
    if source_type not in _PROV_SOURCES:
        raise HTTPException(status_code=422, detail="invalid source_type")

    try:
        # 2. Switch into the AI-ingestion role (author_role='system'). app.user_id
        #    and app.clinic_id stay unchanged so RLS stays clinic-scoped.
        db.execute(text("SET LOCAL ROLE system_pipeline"))
        db.execute(text("SELECT set_config('app.role', 'system', true)"))

        # system_pipeline has INSERT but NOT SELECT on provenance/entry
        # (rls.sql), so `RETURNING id` would fail with "permission denied".
        # Generate the ids client-side and INSERT them explicitly instead.
        prov_id = uuid.uuid4()
        db.execute(
            text(
                """
                INSERT INTO provenance(id, clinic_id, patient_id, source_type,
                                       external_ref, payload)
                VALUES (:id, :clinic_id, :patient_id, :source_type, :external_ref, '{}')
                """
            ),
            {
                "id": str(prov_id),
                "clinic_id": str(actor.clinic_id),
                "patient_id": str(patient_id),
                "source_type": source_type,
                "external_ref": payload.external_ref,
            },
        )

        entry_id = uuid.uuid4()
        db.execute(
            text(
                """
                INSERT INTO entry(id, clinic_id, patient_id, author_role, entry_type,
                                  title, body, section, visibility, provenance_id,
                                  version, status)
                VALUES (:id, :clinic_id, :patient_id, 'system', :entry_type,
                        :title, :body, NULL, :visibility, :provenance_id, 1, 'final')
                """
            ),
            {
                "id": str(entry_id),
                "clinic_id": str(actor.clinic_id),
                "patient_id": str(patient_id),
                "entry_type": payload.scribe_type,
                "title": title,
                "body": result["content"],
                "visibility": visibility,
                "provenance_id": str(prov_id),
            },
        )

        db.execute(
            text(
                """
                INSERT INTO ai_scribed_note(entry_id, clinic_id, ai_note_type,
                                            model_name, pipeline_version,
                                            source_session_id, redaction_confirmed,
                                            redaction_mask, clinical_summary,
                                            raw_confidence)
                VALUES (:entry_id, :clinic_id, :ai_note_type, :model_name,
                        :pipeline_version, :source_session_id, true,
                        CAST(:redaction_mask AS jsonb), :clinical_summary, NULL)
                """
            ),
            {
                "entry_id": entry_id,
                "clinic_id": str(actor.clinic_id),
                "ai_note_type": payload.scribe_type,
                "model_name": result["model"],
                "pipeline_version": _PIPELINE_VERSION,
                "source_session_id": payload.external_ref,
                "redaction_mask": json.dumps(result["redaction_mask"]),
                "clinical_summary": result["content"][:2000],
            },
        )

        # 3. Restore the caller's role class + role GUC for any follow-up query.
        db.execute(text(f"SET LOCAL ROLE {ROLE_CLASS_BY_NAME[actor.role]}"))
        db.execute(text("SELECT set_config('app.role', :role, true)"), {"role": actor.role})

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:  # RLS violation, trigger, enum mismatch, etc.
        db.rollback()
        # Never echo raw PHI-bearing error text; return a sanitized message.
        raise HTTPException(status_code=422, detail="ai-scribe ingestion failed") from exc

    return {
        "entry_id": entry_id,
        "entry_type": payload.scribe_type,
        "ai_note_type": payload.scribe_type,
        "model_name": result["model"],
        "redaction_confirmed": True,
        "redaction_mask": result["redaction_mask"],
        "visibility": visibility,
    }
