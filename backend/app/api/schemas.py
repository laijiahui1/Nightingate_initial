"""Pydantic request/response models for the M2/M3 Care Note API surface.

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


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    parent_id: uuid.UUID | None = None
    visibility: Literal["internal", "patient_visible"] | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    entry_id: uuid.UUID | None = None
    assignee_id: uuid.UUID
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    due_at: dt.datetime | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: Literal["open", "in_progress", "done", "cancelled"] | None = None
    assignee_id: uuid.UUID | None = None
    priority: Literal["low", "medium", "high", "critical"] | None = None
    due_at: dt.datetime | None = None


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
    patient_id: uuid.UUID | None = None
    author_id: uuid.UUID | None = None
    resolved_by: uuid.UUID | None = None
    resolved_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None


class TaskSummary(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    entry_id: uuid.UUID | None
    assignee_id: uuid.UUID
    assigner_id: uuid.UUID | None
    title: str
    description: str | None
    status: str
    priority: str
    due_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime
    completed_at: dt.datetime | None


class UserSummary(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    role: str


class MentionContext(BaseModel):
    comment_body: str | None
    comment_author_role: str | None
    entry_title: str | None
    entry_id: uuid.UUID | None
    patient_name: str | None
    patient_id: uuid.UUID | None


class MentionSummary(BaseModel):
    id: uuid.UUID
    comment_id: uuid.UUID | None
    entry_id: uuid.UUID | None
    mentioned_user_id: uuid.UUID
    created_by: uuid.UUID
    read_at: dt.datetime | None
    created_at: dt.datetime
    context: MentionContext | None = None


class VersionRow(BaseModel):
    id: int
    version: int
    body: str
    delta_from_prev: dict | None
    author_role: str | None
    author_id: uuid.UUID | None
    change_summary: str | None
    conflict_flag: bool
    conflict_of: int | None
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
    tasks: list[TaskSummary]
