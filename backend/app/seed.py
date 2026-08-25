"""Deterministic synthetic seed generator for the Nightingale demo database.

Reproduces the three demo scenarios (docs/SYNTHESIS.md §2, M1) with a
longitudinal timeline spanning FIXED dates including **2025-04-15** and
**2026-02-06**, mixing manual notes (staff_note, clinician plan/diagnosis),
all three AI-scribed note types (author_role='system' + provenance_pointer),
threaded comments (resolved / open / @mention), tasks (needs lab order /
waiting nurse follow-up), highlights (risk_reason + provenance), entry versions
(v1/v2), learning interactions/weights, and a precomputed patient_glance.

Guarantees:

- **Deterministic** — every id is a UUID5 derived from a stable key and every
  timestamp is fixed. Running twice produces byte-identical rows. No
  randomness anywhere.
- **Idempotent** — the first step deletes the previously-seeded rows (scoped
  to the two seed clinics) in foreign-key order, then re-inserts everything in
  a single transaction. Re-running is a clean reset-and-fill.
  ``audit_log`` is deliberately NOT touched (neither reset nor insert): its
  append-only trigger blocks DELETE even for superusers, and a synthetic seed
  must never fabricate tamper-evidence audit entries (docs/SECURITY.md §g).
- **PHI self-checked** — every AI-scribed body is passed through
  ``redaction.redact()`` and asserted PHI-free at the end (belt-and-suspenders
  for the redaction tests, docs/SECURITY.md §b).

Connection: the seed is a bootstrap operation, so it connects as a superuser
(the bootstrap ``POSTGRES_USER``) that bypasses RLS in order to write the
tables only SECURITY DEFINER functions may write at runtime (ai_scribed_note,
learning_*, patient_glance). The runtime API still connects as the restricted
``app_nightingale`` role; RLS filters reads at query time. URL precedence:
``SEED_DATABASE_URL`` > ``MIGRATION_DATABASE_URL`` > ``DATABASE_URL``.

Usage (from the ``backend/`` directory, stack up):

    python -m app.seed

or inside the container:

    docker compose exec api python -m app.seed

Exit code 0 on success, non-zero on any failure (the transaction is rolled back).
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import uuid

import psycopg
from psycopg.types.json import Jsonb

# The PHI redaction pipeline (stdlib-only, safe to import anywhere).
from app.services.redaction import contains_phi, redact, redaction_mask, restore

# --- Stable id namespace: every id is uuid5(ns, "nightingale.seed:<key>") -----
_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")


def _uid(key: str) -> uuid.UUID:
    return uuid.uuid5(_NS, f"nightingale.seed:{key}")


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0
) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc)


# Fixed dates the brief requires to be present in the timeline.
DATE_SCENARIO_A = "2025-04-15"  # Scenario A: glance + AI-scribe traceability
DATE_SCENARIO_C = "2026-02-06"  # Scenario C: longitudinal history + highlight logic


# ---------------------------------------------------------------------------
# Small DB helpers
# ---------------------------------------------------------------------------

def _database_url() -> str:
    """Return the bootstrap connection URL (superuser), normalized for psycopg v3."""
    url = (
        os.environ.get("SEED_DATABASE_URL")
        or os.environ.get("MIGRATION_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        raise SystemExit(
            "no database URL: set SEED_DATABASE_URL / MIGRATION_DATABASE_URL / "
            "DATABASE_URL, e.g. postgresql+psycopg://nightingale:...@db:5432/nightingale"
        )
    # Normalise the SQLAlchemy dialect marker psycopg v3 does not understand
    # (mirrors app.db.apply_migrations.database_url).
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        return url
    raise SystemExit(f"unrecognised database URL scheme: {url!r}")


def _insert_row(conn: psycopg.Connection, table: str, row: dict[str, object]) -> None:
    """Insert one row. ``table`` is always a module constant, never user input."""
    columns = ", ".join(row.keys())
    placeholders = ", ".join(["%s"] * len(row))
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


def _quote_offsets(body: str, quote: str) -> tuple[int, int]:
    """Resolve a quoted span inside a body; fail loudly if it cannot be found.

    Keeps highlight/entity offsets exact and never dangling (the provenance
    contract in docs/DATA_SCHEMA.md §3.6).
    """
    start = body.find(quote)
    if start < 0:
        raise ValueError(f"quote not found in body: {quote!r}")
    return start, start + len(quote)


def _char_diff(old: str, new: str) -> dict[str, object]:
    """Minimal char-level diff mirroring the SQL ``char_diff`` helper."""
    if old == new:
        return {"equal": True}
    lo, ln = len(old), len(new)
    i = 0
    while i < lo and i < ln and old[i] == new[i]:
        i += 1
    j = 0
    while j < (lo - i) and j < (ln - i) and old[lo - 1 - j] == new[ln - 1 - j]:
        j += 1
    return {
        "unchanged_prefix": i,
        "unchanged_suffix": j,
        "removed": old[i : lo - j],
        "added": new[i : ln - j],
        "old_length": lo,
        "new_length": ln,
    }


# ---------------------------------------------------------------------------
# Seed state: holds every generated id so downstream rows can reference it
# ---------------------------------------------------------------------------

class SeedState:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self.clinics: dict[str, uuid.UUID] = {}
        self.users: dict[str, uuid.UUID] = {}
        self.patients: dict[str, uuid.UUID] = {}
        self.provenance: dict[str, uuid.UUID] = {}
        self.entries: dict[str, uuid.UUID] = {}
        self.counts: dict[str, int] = {}
        self.ai_bodies: list[tuple[str, str]] = []  # (entry_key, body) for the PHI check

    def bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1


# ---------------------------------------------------------------------------
# Step 1: reset previously-seeded rows (idempotency)
# ---------------------------------------------------------------------------

# Child-first delete order respecting foreign keys: children (mention, comment,
# highlight, task, entry_entity, learning_interaction, entry_version,
# ai_scribed_note) are deleted before their parents (entry, provenance, patient,
# users), so no DELETE ever violates a FK. Every table is scoped by clinic_id to
# the two seed clinics, so only demo rows are removed.
#
# Deliberately excluded: audit_log (append-only — trg_audit_append_only raises
# on DELETE even for superusers) and the cold archives (entry_archive,
# entry_version_archive), which this seed never writes.
_RESET_TABLES: tuple[str, ...] = (
    "mention",
    "comment",
    "highlight",
    "task",
    "entry_entity",
    "learning_interaction",
    "entry_version",
    "ai_scribed_note",
    "entry",
    "provenance",
    "patient_glance",
    "learning_weight",
    "patient",
    "users",
)


def _reset(conn: psycopg.Connection, clinic_ids: list[uuid.UUID]) -> None:
    for table in _RESET_TABLES:
        conn.execute(
            f"DELETE FROM {table} WHERE clinic_id IN (%s, %s)",
            (clinic_ids[0], clinic_ids[1]),
        )
    conn.execute(
        "DELETE FROM clinic WHERE id IN (%s, %s)",
        (clinic_ids[0], clinic_ids[1]),
    )


# ---------------------------------------------------------------------------
# Step 2: clinics, users (all four roles), patients
# ---------------------------------------------------------------------------

def _seed_clinics(state: SeedState) -> None:
    rows = {
        "meridian": {
            "id": _uid("clinic:meridian"),
            "name": "Meridian Family Clinic",
            "created_at": _utc(2025, 4, 1),
        },
        "harbourview": {
            "id": _uid("clinic:harbourview"),
            "name": "Harbourview Medical Centre",
            "created_at": _utc(2025, 4, 1),
        },
    }
    for key, row in rows.items():
        _insert_row(state.conn, "clinic", row)
        state.clinics[key] = row["id"]
        state.bump("clinics")


def _seed_users(state: SeedState) -> None:
    rows = {
        # --- Clinic A: Meridian Family Clinic ---
        "u_alice": {
            "id": _uid("user:alice-tan"),
            "clinic_id": state.clinics["meridian"],
            "email": "alice.tan@meridian.demo",
            "full_name": "Alice Tan",
            "role": "patient",
            "is_active": True,
            "created_at": _utc(2025, 4, 1),
        },
        "u_bala": {
            "id": _uid("user:bala-kumar"),
            "clinic_id": state.clinics["meridian"],
            "email": "bala.kumar@meridian.demo",
            "full_name": "Bala Kumar",
            "role": "patient",
            "is_active": True,
            "created_at": _utc(2025, 4, 1),
        },
        "u_priya": {
            "id": _uid("user:priya-nair"),
            "clinic_id": state.clinics["meridian"],
            "email": "priya.nair@meridian.demo",
            "full_name": "Priya Nair",
            "role": "staff",
            "is_active": True,
            "created_at": _utc(2025, 4, 1),
        },
        "u_marcus": {
            "id": _uid("user:marcus-lee"),
            "clinic_id": state.clinics["meridian"],
            "email": "marcus.lee@meridian.demo",
            "full_name": "Marcus Lee",
            "role": "clinician",
            "is_active": True,
            "created_at": _utc(2025, 4, 1),
        },
        "u_sandra": {
            "id": _uid("user:sandra-ho"),
            "clinic_id": state.clinics["meridian"],
            "email": "sandra.ho@meridian.demo",
            "full_name": "Sandra Ho",
            "role": "admin",
            "is_active": True,
            "created_at": _utc(2025, 4, 1),
        },
        # --- Clinic B: Harbourview Medical Centre ---
        "u_mei": {
            "id": _uid("user:mei-ling-chua"),
            "clinic_id": state.clinics["harbourview"],
            "email": "mei.ling.chua@harbourview.demo",
            "full_name": "Mei Ling Chua",
            "role": "patient",
            "is_active": True,
            "created_at": _utc(2025, 4, 1),
        },
        "u_david": {
            "id": _uid("user:david-ong"),
            "clinic_id": state.clinics["harbourview"],
            "email": "david.ong@harbourview.demo",
            "full_name": "David Ong",
            "role": "staff",
            "is_active": True,
            "created_at": _utc(2025, 4, 1),
        },
        "u_amelia": {
            "id": _uid("user:amelia-wong"),
            "clinic_id": state.clinics["harbourview"],
            "email": "amelia.wong@harbourview.demo",
            "full_name": "Amelia Wong",
            "role": "clinician",
            "is_active": True,
            "created_at": _utc(2025, 4, 1),
        },
        "u_jason": {
            "id": _uid("user:jason-lim"),
            "clinic_id": state.clinics["harbourview"],
            "email": "jason.lim@harbourview.demo",
            "full_name": "Jason Lim",
            "role": "admin",
            "is_active": True,
            "created_at": _utc(2025, 4, 1),
        },
    }
    for key, row in rows.items():
        _insert_row(state.conn, "users", row)
        state.users[key] = row["id"]
        state.bump("users")


def _seed_patients(state: SeedState) -> None:
    rows = {
        "p_alice": {
            "id": _uid("patient:alice-tan"),
            "clinic_id": state.clinics["meridian"],
            "user_id": state.users["u_alice"],
            "mrn": "MRN-1001",
            "display_name": "Alice Tan",
            "date_of_birth": dt.date(1980, 4, 12),
            "gender": "female",
            "consent_to_store": True,
            "created_at": _utc(2025, 4, 1),
        },
        "p_bala": {
            "id": _uid("patient:bala-kumar"),
            "clinic_id": state.clinics["meridian"],
            "user_id": state.users["u_bala"],
            "mrn": "MRN-1002",
            "display_name": "Bala Kumar",
            "date_of_birth": dt.date(1995, 11, 2),
            "gender": "male",
            "consent_to_store": True,
            "created_at": _utc(2025, 4, 1),
        },
        "p_mei": {
            "id": _uid("patient:mei-ling-chua"),
            "clinic_id": state.clinics["harbourview"],
            "user_id": state.users["u_mei"],
            "mrn": "MRN-2001",
            "display_name": "Mei Ling Chua",
            "date_of_birth": dt.date(1992, 8, 19),
            "gender": "female",
            "consent_to_store": True,
            "created_at": _utc(2025, 4, 1),
        },
    }
    for key, row in rows.items():
        _insert_row(state.conn, "patient", row)
        state.patients[key] = row["id"]
        state.bump("patients")


# ---------------------------------------------------------------------------
# Step 3: provenance registry (the provenance_pointer target)
# ---------------------------------------------------------------------------

def _seed_provenance(state: SeedState) -> None:
    rows = {
        "prov_doctor_2025": {
            "id": _uid("prov:doctor-2025-04-15"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "source_type": "ai_doctor_consult",
            "external_ref": "enc-2025-04-15-001",
            "span_start": 0,
            "span_end": 10,
            "payload": Jsonb(
                {
                    "speaker_labels": ["Doctor", "Patient"],
                    "segments": [{"t0": "00:00:12", "t1": "00:12:40"}],
                }
            ),
            "created_at": _utc(2025, 4, 15, 10, 0),
        },
        "prov_nurse_2025": {
            "id": _uid("prov:nurse-2025-05-02"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "source_type": "ai_nurse_consult",
            "external_ref": "nurse-enc-2025-05-02-002",
            "span_start": 0,
            "span_end": 10,
            "payload": Jsonb({"speaker_labels": ["Nurse", "Patient"]}),
            "created_at": _utc(2025, 5, 2, 14, 30),
        },
        "prov_session_2025": {
            "id": _uid("prov:session-2025-06-10"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "source_type": "ai_patient_session",
            "external_ref": "session-2025-06-10-003",
            "span_start": 0,
            "span_end": 10,
            "payload": Jsonb({"channel": "mobile-app"}),
            "created_at": _utc(2025, 6, 10, 9, 0),
        },
        "prov_session_2026": {
            "id": _uid("prov:session-2026-01-05"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "source_type": "ai_patient_session",
            "external_ref": "session-2026-01-05-004",
            "span_start": 0,
            "span_end": 10,
            "payload": Jsonb({"channel": "mobile-app"}),
            "created_at": _utc(2026, 1, 5, 18, 0),
        },
        "prov_plan_2026": {
            "id": _uid("prov:plan-2026-02-06"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "source_type": "manual_note",
            "external_ref": "entry:2026-02-06-plan",
            "span_start": 0,
            "span_end": 10,
            "payload": Jsonb({}),
            "created_at": _utc(2026, 2, 6, 10, 0),
        },
        "prov_plan_2025": {
            "id": _uid("prov:plan-2025-11-30"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "source_type": "manual_note",
            "external_ref": "entry:2025-11-30-plan",
            "span_start": 0,
            "span_end": 10,
            "payload": Jsonb({}),
            "created_at": _utc(2025, 11, 30, 15, 0),
        },
        "prov_bala_doctor": {
            "id": _uid("prov:bala-doctor-2025-07-14"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_bala"],
            "source_type": "ai_doctor_consult",
            "external_ref": "enc-2025-07-14-005",
            "span_start": 0,
            "span_end": 10,
            "payload": Jsonb({"speaker_labels": ["Doctor", "Patient"]}),
            "created_at": _utc(2025, 7, 14, 9, 30),
        },
        "prov_bala_session": {
            "id": _uid("prov:bala-session-2025-09-01"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_bala"],
            "source_type": "ai_patient_session",
            "external_ref": "session-2025-09-01-101",
            "span_start": 0,
            "span_end": 10,
            "payload": Jsonb({"channel": "mobile-app"}),
            "created_at": _utc(2025, 9, 1, 19, 0),
        },
        "prov_mei_doctor": {
            "id": _uid("prov:mei-doctor-2026-02-06"),
            "clinic_id": state.clinics["harbourview"],
            "patient_id": state.patients["p_mei"],
            "source_type": "ai_doctor_consult",
            "external_ref": "enc-2026-02-06-006",
            "span_start": 0,
            "span_end": 10,
            "payload": Jsonb({"speaker_labels": ["Doctor", "Patient"]}),
            "created_at": _utc(2026, 2, 6, 15, 0),
        },
    }
    for key, row in rows.items():
        _insert_row(state.conn, "provenance", row)
        state.provenance[key] = row["id"]
        state.bump("provenance")


# ---------------------------------------------------------------------------
# Step 4: entries — the longitudinal timeline (fixed dates, manual + AI)
# ---------------------------------------------------------------------------

def _insert_entry(
    state: SeedState,
    key: str,
    *,
    clinic_key: str,
    patient_key: str,
    author_user: str | None,
    author_role: str,
    entry_type: str,
    title: str,
    body: str,
    section: str | None = None,
    visibility: str = "internal",
    provenance_key: str | None = None,
    risk_level: str | None = None,
    version: int = 1,
    status: str = "final",
    created_at: dt.datetime,
    updated_at: dt.datetime | None = None,
) -> uuid.UUID:
    row: dict[str, object] = {
        "id": _uid(f"entry:{key}"),
        "clinic_id": state.clinics[clinic_key],
        "patient_id": state.patients[patient_key],
        "author_role": author_role,
        "entry_type": entry_type,
        "title": title,
        "body": body,
        "visibility": visibility,
        "version": version,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
    }
    if author_user is not None:
        row["author_id"] = state.users[author_user]
    if section is not None:
        row["section"] = section
    if provenance_key is not None:
        row["provenance_id"] = state.provenance[provenance_key]
    if risk_level is not None:
        row["risk_level"] = risk_level
    _insert_row(state.conn, "entry", row)
    state.entries[key] = row["id"]  # type: ignore[assignment]
    state.bump("entries")
    if entry_type.startswith("ai_"):
        state.ai_bodies.append((key, body))
    return state.entries[key]


def _seed_entries(state: SeedState) -> None:
    # --- Patient 1: Alice Tan (Meridian) — the primary demo timeline ---------
    # 2025-04-15 (Scenario A: glance + AI-scribe traceability)
    _insert_entry(
        state,
        "e1_intake",
        clinic_key="meridian",
        patient_key="p_alice",
        author_user="u_priya",
        author_role="staff",
        entry_type="staff_note",
        title="Intake — Alice Tan",
        body=(
            "Intake for Alice Tan (NRIC S9123456D, +65 9123 4567). Chief complaint: "
            "intermittent chest tightness and headache for 2 weeks.\n\n"
            "Triage vitals: BP 148/92, HR 88, SpO2 98%.\n"
            "Allergies: Penicillin (rash) — noted on intake.\n"
            "Pending: full blood count, lipid panel.\n"
            "Next: clinician consult scheduled for today."
        ),
        section="staff_handoff",
        risk_level="medium",
        created_at=_utc(2025, 4, 15, 9, 15),
    )

    _insert_entry(
        state,
        "e2_ai_doctor",
        clinic_key="meridian",
        patient_key="p_alice",
        author_user=None,
        author_role="system",
        entry_type="ai_doctor_consult_summary",
        title="AI Doctor Consult Summary — 2025-04-15",
        body=(
            "SUBJECTIVE\n"
            "Alice Tan reported chest tightness and morning headaches. "
            "Symptoms worsen with stress.\n\n"
            "OBJECTIVE\n"
            "BP 148/92 (right arm, seated), HR 88, BMI 27.1. No peripheral oedema.\n\n"
            "ASSESSMENT\n"
            "Probable essential hypertension Stage 1. Rule out cardiac cause. "
            "Allergies: Penicillin.\n\n"
            "PLAN\n"
            "1. Start amlodipine 5 mg OD.\n"
            "2. Order ambulatory BP monitoring.\n"
            "3. ECG at next visit.\n"
            "4. Review in 4 weeks. Cardiologist referral if BP remains elevated.\n\n"
            "Follow-ups\n"
            "- Recheck BP in 4 weeks.\n"
            "- Monitor for dizziness."
        ),
        provenance_key="prov_doctor_2025",
        risk_level="medium",
        created_at=_utc(2025, 4, 15, 10, 0),
    )

    # Patient-visible care instructions (the patient-role demo needs content).
    # Deliberately patient-safe: no NRIC/phone, plain English care plan.
    _insert_entry(
        state,
        "e_patient_discharge",
        clinic_key="meridian",
        patient_key="p_alice",
        author_user="u_marcus",
        author_role="clinician",
        entry_type="instruction",
        title="Care Instructions — Alice Tan",
        body=(
            "Care instructions for Alice Tan.\n\n"
            "Medications\n"
            "- Take amlodipine 5 mg once daily in the morning.\n\n"
            "Follow-up\n"
            "- Attend your blood pressure review in 4 weeks.\n"
            "- Complete 7 days of ambulatory blood pressure monitoring.\n\n"
            "When to seek help\n"
            "- Contact the clinic or seek urgent care if you feel chest pain, "
            "severe dizziness, or faint."
        ),
        section="plan",
        visibility="patient_visible",
        risk_level="low",
        created_at=_utc(2025, 4, 15, 12, 0),
    )

    # Versioned manual entry: current version (v2) with v1/v2 snapshots below.
    _insert_entry(
        state,
        "e3_care_plan",
        clinic_key="meridian",
        patient_key="p_alice",
        author_user="u_marcus",
        author_role="clinician",
        entry_type="clinician_note",
        title="Care Plan — 2025-04-15",
        body=(
            "1. Amlodipine 5 mg OD.\n"
            "2. Ambulatory BP monitoring for 7 days.\n"
            "3. ECG at next visit.\n"
            "4. Review in 4 weeks.\n"
            "5. Added 24h Holter monitoring per cardiology advice (2025-05-02)."
        ),
        section="plan",
        risk_level="medium",
        version=2,
        created_at=_utc(2025, 4, 15, 11, 30),
        updated_at=_utc(2025, 5, 2, 14, 0),
    )

    _insert_entry(
        state,
        "e4_ai_nurse",
        clinic_key="meridian",
        patient_key="p_alice",
        author_user=None,
        author_role="system",
        entry_type="ai_nurse_consult_summary",
        title="AI Nurse Consult Summary — 2025-05-02",
        body=(
            "Nurse follow-up call with Alice Tan (contact +65 9123 4567).\n\n"
            "- Reports taking amlodipine 5 mg daily without missing doses.\n"
            "- Morning BP readings this week: 140/88, 138/85, 142/90.\n"
            "- No new symptoms. Appetite normal.\n\n"
            "Action\n"
            "- Continue current plan.\n"
            "- Remind patient about 24h Holter monitor appointment.\n"
            "- Cardiology referral discussion pending."
        ),
        provenance_key="prov_nurse_2025",
        risk_level="low",
        created_at=_utc(2025, 5, 2, 14, 30),
    )

    _insert_entry(
        state,
        "e5_ai_session",
        clinic_key="meridian",
        patient_key="p_alice",
        author_user=None,
        author_role="system",
        entry_type="ai_patient_session_summary",
        title="AI Patient Session Summary — 2025-06-10",
        body=(
            "AI-patient session (session-2025-06-10-003) with Alice Tan.\n\n"
            "Questions the patient asked\n"
            '- "Will the new blood pressure medicine make me dizzy?"\n'
            '- "Can I take it in the evening instead of the morning?"\n\n'
            "Patient concerns\n"
            "- Worried about dizziness with BP medication.\n"
            "- Prefers evening consultations due to work schedule.\n\n"
            "Structured facts\n"
            "- Medication: amlodipine 5 mg OD, currently tolerating.\n"
            "- Follow-up preference: evening slots."
        ),
        provenance_key="prov_session_2025",
        risk_level="low",
        created_at=_utc(2025, 6, 10, 9, 0),
    )

    _insert_entry(
        state,
        "e6_lab_handoff",
        clinic_key="meridian",
        patient_key="p_alice",
        author_user="u_priya",
        author_role="staff",
        entry_type="staff_note",
        title="Handoff — lab results pending",
        body=(
            "Alice Tan's lipid panel and renal panel results are still pending "
            "from the lab.\n\n"
            "Patient called to confirm contact: +65 9123 4567.\n\n"
            "Action\n"
            "- Needs lab order for follow-up blood tests.\n"
            "- Staff to chase the lab before 2025-08-28."
        ),
        section="staff_handoff",
        risk_level="low",
        created_at=_utc(2025, 8, 21, 10, 0),
    )

    _insert_entry(
        state,
        "e7_med_review",
        clinic_key="meridian",
        patient_key="p_alice",
        author_user="u_marcus",
        author_role="clinician",
        entry_type="clinician_note",
        title="Medication Review — 2025-11-30",
        body=(
            "BP 135/85 on amlodipine 5 mg. Persistent diastolic elevation.\n\n"
            "Plan\n"
            "1. Consider adding losartan 50 mg OD.\n"
            "2. Recheck renal panel and electrolytes in 2 weeks.\n"
            "3. Reviewed allergies: Penicillin (rash) — avoid penicillin group.\n\n"
            "Note\n"
            "- Patient tolerated amlodipine well; no dizziness reported at this visit."
        ),
        section="plan",
        provenance_key="prov_plan_2025",
        risk_level="medium",
        created_at=_utc(2025, 11, 30, 15, 0),
    )

    _insert_entry(
        state,
        "e8_session_dizziness",
        clinic_key="meridian",
        patient_key="p_alice",
        author_user=None,
        author_role="system",
        entry_type="ai_patient_session_summary",
        title="AI Patient Session Summary — 2026-01-05",
        body=(
            "AI-patient session (session-2026-01-05-004) with Alice Tan.\n\n"
            "Questions the patient asked\n"
            '- "I have been feeling dizzy after starting losartan. Is this normal?"\n\n'
            "Patient concerns\n"
            "- Episodes of dizziness since 2026-01-02, mostly in the morning.\n"
            "- Asks whether to stop the medication.\n\n"
            "Structured facts\n"
            "- Medication: losartan 50 mg started 2025-12-07.\n"
            "- New symptom: dizziness episodes (since 2026-01-02)."
        ),
        provenance_key="prov_session_2026",
        risk_level="medium",
        created_at=_utc(2026, 1, 5, 18, 0),
    )

    # 2026-02-06 (Scenario C: longitudinal history + highlight logic)
    _insert_entry(
        state,
        "e9_review",
        clinic_key="meridian",
        patient_key="p_alice",
        author_user="u_marcus",
        author_role="clinician",
        entry_type="clinician_note",
        title="Review — 2026-02-06",
        body=(
            "Review of Alice Tan on 2026-02-06.\n\n"
            "BP 152/96 this visit. Dizziness reported since starting losartan 50 mg.\n\n"
            "Assessment\n"
            "- Elevated BP despite combination therapy.\n\n"
            "Plan\n"
            "1. Escalate to amlodipine 10 mg + losartan 50 mg.\n"
            "2. Recheck renal panel and electrolytes.\n"
            "3. Refer to cardiology if no improvement in 2 weeks.\n\n"
            "Flag\n"
            "- Elevated BP and dizziness despite medication — requires follow-up."
        ),
        section="plan",
        provenance_key="prov_plan_2026",
        risk_level="high",
        created_at=_utc(2026, 2, 6, 10, 0),
    )

    _insert_entry(
        state,
        "e10_handoff_q1",
        clinic_key="meridian",
        patient_key="p_alice",
        author_user="u_priya",
        author_role="staff",
        entry_type="staff_note",
        title="Handoff — Q1 2026",
        body=(
            "Alice Tan continues on combination therapy. Home BP readings "
            "averaging 138/86.\n\n"
            "Next review scheduled for 2026-04-10.\n"
            "Patient prefers evening consultations.\n\n"
            "Action\n"
            "- Confirm cardiology referral status with clinic administrator."
        ),
        section="staff_handoff",
        risk_level="low",
        created_at=_utc(2026, 3, 18, 9, 0),
    )

    # --- Patient 2: Bala Kumar (Meridian) — shorter longitudinal stream ------
    _insert_entry(
        state,
        "b1_ai_doctor",
        clinic_key="meridian",
        patient_key="p_bala",
        author_user=None,
        author_role="system",
        entry_type="ai_doctor_consult_summary",
        title="AI Doctor Consult Summary — 2025-07-14",
        body=(
            "SUBJECTIVE\n"
            "Bala Kumar (NRIC 800101-14-5678, +60 12 345 6789) reported worsening "
            "asthma symptoms with nocturnal cough.\n\n"
            "OBJECTIVE\n"
            "SpO2 96%, wheeze on auscultation. Peak flow 62% of predicted.\n\n"
            "ASSESSMENT\n"
            "Moderate asthma exacerbation. Allergies: dust mites.\n\n"
            "PLAN\n"
            "1. Salbutamol inhaler 2 puffs PRN.\n"
            "2. Start inhaled corticosteroid (budesonide 200 mcg).\n"
            "3. Review in 2 weeks. Refer for spirometry if no improvement."
        ),
        provenance_key="prov_bala_doctor",
        risk_level="medium",
        created_at=_utc(2025, 7, 14, 9, 30),
    )

    _insert_entry(
        state,
        "b2_asthma_plan",
        clinic_key="meridian",
        patient_key="p_bala",
        author_user="u_marcus",
        author_role="clinician",
        entry_type="clinician_note",
        title="Asthma Management Plan — 2025-07-14",
        body=(
            "Asthma action plan for Bala Kumar.\n\n"
            "1. Budesonide 200 mcg BD.\n"
            "2. Salbutamol 2 puffs PRN for breakthrough symptoms.\n"
            "3. Avoid dust mite triggers; consider mattress covers.\n"
            "4. Review in 2 weeks with peak flow diary."
        ),
        section="plan",
        risk_level="low",
        created_at=_utc(2025, 7, 14, 11, 0),
    )

    _insert_entry(
        state,
        "b3_ai_session",
        clinic_key="meridian",
        patient_key="p_bala",
        author_user=None,
        author_role="system",
        entry_type="ai_patient_session_summary",
        title="AI Patient Session Summary — 2025-09-01",
        body=(
            "AI-patient session (session-2025-09-01-101) with Bala Kumar.\n\n"
            "Questions the patient asked\n"
            '- "Do I need to take the brown inhaler even when I feel fine?"\n\n'
            "Patient concerns\n"
            "- Unsure about maintenance inhaler adherence.\n"
            "- Reports fewer symptoms since starting budesonide.\n\n"
            "Structured facts\n"
            "- Medication: budesonide 200 mcg BD, salbutamol PRN.\n"
            "- Peak flow improving."
        ),
        provenance_key="prov_bala_session",
        risk_level="low",
        created_at=_utc(2025, 9, 1, 19, 0),
    )

    # --- Patient 3: Mei Ling Chua (Harbourview) — cross-clinic isolation -----
    _insert_entry(
        state,
        "m1_ai_doctor",
        clinic_key="harbourview",
        patient_key="p_mei",
        author_user=None,
        author_role="system",
        entry_type="ai_doctor_consult_summary",
        title="AI Doctor Consult Summary — 2026-02-06",
        body=(
            "SUBJECTIVE\n"
            "Mei Ling Chua (NRIC S8877665F, +65 8765 4321) reported fever and "
            "joint pain for 4 days.\n\n"
            "OBJECTIVE\n"
            "Temp 38.4 C. Platelets 128 x 10^9/L.\n\n"
            "ASSESSMENT\n"
            "Suspected dengue. Monitor platelet trend.\n\n"
            "PLAN\n"
            "1. Paracetamol PRN for fever.\n"
            "2. Serial full blood count every 48h.\n"
            "3. Return immediately if bleeding, severe abdominal pain, or vomiting."
        ),
        provenance_key="prov_mei_doctor",
        risk_level="high",
        created_at=_utc(2026, 2, 6, 15, 0),
    )

    _insert_entry(
        state,
        "m2_dengue_followup",
        clinic_key="harbourview",
        patient_key="p_mei",
        author_user="u_amelia",
        author_role="clinician",
        entry_type="clinician_note",
        title="Dengue Follow-up — 2026-03-02",
        body=(
            "Recovery confirmed. Platelets normalised (210 x 10^9/L).\n\n"
            "Discharge advice\n"
            "- Resume normal activities.\n"
            "- Continue mosquito precautions at home."
        ),
        section="diagnosis",
        risk_level="low",
        created_at=_utc(2026, 3, 2, 10, 0),
    )


# ---------------------------------------------------------------------------
# Step 5: ai_scribed_note rows (1:1 with system-authored entries)
# ---------------------------------------------------------------------------

def _seed_ai_notes(state: SeedState) -> None:
    rows = {
        "e2_ai_doctor": {
            "entry_id": state.entries["e2_ai_doctor"],
            "clinic_id": state.clinics["meridian"],
            "ai_note_type": "ai_doctor_consult_summary",
            "model_name": "mock-llm",
            "model_version": "1.0",
            "pipeline_version": "seed-1.0",
            "source_session_id": "enc-2025-04-15-001",
            "redaction_confirmed": True,
            "speaker_timestamps": Jsonb(
                [{"speaker": "Doctor", "start": 0.0, "end": 12.0, "confidence": 0.91}]
            ),
            "clinical_summary": "Hypertension Stage 1; started amlodipine.",
            "raw_confidence": 0.95,
            "created_at": _utc(2025, 4, 15, 10, 0),
        },
        "e4_ai_nurse": {
            "entry_id": state.entries["e4_ai_nurse"],
            "clinic_id": state.clinics["meridian"],
            "ai_note_type": "ai_nurse_consult_summary",
            "model_name": "mock-llm",
            "model_version": "1.0",
            "pipeline_version": "seed-1.0",
            "source_session_id": "nurse-enc-2025-05-02-002",
            "redaction_confirmed": True,
            "speaker_timestamps": Jsonb([]),
            "clinical_summary": "Nurse follow-up call; BP improving.",
            "raw_confidence": 0.93,
            "created_at": _utc(2025, 5, 2, 14, 30),
        },
        "e5_ai_session": {
            "entry_id": state.entries["e5_ai_session"],
            "clinic_id": state.clinics["meridian"],
            "ai_note_type": "ai_patient_session_summary",
            "model_name": "mock-llm",
            "model_version": "1.0",
            "pipeline_version": "seed-1.0",
            "source_session_id": "session-2025-06-10-003",
            "redaction_confirmed": True,
            "speaker_timestamps": Jsonb([]),
            "clinical_summary": "Patient worried about dizziness; prefers evening slots.",
            "raw_confidence": 0.94,
            "created_at": _utc(2025, 6, 10, 9, 0),
        },
        "e8_session_dizziness": {
            "entry_id": state.entries["e8_session_dizziness"],
            "clinic_id": state.clinics["meridian"],
            "ai_note_type": "ai_patient_session_summary",
            "model_name": "mock-llm",
            "model_version": "1.0",
            "pipeline_version": "seed-1.0",
            "source_session_id": "session-2026-01-05-004",
            "redaction_confirmed": True,
            "speaker_timestamps": Jsonb([]),
            "clinical_summary": "Dizziness episodes since starting losartan.",
            "raw_confidence": 0.94,
            "created_at": _utc(2026, 1, 5, 18, 0),
        },
        "b1_ai_doctor": {
            "entry_id": state.entries["b1_ai_doctor"],
            "clinic_id": state.clinics["meridian"],
            "ai_note_type": "ai_doctor_consult_summary",
            "model_name": "mock-llm",
            "model_version": "1.0",
            "pipeline_version": "seed-1.0",
            "source_session_id": "enc-2025-07-14-005",
            "redaction_confirmed": True,
            "speaker_timestamps": Jsonb([]),
            "clinical_summary": "Moderate asthma exacerbation; started budesonide.",
            "raw_confidence": 0.92,
            "created_at": _utc(2025, 7, 14, 9, 30),
        },
        "b3_ai_session": {
            "entry_id": state.entries["b3_ai_session"],
            "clinic_id": state.clinics["meridian"],
            "ai_note_type": "ai_patient_session_summary",
            "model_name": "mock-llm",
            "model_version": "1.0",
            "pipeline_version": "seed-1.0",
            "source_session_id": "session-2025-09-01-101",
            "redaction_confirmed": True,
            "speaker_timestamps": Jsonb([]),
            "clinical_summary": "Patient unsure about maintenance inhaler adherence.",
            "raw_confidence": 0.93,
            "created_at": _utc(2025, 9, 1, 19, 0),
        },
        "m1_ai_doctor": {
            "entry_id": state.entries["m1_ai_doctor"],
            "clinic_id": state.clinics["harbourview"],
            "ai_note_type": "ai_doctor_consult_summary",
            "model_name": "mock-llm",
            "model_version": "1.0",
            "pipeline_version": "seed-1.0",
            "source_session_id": "enc-2026-02-06-006",
            "redaction_confirmed": True,
            "speaker_timestamps": Jsonb([]),
            "clinical_summary": "Suspected dengue; platelet monitoring plan.",
            "raw_confidence": 0.95,
            "created_at": _utc(2026, 2, 6, 15, 0),
        },
    }
    # The redaction_mask is computed from the ACTUAL entry body (never hardcoded)
    # so the stored metadata always reflects what the pipeline would redact.
    body_of = dict(state.ai_bodies)
    for key, row in rows.items():
        row["redaction_mask"] = Jsonb(redaction_mask(redact(body_of[key])[1]))
        _insert_row(state.conn, "ai_scribed_note", row)
        state.bump("ai_scribed_notes")


# ---------------------------------------------------------------------------
# Step 6: entry_entity (tagged clinical entities -> risk + learning)
# ---------------------------------------------------------------------------

def _seed_entities(state: SeedState) -> None:
    rows: list[tuple[str, str, str, str, dt.datetime]] = [
        # (entry_key, entity_type, entity_value, created_by, ts)
        ("e2_ai_doctor", "allergy", "penicillin", "ai", _utc(2025, 4, 15, 10, 0)),
        ("e2_ai_doctor", "chief_complaint", "chest tightness", "ai", _utc(2025, 4, 15, 10, 0)),
        ("e2_ai_doctor", "medication", "amlodipine", "ai", _utc(2025, 4, 15, 10, 0)),
        ("e7_med_review", "medication", "losartan", "rule", _utc(2025, 11, 30, 15, 0)),
        ("e8_session_dizziness", "symptom", "dizziness", "ai", _utc(2026, 1, 5, 18, 0)),
        ("e9_review", "symptom", "dizziness", "rule", _utc(2026, 2, 6, 10, 0)),
        ("e9_review", "vitals", "bp 152/96", "rule", _utc(2026, 2, 6, 10, 0)),
        ("b1_ai_doctor", "allergy", "dust mites", "ai", _utc(2025, 7, 14, 9, 30)),
        ("b1_ai_doctor", "symptom", "wheezing", "ai", _utc(2025, 7, 14, 9, 30)),
        ("b1_ai_doctor", "medication", "salbutamol", "ai", _utc(2025, 7, 14, 9, 30)),
        ("m1_ai_doctor", "symptom", "fever", "ai", _utc(2026, 2, 6, 15, 0)),
        ("m1_ai_doctor", "symptom", "joint pain", "ai", _utc(2026, 2, 6, 15, 0)),
    ]
    for entry_key, entity_type, entity_value, created_by, ts in rows:
        _insert_row(
            state.conn,
            "entry_entity",
            {
                "entry_id": state.entries[entry_key],
                "clinic_id": (
                    state.clinics["harbourview"]
                    if entry_key == "m1_ai_doctor"
                    else state.clinics["meridian"]
                ),
                "entity_type": entity_type,
                "entity_value": entity_value,
                "created_by": created_by,
                "created_at": ts,
            },
        )
        state.bump("entry_entities")


# ---------------------------------------------------------------------------
# Step 7: comments (resolved / open / @mention / patient-facing reply) + mentions
# ---------------------------------------------------------------------------

def _seed_comments_and_mentions(state: SeedState) -> None:
    comments = {
        "c1_resolved": {
            "id": _uid("comment:c1-resolved"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e9_review"],
            "author_id": state.users["u_priya"],
            "author_role": "staff",
            "body": "Patient contacted by phone; will recheck BP at home daily for "
            "7 days and log readings.",
            "visibility": "internal",
            "status": "resolved",
            "resolved_by": state.users["u_marcus"],
            "resolved_at": _utc(2026, 2, 7, 9, 0),
            "created_at": _utc(2026, 2, 6, 14, 0),
            "updated_at": _utc(2026, 2, 7, 9, 0),
        },
        "c2_open_mention": {
            "id": _uid("comment:c2-open-mention"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e4_ai_nurse"],
            "author_id": state.users["u_priya"],
            "author_role": "staff",
            "body": "Nurse to coordinate the cardiology referral discussion. "
            "@Marcus Lee please advise on timing.",
            "visibility": "internal",
            "status": "open",
            "created_at": _utc(2025, 5, 3, 10, 0),
            "updated_at": _utc(2025, 5, 3, 10, 0),
        },
        "c3_reply": {
            "id": _uid("comment:c3-reply"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e4_ai_nurse"],
            "parent_id": _uid("comment:c2-open-mention"),
            "author_id": state.users["u_marcus"],
            "author_role": "clinician",
            "body": "Agreed — referring to Dr. Amelia Wong at Harbourview Medical "
            "Centre. @Priya Nair will coordinate scheduling.",
            "visibility": "internal",
            "status": "open",
            "created_at": _utc(2025, 5, 4, 9, 0),
            "updated_at": _utc(2025, 5, 4, 9, 0),
        },
        "c4_patient": {
            "id": _uid("comment:c4-patient"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e5_ai_session"],
            "author_id": state.users["u_alice"],
            "author_role": "patient",
            "body": "I feel fine now, just a little worried about the dizziness.",
            "visibility": "patient_visible",
            "status": "open",
            "created_at": _utc(2025, 6, 11, 8, 0),
            "updated_at": _utc(2025, 6, 11, 8, 0),
        },
    }
    for key, row in comments.items():
        _insert_row(state.conn, "comment", row)
        state.bump("comments")

    mentions = {
        "m_c2": {
            "id": _uid("mention:m-c2"),
            "clinic_id": state.clinics["meridian"],
            "comment_id": _uid("comment:c2-open-mention"),
            "mentioned_user_id": state.users["u_marcus"],
            "created_by": state.users["u_priya"],
            "read_at": None,
            "created_at": _utc(2025, 5, 3, 10, 0),
        },
        "m_c3": {
            "id": _uid("mention:m-c3"),
            "clinic_id": state.clinics["meridian"],
            "comment_id": _uid("comment:c3-reply"),
            "mentioned_user_id": state.users["u_priya"],
            "created_by": state.users["u_marcus"],
            "read_at": None,
            "created_at": _utc(2025, 5, 4, 9, 0),
        },
    }
    for key, row in mentions.items():
        _insert_row(state.conn, "mention", row)
        state.bump("mentions")


# ---------------------------------------------------------------------------
# Step 8: tasks (needs lab order / waiting nurse follow-up / a done one)
# ---------------------------------------------------------------------------

def _seed_tasks(state: SeedState) -> None:
    rows = {
        "t_lab_order": {
            "id": _uid("task:lab-order"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e6_lab_handoff"],
            "assignee_id": state.users["u_priya"],
            "assigner_id": state.users["u_marcus"],
            "title": "Needs lab order",
            "description": "Place the follow-up blood test order (lipid + renal panel).",
            "status": "open",
            "priority": "high",
            "due_at": _utc(2025, 8, 28, 17, 0),
            "created_at": _utc(2025, 8, 21, 10, 0),
            "updated_at": _utc(2025, 8, 21, 10, 0),
        },
        "t_nurse_followup": {
            "id": _uid("task:nurse-followup"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e9_review"],
            "assignee_id": state.users["u_priya"],
            "assigner_id": state.users["u_marcus"],
            "title": "Waiting nurse follow-up",
            "description": "Nurse to recheck BP daily and log readings for 7 days.",
            "status": "in_progress",
            "priority": "high",
            "due_at": _utc(2026, 2, 13, 17, 0),
            "created_at": _utc(2026, 2, 6, 10, 0),
            "updated_at": _utc(2026, 2, 7, 9, 0),
        },
        "t_call_lab": {
            "id": _uid("task:call-lab"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e6_lab_handoff"],
            "assignee_id": state.users["u_priya"],
            "assigner_id": state.users["u_marcus"],
            "title": "Call patient re: lab results",
            "description": "Call Alice Tan with the lab results once available.",
            "status": "done",
            "priority": "low",
            "due_at": _utc(2025, 9, 5, 17, 0),
            "completed_at": _utc(2025, 9, 1, 12, 0),
            "created_at": _utc(2025, 8, 21, 10, 0),
            "updated_at": _utc(2025, 9, 1, 12, 0),
        },
    }
    for key, row in rows.items():
        _insert_row(state.conn, "task", row)
        state.bump("tasks")


# ---------------------------------------------------------------------------
# Step 9: highlights (risk_reason + provenance_pointer + exact span)
# ---------------------------------------------------------------------------

def _seed_highlights(state: SeedState) -> None:
    rows = [
        {
            "key": "h1_high_bp",
            "id": _uid("highlight:h1-high-bp"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e9_review"],
            "quote": "Elevated BP and dizziness despite medication",
            "risk_reason": "Elevated BP (152/96) with dizziness despite medication "
            "- requires follow-up",
            "risk_level": "high",
            "source": "rule",
            "status": "suggested",
            "confidence": 0.90,
            "provenance_key": "prov_plan_2026",
            "created_by": None,
            "resolved_by": None,
            "resolved_at": None,
            "created_at": _utc(2026, 2, 6, 10, 0),
        },
        {
            "key": "h2_penicillin",
            "id": _uid("highlight:h2-penicillin"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e2_ai_doctor"],
            "quote": "Penicillin",
            "risk_reason": "Penicillin allergy (rash) - critical for prescribing",
            "risk_level": "critical",
            "source": "ai",
            "status": "accepted",
            "confidence": 0.98,
            "provenance_key": "prov_doctor_2025",
            "created_by": None,
            "resolved_by": "u_marcus",
            "resolved_at": _utc(2025, 4, 16, 9, 0),
            "created_at": _utc(2025, 4, 15, 10, 0),
        },
        {
            "key": "h3_pharm_review",
            "id": _uid("highlight:h3-pharm-review"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e7_med_review"],
            "quote": "Consider adding losartan",
            "risk_reason": "Medication change requires pharmacist review before dispensing",
            "risk_level": "medium",
            "source": "manual",
            "status": "suggested",
            "confidence": None,
            "provenance_key": "prov_plan_2025",
            "created_by": "u_priya",
            "resolved_by": None,
            "resolved_at": None,
            "created_at": _utc(2025, 11, 30, 15, 0),
        },
        {
            "key": "h4_dizziness",
            "id": _uid("highlight:h4-dizziness"),
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e8_session_dizziness"],
            "quote": "Episodes of dizziness",
            "risk_reason": "Possible losartan side effect - monitor and consider dose adjustment",
            "risk_level": "medium",
            "source": "ai",
            "status": "rejected",
            "confidence": 0.85,
            "provenance_key": "prov_session_2026",
            "created_by": None,
            "resolved_by": "u_marcus",
            "resolved_at": _utc(2026, 1, 6, 10, 0),
            "created_at": _utc(2026, 1, 5, 18, 0),
        },
    ]
    for item in rows:
        entry_id = item["entry_id"]
        body = state.conn.execute(
            "SELECT body FROM entry WHERE id = %s", (entry_id,)
        ).fetchone()[0]
        offset_start, offset_end = _quote_offsets(body, item["quote"])
        row = {
            "id": item["id"],
            "clinic_id": item["clinic_id"],
            "patient_id": item["patient_id"],
            "entry_id": entry_id,
            "offset_start": offset_start,
            "offset_end": offset_end,
            "quoted_text": item["quote"],
            "risk_reason": item["risk_reason"],
            "risk_level": item["risk_level"],
            "source": item["source"],
            "status": item["status"],
            "provenance_id": state.provenance[item["provenance_key"]],
            "created_at": item["created_at"],
        }
        if item["confidence"] is not None:
            row["confidence"] = item["confidence"]
        if item["created_by"] is not None:
            row["created_by"] = state.users[item["created_by"]]
        if item["resolved_by"] is not None:
            row["resolved_by"] = state.users[item["resolved_by"]]
        if item["resolved_at"] is not None:
            row["resolved_at"] = item["resolved_at"]
        _insert_row(state.conn, "highlight", row)
        state.bump("highlights")


# ---------------------------------------------------------------------------
# Step 10: learning interactions + weights (persisted self-learning demo)
# ---------------------------------------------------------------------------

def _seed_learning(state: SeedState) -> None:
    interactions = [
        {
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e2_ai_doctor"],
            "user_id": state.users["u_marcus"],
            "interaction_type": "highlight_accept",
            "feature_keys": Jsonb(["entity:allergy:penicillin", "section:plan"]),
            "weight_delta": 1.0,
            "occurred_at": _utc(2025, 4, 16, 9, 30),
        },
        {
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e4_ai_nurse"],
            "user_id": state.users["u_priya"],
            "interaction_type": "comment",
            "feature_keys": Jsonb(["keyword:follow_up"]),
            "weight_delta": 1.0,
            "occurred_at": _utc(2025, 5, 3, 10, 0),
        },
        {
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e6_lab_handoff"],
            "user_id": state.users["u_priya"],
            "interaction_type": "task_complete",
            "feature_keys": Jsonb(["keyword:lab_order"]),
            "weight_delta": 1.0,
            "occurred_at": _utc(2025, 9, 1, 12, 0),
        },
        {
            "clinic_id": state.clinics["meridian"],
            "patient_id": state.patients["p_alice"],
            "entry_id": state.entries["e8_session_dizziness"],
            "user_id": state.users["u_marcus"],
            "interaction_type": "highlight_reject",
            "feature_keys": Jsonb(["keyword:dizziness"]),
            "weight_delta": -1.0,
            "occurred_at": _utc(2026, 1, 6, 10, 0),
        },
    ]
    for row in interactions:
        _insert_row(state.conn, "learning_interaction", row)
        state.bump("learning_interactions")

    weights = [
        ("entity:allergy:penicillin", 1.0, 1, 0, 1),
        ("section:plan", 1.0, 1, 0, 1),
        ("keyword:follow_up", 1.0, 1, 0, 1),
        ("keyword:lab_order", 1.0, 1, 0, 1),
        ("keyword:dizziness", -1.0, 0, 1, 1),
    ]
    for feature_key, weight, positive, negative, total in weights:
        _insert_row(
            state.conn,
            "learning_weight",
            {
                "clinic_id": state.clinics["meridian"],
                "feature_key": feature_key,
                "weight": weight,
                "positive_count": positive,
                "negative_count": negative,
                "total_interactions": total,
                "last_seen_at": _utc(2026, 1, 6, 10, 0),
                "created_at": _utc(2026, 1, 6, 10, 0),
            },
        )
        state.bump("learning_weights")


# ---------------------------------------------------------------------------
# Step 11: entry_version snapshots (v1/v2 for the care-plan entry) + glance
# ---------------------------------------------------------------------------

def _seed_entry_versions(state: SeedState) -> None:
    v1_body = (
        "1. Amlodipine 5 mg OD.\n"
        "2. Ambulatory BP monitoring for 7 days.\n"
        "3. Review in 4 weeks."
    )
    v2_body = (
        "1. Amlodipine 5 mg OD.\n"
        "2. Ambulatory BP monitoring for 7 days.\n"
        "3. ECG at next visit.\n"
        "4. Review in 4 weeks.\n"
        "5. Added 24h Holter monitoring per cardiology advice (2025-05-02)."
    )
    entry_id = state.entries["e3_care_plan"]
    clinic_id = state.clinics["meridian"]

    v1 = {
        "entry_id": entry_id,
        "clinic_id": clinic_id,
        "version": 1,
        "body": v1_body,
        "delta_from_prev": None,
        "author_id": state.users["u_marcus"],
        "author_role": "clinician",
        "change_summary": None,
        "conflict_flag": False,
        "created_at": _utc(2025, 4, 15, 11, 30),
    }
    v2 = {
        "entry_id": entry_id,
        "clinic_id": clinic_id,
        "version": 2,
        "body": v2_body,
        "delta_from_prev": Jsonb(_char_diff(v1_body, v2_body)),
        "author_id": state.users["u_marcus"],
        "author_role": "clinician",
        "change_summary": "Added 24h Holter monitoring per cardiology recommendation.",
        "conflict_flag": False,
        "created_at": _utc(2025, 5, 2, 14, 0),
    }
    for row in (v1, v2):
        _insert_row(state.conn, "entry_version", row)
        state.bump("entry_versions")


def _recompute_glances(state: SeedState) -> None:
    """Precompute the patient_glance top-card via the SECURITY DEFINER function."""
    for patient_key in state.patients:
        state.conn.execute(
            "SELECT recompute_glance(%s)", (state.patients[patient_key],)
        )
        state.bump("glances")


# ---------------------------------------------------------------------------
# Step 12: PHI self-check over every AI-scribed body (redaction round-trip)
# ---------------------------------------------------------------------------

def _verify_phi(state: SeedState) -> None:
    failures: list[str] = []
    for entry_key, body in state.ai_bodies:
        redacted, mapping = redact(body)
        if contains_phi(redacted):
            failures.append(f"{entry_key}: PHI remained after redaction")
            continue
        if restore(redacted, mapping) != body:
            failures.append(f"{entry_key}: restore(redact(body)) != body")
    if failures:
        raise AssertionError(
            "PHI self-check failed:\n  " + "\n  ".join(failures)
        )


# ---------------------------------------------------------------------------
# Orchestration + summary
# ---------------------------------------------------------------------------

def _seed(conn: psycopg.Connection) -> SeedState:
    state = SeedState(conn)
    clinic_ids = [
        _uid("clinic:meridian"),
        _uid("clinic:harbourview"),
    ]
    _reset(conn, clinic_ids)
    _seed_clinics(state)
    _seed_users(state)
    _seed_patients(state)
    _seed_provenance(state)
    _seed_entries(state)
    _seed_ai_notes(state)
    _seed_entities(state)
    _seed_comments_and_mentions(state)
    _seed_tasks(state)
    _seed_highlights(state)
    _seed_learning(state)
    _seed_entry_versions(state)
    _recompute_glances(state)
    _verify_phi(state)
    return state


def _print_summary(state: SeedState) -> None:
    c = state.counts
    lines = [
        "=" * 72,
        "Nightingale synthetic seed applied (deterministic + idempotent).",
        "=" * 72,
        "",
        "Clinics:",
        f"  - Meridian Family Clinic    ({state.clinics['meridian']})",
        f"  - Harbourview Medical Centre({state.clinics['harbourview']})",
        "",
        "Users (all four roles, one per clinic):",
        "  - Meridian: patient Alice Tan, patient Bala Kumar, staff Priya Nair,",
        "              clinician Marcus Lee, admin Sandra Ho",
        "  - Harbourview: patient Mei Ling Chua, staff David Ong,",
        "              clinician Amelia Wong, admin Jason Lim",
        "",
        "Patients:",
        "  - Alice Tan (MRN-1001), Bala Kumar (MRN-1002), Mei Ling Chua (MRN-2001)",
        "",
        "Longitudinal timeline (fixed dates, incl. required 2025-04-15 and 2026-02-06):",
        f"  - {c.get('entries', 0)} entries: 3x staff_note, 4x clinician_note (plan/diagnosis),",
        "    7x AI-scribed (3x doctor, 1x nurse, 3x patient-session), all author_role=system",
        "    with provenance_pointer set and redaction_confirmed=true",
        "",
        "Supporting rows:",
        f"  - {c.get('provenance', 0)} provenance records (source session/segment pointers)",
        f"  - {c.get('comments', 0)} comments (1 resolved, 1 open with @mention, 1 reply thread,",
        "    1 patient-facing)",
        f"  - {c.get('mentions', 0)} mentions (unread notification queue)",
        f"  - {c.get('tasks', 0)} tasks (open 'needs lab order', in-progress 'waiting nurse",
        "    follow-up', 1 done)",
        f"  - {c.get('highlights', 0)} highlights (suggested/accepted/rejected; risk_reason +",
        "    provenance_pointer + exact span)",
        f"  - {c.get('entry_versions', 0)} entry versions (v1, v2 for the care-plan entry)",
        f"  - {c.get('entry_entities', 0)} tagged clinical entities",
        "    (allergy -> critical risk boost in the glance scoring)",
        f"  - {c.get('learning_interactions', 0)} learning interactions +",
        f"    {c.get('learning_weights', 0)} learning weights (self-learning demo)",
        f"  - {c.get('glances', 0)} precomputed patient_glance snapshots (recompute_glance)",
        "",
        "PHI self-check: every AI-scribed body redacts PHI-free and round-trips",
        "(restore(redact(body)) == body).",
        "",
        "How to run (backend/ directory, stack up):",
        "  python -m app.seed",
        "  # or inside the container:",
        "  docker compose exec api python -m app.seed",
        "",
        "Note: the repo Makefile's `make seed` runs `python -m app.seed` inside",
        "the api container (matching `make migrate`), so `make seed` is the",
        "canonical entry point.",
        "=" * 72,
    ]
    print("\n".join(lines))


def main() -> int:
    """Connect, seed, commit, and print the summary. Returns process exit code."""
    url = _database_url()
    host = url.split("@")[-1]
    print(f"== seed connecting to {host}", flush=True)
    try:
        with psycopg.connect(url) as conn:
            state = _seed(conn)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - report any failure, rollback is implicit
        print(f"!! seed FAILED: {exc}", file=sys.stderr)
        return 1
    _print_summary(state)
    print("== seed complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
