"""Highlight routes: list / generate / accept / reject.

Generation is deterministic (no LLM): the route scans the source entry body for
the first ranked risk phrase not already covered by an existing highlight (dedupe
by exact ``quoted_text``, any status) and freezes a resolvable span
``(offset_start, offset_end, quoted_text)`` that slices the authored body exactly.

A generated highlight has ``source='ai'``, which staff/clinician cannot insert
(their ``hl_ins_*`` policies require ``source='manual'``) — so the generate route
uses the same ``SET LOCAL ROLE system_pipeline`` dance as ai-scribe. The pipeline
has no SELECT on ``entry``/``highlight``, so the body is read AS THE CALLER first
and the span is computed in Python; the pipeline only INSERTs (highlight, and a
provenance row when the entry has none) and writes the audit row.

Accept/reject transition ``suggested -> accepted|rejected`` under the
``hl_upd_*`` policies, audit via the M3 ``_audit`` helper, and feed the M6
self-learning loop by calling ``record_interaction`` with the entry's entity
features.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.api.routes._audit import audit
from app.api.schemas import HighlightGenerateRequest, HighlightSummary
from app.db.session import ROLE_CLASS_BY_NAME, get_db, set_app_context

router = APIRouter()

# Ranked risk phrases: (keyword, risk_level, reason, confidence). Order matters —
# the first phrase present in the body and not already highlighted wins.
_RISK_PHRASES: list[tuple[str, str, str, float]] = [
    ("Penicillin", "critical", "Penicillin allergy documented — critical for prescribing", 0.95),
    ("BP 148/92", "high", "Elevated blood pressure (148/92) requires follow-up", 0.90),
    ("chest tightness", "high", "Chest tightness — rule out cardiac cause", 0.90),
    ("hypertension", "high", "Hypertension in assessment — review management", 0.90),
    (
        "Cardiologist referral",
        "high",
        "Cardiologist referral indicated if BP remains elevated",
        0.90,
    ),    ("dizziness", "medium", "Episodes of dizziness — possible side effect; monitor", 0.85),
    ("monitor", "low", "Ongoing monitoring requested", 0.80),
]

_HL_COLS = (
    "id, patient_id, entry_id, offset_start, offset_end, quoted_text, risk_reason, "
    "risk_level, source, status, confidence, provenance_id, created_by, resolved_by, "
    "resolved_at, created_at"
)


def _patient_visible(db: Session, patient_id: uuid.UUID) -> None:
    """404 when the caller cannot see this patient (RLS already hides it)."""
    row = db.execute(
        text("SELECT id FROM patient WHERE id = :pid"), {"pid": str(patient_id)}
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="patient not found")


def _existing_quotes(db: Session, entry_id: uuid.UUID) -> set[str]:
    rows = db.execute(
        text("SELECT quoted_text FROM highlight WHERE entry_id = :eid"),
        {"eid": str(entry_id)},
    ).mappings().all()
    return {r["quoted_text"] for r in rows}


@router.get("/patients/{patient_id}/highlights")
def list_highlights(
    patient_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> dict:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="highlights are clinical-only")
    _patient_visible(db, patient_id)

    rows = db.execute(
        text(
            f"""
            SELECT {_HL_COLS}
            FROM highlight
            WHERE patient_id = :pid
            ORDER BY CASE status WHEN 'suggested' THEN 0 ELSE 1 END, created_at DESC
            """
        ),
        {"pid": str(patient_id)},
    ).mappings().all()
    return {"highlights": [HighlightSummary(**dict(r)).model_dump() for r in rows]}


@router.post("/patients/{patient_id}/highlights/generate")
def generate_highlights(
    patient_id: uuid.UUID,
    payload: HighlightGenerateRequest,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> HighlightSummary:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="highlights are clinical-only")
    _patient_visible(db, patient_id)

    # Read the source entry AS THE CALLER (RLS-scoped); the pipeline role has no
    # SELECT on entry, so the span is computed here in Python.
    entry = db.execute(
        text(
            """
            SELECT id, body, provenance_id
            FROM entry
            WHERE id = :eid AND patient_id = :pid AND clinic_id = app_clinic_id()
            """
        ),
        {"eid": str(payload.entry_id), "pid": str(patient_id)},
    ).mappings().first()
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")

    body: str = entry["body"]
    covered = _existing_quotes(db, payload.entry_id)

    phrase: tuple[str, str, str, float] | None = None
    for keyword, level, reason, conf in _RISK_PHRASES:
        if keyword in body and keyword not in covered:
            phrase = (keyword, level, reason, conf)
            break

    if phrase is None:
        # Idempotent re-generate: surface the highest-risk existing suggested
        # highlight for this entry instead of spamming duplicates.
        existing = db.execute(
            text(
                f"""
                SELECT {_HL_COLS}
                FROM highlight
                WHERE entry_id = :eid AND status = 'suggested'
                ORDER BY CASE risk_level
                    WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2 ELSE 3 END, confidence DESC NULLS LAST
                LIMIT 1
                """
            ),
            {"eid": str(payload.entry_id)},
        ).mappings().first()
        if existing is None:
            raise HTTPException(status_code=422, detail="no risk span detected in the source entry")
        return HighlightSummary(**dict(existing))

    keyword, level, reason, conf = phrase
    offset_start = body.find(keyword)
    offset_end = offset_start + len(keyword)
    quoted_text = body[offset_start:offset_end]

    prov_id = entry["provenance_id"]
    highlight_id = uuid.uuid4()

    try:
        # Switch into the AI-ingestion pipeline role (source='ai' highlights are
        # pipeline-only per the hl_ins_system policy).
        db.execute(text("SET LOCAL ROLE system_pipeline"))
        db.execute(text("SELECT set_config('app.role', 'system', true)"))

        if prov_id is None:
            prov_id = uuid.uuid4()
            db.execute(
                text(
                    """
                    INSERT INTO provenance(id, clinic_id, patient_id, source_type,
                                           external_ref, payload)
                    VALUES (:id, :clinic_id, :patient_id, 'system', :external_ref, '{}')
                    """
                ),
                {
                    "id": str(prov_id),
                    "clinic_id": str(actor.clinic_id),
                    "patient_id": str(patient_id),
                    "external_ref": f"generated-highlight:{payload.entry_id}",
                },
            )

        db.execute(
            text(
                """
                INSERT INTO highlight(id, clinic_id, patient_id, entry_id,
                                      offset_start, offset_end, quoted_text,
                                      risk_reason, risk_level, source, status,
                                      confidence, provenance_id, created_by)
                VALUES (:id, :clinic_id, :patient_id, :entry_id, :offset_start,
                        :offset_end, :quoted_text, :risk_reason, :risk_level,
                        'ai', 'suggested', :confidence, :provenance_id, NULL)
                """
            ),
            {
                "id": str(highlight_id),
                "clinic_id": str(actor.clinic_id),
                "patient_id": str(patient_id),
                "entry_id": str(payload.entry_id),
                "offset_start": offset_start,
                "offset_end": offset_end,
                "quoted_text": quoted_text,
                "risk_reason": reason,
                "risk_level": level,
                "confidence": conf,
                "provenance_id": str(prov_id),
            },
        )

        # Audit while still in the pipeline role (log_audit is pipeline-only).
        db.execute(
            text(
                "SELECT log_audit(NULL, 'system', 'highlight_suggest', 'highlight', "
                ":hid, :pid, :clinic, NULL, CAST(:metadata AS jsonb))"
            ),
            {
                "hid": str(highlight_id),
                "pid": str(patient_id),
                "clinic": str(actor.clinic_id),
                "metadata": json.dumps(
                    {"entry_id": str(payload.entry_id), "reason": reason}
                ),
            },
        )

        # Restore the caller's role class + role GUC before commit.
        db.execute(text(f"SET LOCAL ROLE {ROLE_CLASS_BY_NAME[actor.role]}"))
        db.execute(
            text("SELECT set_config('app.role', :role, true)"), {"role": actor.role}
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:  # RLS violation, trigger, enum mismatch, etc.
        db.rollback()
        raise HTTPException(status_code=422, detail="highlight generation failed") from exc

    # commit() ended the transaction and reset SET LOCAL ROLE — re-apply the
    # caller's context so the re-SELECT runs under RLS as the caller.
    set_app_context(db, role=actor.role, user_id=actor.user_id, clinic_id=actor.clinic_id)
    row = db.execute(
        text(f"SELECT {_HL_COLS} FROM highlight WHERE id = :hid"),
        {"hid": str(highlight_id)},
    ).mappings().first()
    return HighlightSummary(**dict(row))


def _resolve_highlight(
    db: Session,
    actor: Actor,
    patient_id: uuid.UUID,
    highlight_id: uuid.UUID,
    new_status: str,
) -> HighlightSummary:
    """Shared accept/reject transition: suggested -> accepted|rejected."""
    row = db.execute(
        text(
            """
            SELECT status, patient_id, entry_id
            FROM highlight
            WHERE id = :hid AND clinic_id = app_clinic_id() AND patient_id = :pid
            """
        ),
        {"hid": str(highlight_id), "pid": str(patient_id)},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="highlight not found")
    if row["status"] != "suggested":
        raise HTTPException(status_code=409, detail="highlight is no longer suggested")

    interaction_type = "highlight_accept" if new_status == "accepted" else "highlight_reject"

    result = db.execute(
        text(
            """
            UPDATE highlight
            SET status = :status, resolved_by = :uid, resolved_at = now()
            WHERE id = :hid AND status = 'suggested' AND patient_id = :pid
            """
        ),
        {
            "status": new_status,
            "uid": str(actor.user_id),
            "hid": str(highlight_id),
            "pid": str(patient_id),
        },
    )
    if result.rowcount == 0:
        # TOCTOU guard: a concurrent request already resolved this highlight
        # between the SELECT above and this UPDATE. Do not audit/record/200
        # a transition that did not happen.
        raise HTTPException(status_code=409, detail="highlight is no longer suggested")

    audit(db, actor, interaction_type, "highlight", highlight_id, row["patient_id"])

    # Self-learning hook (M6): record the interaction against the entry's entity
    # features so accept/reject nudges learning_weight now.
    db.execute(
        text(
            "SELECT record_interaction(:uid, :eid, :itype, "
            "COALESCE((SELECT jsonb_agg('entity:' || entity_type || ':' || entity_value) "
            "FROM entry_entity WHERE entry_id = :eid), '[]'::jsonb))"
        ),
        {
            "uid": str(actor.user_id),
            "eid": str(row["entry_id"]),
            "itype": interaction_type,
        },
    )

    db.commit()

    set_app_context(db, role=actor.role, user_id=actor.user_id, clinic_id=actor.clinic_id)
    updated = db.execute(
        text(f"SELECT {_HL_COLS} FROM highlight WHERE id = :hid"),
        {"hid": str(highlight_id)},
    ).mappings().first()
    return HighlightSummary(**dict(updated))


@router.post("/patients/{patient_id}/highlights/{highlight_id}/accept")
def accept_highlight(
    patient_id: uuid.UUID,
    highlight_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> HighlightSummary:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="highlights are clinical-only")
    return _resolve_highlight(db, actor, patient_id, highlight_id, "accepted")


@router.post("/patients/{patient_id}/highlights/{highlight_id}/reject")
def reject_highlight(
    patient_id: uuid.UUID,
    highlight_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> HighlightSummary:
    if actor.role == "patient":
        raise HTTPException(status_code=403, detail="highlights are clinical-only")
    return _resolve_highlight(db, actor, patient_id, highlight_id, "rejected")
