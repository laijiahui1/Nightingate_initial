"""Pydantic request/response models for the M2 Care Note API surface.

Request models validate inbound JSON; response models document the wire shape
(fields are ``snake_case`` and serialized directly to JSON). The frontend is the
only client, so the vertical slice returns plain JSON — no envelope wrapper.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

ScribeType = Literal[
    "ai_doctor_consult_summary",
    "ai_nurse_consult_summary",
    "ai_patient_session_summary",
]

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AiScribeRequest(BaseModel):
    scribe_type: ScribeType
    transcript: str = Field(min_length=1)
    source_type: str | None = None
    external_ref: str | None = None
    visibility: Literal["internal", "patient_visible"] | None = None


class EntryEditRequest(BaseModel):
    body: str
    base_version: int


class RevertRequest(BaseModel):
    target_version: int


class LoginRequest(BaseModel):
    email: EmailStr


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PatientSummary(BaseModel):
    id: uuid.UUID
    mrn: str | None
    display_name: str
    date_of_birth: dt.date | None
    gender: str | None


class EntryAi(BaseModel):
    ai_note_type: str
    model_name: str
    redaction_confirmed: bool


class EntrySummary(BaseModel):
    id: uuid.UUID
    entry_type: str
    title: str
    body: str
    author_role: str
    section: str | None
    visibility: str
    risk_level: str | None
    version: int
    status: str
    created_at: dt.datetime
    updated_at: dt.datetime
    ai: EntryAi | None = None


class CommentSummary(BaseModel):
    id: uuid.UUID
    entry_id: uuid.UUID
    parent_id: uuid.UUID | None
    author_role: str
    body: str
    status: str
    created_at: dt.datetime


class GlanceCard(BaseModel):
    patient_id: uuid.UUID
    top_items: list[dict]
    open_actions: list[dict]
    risk_flags: list[dict]
    computed_at: dt.datetime | None
    invalidated: bool


class AiScribeResult(BaseModel):
    entry_id: uuid.UUID
    entry_type: str
    ai_note_type: str
    model_name: str
    redaction_confirmed: bool
    redaction_mask: list[dict]
    visibility: str


class LoginResult(BaseModel):
    access_token: str
    token_type: Literal["bearer"]
    role: str
    clinic_id: uuid.UUID
    full_name: str
    email: str


class AuditRow(BaseModel):
    id: int
    target_type: str
    target_id: uuid.UUID
    version: int | None
    actor_id: uuid.UUID | None
    actor_role: str | None
    action: str
    occurred_at: dt.datetime


class PageBundle(BaseModel):
    entries: list[EntrySummary]
    comments: list[CommentSummary]
