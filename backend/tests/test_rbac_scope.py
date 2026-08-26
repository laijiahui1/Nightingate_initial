"""RBAC scope micro-test (RED skeleton).

Proves the server-side RBAC matrix of docs/SECURITY.md §a / docs/PLAN.md Phase 2:

- a staff user can NEVER write/edit a clinician-authored entry — and a
  clinician can NEVER write/edit a staff-authored entry (section ownership +
  ``author_role`` write-isolation, enforced by RLS AND the route guards);
- a patient can NEVER see internal staff/clinician comments or raw AI-scribed
  notes (patient sees only ``patient_visible`` content and their own record);
- cross-clinic reads are denied (404 for out-of-scope ids, no existence leak).

Each test is a real test with fixtures. The DB-level RLS backstop is asserted
first (the RLS policies already exist); the API-level enforcement point (the
route guards from docs/PLAN.md Phase 2) is asserted second and is what keeps
these RED: the RBAC endpoints do not exist yet, so every request 404s and the
API assertion fails. Remove the xfail markers once the routes land.
"""

import psycopg
import pytest


@pytest.mark.asyncio
async def test_staff_cannot_edit_clinician_entry(role_conn, staff_client, entries):
    """Staff must not be able to write/edit a clinician-authored care plan."""
    entry_id = entries["e3_care_plan"]  # clinician-authored, section='plan'

    # (1) DB backstop (RLS): a staff UPDATE matches ZERO clinician rows.
    with role_conn("staff") as conn:
        cur = conn.execute(
            "UPDATE entry SET body = %s, updated_at = now() WHERE id = %s",
            ("staff attempted overwrite", entry_id),
        )
        assert cur.rowcount == 0, "RLS allowed a staff write to a clinician entry"

    # (2) API enforcement: the edit endpoint must deny the staff PUT (403).
    resp = await staff_client.put(
        f"/api/entries/{entry_id}",
        json={"body": "staff attempted overwrite", "base_version": 2},
    )
    assert resp.status_code == 403, "staff edit of a clinician entry was not denied"


@pytest.mark.asyncio
async def test_clinician_cannot_edit_staff_entry(role_conn, clinician_client, entries):
    """Clinician must not be able to write/edit a staff-authored handoff note."""
    entry_id = entries["e1_intake"]  # staff-authored, section='staff_handoff'

    # (1) DB backstop (RLS): a clinician UPDATE matches ZERO staff rows.
    with role_conn("clinician") as conn:
        cur = conn.execute(
            "UPDATE entry SET body = %s, updated_at = now() WHERE id = %s",
            ("clinician attempted overwrite", entry_id),
        )
        assert cur.rowcount == 0, "RLS allowed a clinician write to a staff entry"

    # (2) API enforcement: the edit endpoint must deny the clinician PUT (403).
    resp = await clinician_client.put(
        f"/api/entries/{entry_id}",
        json={"body": "clinician attempted overwrite", "base_version": 1},
    )
    assert resp.status_code == 403, "clinician edit of a staff entry was not denied"


@pytest.mark.asyncio
async def test_patient_cannot_access_internal_comments(
    role_conn, patient_client, patients, comments
):
    """Patient must see ZERO internal comments; only their own patient_visible one."""
    patient_id = patients["Alice Tan"]
    internal = [comments["c1_resolved"], comments["c2_open_mention"], comments["c3_reply"]]

    # (1) DB backstop (RLS): patient_role has no policy on 'internal' comments.
    with role_conn("patient") as conn:
        internal_visible = 0
        for cid in internal:
            internal_visible += conn.execute(
                "SELECT count(*) FROM comment WHERE id = %s", (cid,)
            ).fetchone()[0]
        assert internal_visible == 0, "patient role could read internal comments"
        own = conn.execute(
            "SELECT count(*) FROM comment WHERE id = %s", (comments["c4_patient"],)
        ).fetchone()[0]
        assert own == 1, "patient role could not read its own patient_visible comment"

    # (2) API enforcement: the patient page bundle must not include internal comments.
    resp = await patient_client.get(f"/api/patients/{patient_id}")
    assert resp.status_code == 200
    payload = resp.json()
    returned_ids = {str(c["id"]) for c in payload.get("comments", [])}
    assert not ({str(c) for c in internal} & returned_ids), (
        "patient bundle leaked internal comments"
    )


@pytest.mark.asyncio
async def test_patient_cannot_access_raw_ai_notes(role_conn, patient_client, patients, entries):
    """Patient must never read raw AI-scribed notes or the ai_scribed_note table."""
    patient_id = patients["Alice Tan"]
    ai_entries = [
        entries["e2_ai_doctor"],
        entries["e4_ai_nurse"],
        entries["e5_ai_session"],
        entries["e8_session_dizziness"],
    ]

    # (1) DB backstop (RLS): no patient policy on raw AI entries, and patient_role
    #     has no grant at all on the ai_scribed_note table.
    with role_conn("patient") as conn:
        for eid in ai_entries:
            n = conn.execute("SELECT count(*) FROM entry WHERE id = %s", (eid,)).fetchone()[0]
            assert n == 0, "patient role could read a raw AI-scribed entry"
        with pytest.raises(psycopg.Error):
            conn.execute("SELECT count(*) FROM ai_scribed_note")

    # (2) API enforcement: the patient page bundle must not include raw AI notes.
    resp = await patient_client.get(f"/api/patients/{patient_id}")
    assert resp.status_code == 200
    payload = resp.json()
    returned_entry_ids = {str(e["id"]) for e in payload.get("entries", [])}
    assert not ({str(e) for e in ai_entries} & returned_entry_ids), (
        "patient bundle leaked raw AI notes"
    )


@pytest.mark.asyncio
async def test_cross_clinic_patient_read_denied(role_conn, staff_client, patients_b):
    """Meridian staff must never read a Harbourview patient (404, not 200)."""
    mei_patient_id = patients_b["Mei Ling Chua"]  # Harbourview Medical Centre

    # (1) DB backstop (RLS): clinic-scoped patient policy hides the other clinic.
    with role_conn("staff") as conn:
        n = conn.execute(
            "SELECT count(*) FROM patient WHERE id = %s", (mei_patient_id,)
        ).fetchone()[0]
        assert n == 0, "RLS leaked a cross-clinic patient to staff"

    # (2) API enforcement: out-of-scope ids resolve to 404 (no existence leak).
    resp = await staff_client.get(f"/api/patients/{mei_patient_id}")
    assert resp.status_code == 404, "cross-clinic patient read was not denied"


@pytest.mark.asyncio
async def test_cross_clinic_entry_edit_and_revert_404(admin_conn, clinic_b_id, clinician_client):
    """Meridian clinician editing a Harbourview entry → 404 (NOT 403, no leak).

    Covers both PUT /entries/{id} and POST /entries/{id}/revert: an out-of-scope
    id must resolve uniformly to 404 so the caller cannot distinguish
    "not found" from "exists but forbidden" (no existence leak).
    """
    harbourview_entry = admin_conn.execute(
        "SELECT id FROM entry WHERE clinic_id = %s LIMIT 1", (clinic_b_id,)
    ).fetchone()
    assert harbourview_entry is not None, "seed produced no Harbourview entry"
    entry_id = harbourview_entry[0]

    resp = await clinician_client.put(
        f"/api/entries/{entry_id}",
        json={"body": "cross-clinic overwrite", "base_version": 1},
    )
    assert resp.status_code == 404, f"cross-clinic edit returned {resp.status_code}, expected 404"

    resp = await clinician_client.post(
        f"/api/entries/{entry_id}/revert",
        json={"target_version": 1},
    )
    assert resp.status_code == 404, f"cross-clinic revert returned {resp.status_code}, expected 404"


@pytest.mark.asyncio
async def test_admin_edit_and_revert_clinician_entry(admin_client, entries):
    """Admin (∉ entry_author_role) edits/reverts a clinician entry → 200.

    Regression guard for the M2 latent enum-cast bug: `admin` is not a valid
    `entry_author_role`, so the route must resolve `role_arg` to the entry's own
    author_role ('clinician') rather than pass the caller's raw role.
    """
    entry_id = entries["e3_care_plan"]  # clinician-authored, version 2

    resp = await admin_client.put(
        f"/api/entries/{entry_id}",
        json={"body": "admin-edited clinician entry", "base_version": 2},
    )
    assert resp.status_code == 200, f"admin edit of clinician entry returned {resp.status_code}"
    assert resp.json()["body"] == "admin-edited clinician entry"

    resp = await admin_client.post(
        f"/api/entries/{entry_id}/revert",
        json={"target_version": 1},
    )
    assert resp.status_code == 200, f"admin revert of clinician entry returned {resp.status_code}"


@pytest.mark.asyncio
async def test_admin_edit_and_revert_staff_entry(admin_client, entries):
    """Admin edits/reverts a staff entry → 200 (role_arg resolves to 'staff')."""
    entry_id = entries["e1_intake"]  # staff-authored, version 1

    resp = await admin_client.put(
        f"/api/entries/{entry_id}",
        json={"body": "admin-edited staff entry", "base_version": 1},
    )
    assert resp.status_code == 200, f"admin edit of staff entry returned {resp.status_code}"
    assert resp.json()["body"] == "admin-edited staff entry"

    # The edit above wrote the first entry_version snapshot for this (previously
    # unversioned) staff entry; reverting to it exercises the staff role_arg path
    # through revert_entry.
    resp = await admin_client.post(
        f"/api/entries/{entry_id}/revert",
        json={"target_version": 1},
    )
    assert resp.status_code == 200, f"admin revert of staff entry returned {resp.status_code}"


@pytest.mark.asyncio
async def test_comment_author_role_cannot_be_spoofed(
    role_conn, staff_client, roles, patients, entries
):
    """A staff author cannot be spoofed as 'clinician' at DB or API level."""
    clinic_id = roles["staff"][1]
    staff_uid = roles["staff"][0]
    patient_id = patients["Alice Tan"]
    entry_id = entries["e1_intake"]

    # (1) DB backstop (RLS WITH CHECK): a staff INSERT claiming
    #     author_role='clinician' violates the comment_ins_staff policy.
    with role_conn("staff") as conn:
        with pytest.raises(psycopg.Error) as exc_info:
            conn.execute(
                "INSERT INTO comment(clinic_id, patient_id, entry_id, author_id, "
                "author_role, body) VALUES (%s, %s, %s, %s, 'clinician', 'spoofed')",
                (clinic_id, patient_id, entry_id, staff_uid),
            )
        assert "row-level security" in str(exc_info.value).lower(), (
            f"spoofed INSERT failed for the wrong reason: {exc_info.value}"
        )

    # (2) API enforcement: the route binds author_id = actor.user_id and
    #     author_role = the actor's role (never trusts a client-supplied value).
    resp = await staff_client.post(
        f"/api/entries/{entry_id}/comments",
        json={"body": "staff note", "visibility": "internal"},
    )
    assert resp.status_code == 201, f"staff comment POST returned {resp.status_code}"
    data = resp.json()
    assert data["author_role"] == "staff", (
        f"comment author_role was not 'staff': {data['author_role']!r}"
    )
    assert data["author_id"] == str(staff_uid), "comment author_id was not the acting user"


def test_audit_log_append_only(admin_conn, role_conn):
    """No user role has INSERT/UPDATE/DELETE/TRUNCATE on audit_log (append-only).

    audit_log writes flow exclusively through the SECURITY DEFINER `log_audit`
    (which runs as the owner, not the caller), so no human role class is granted
    any DML on the table — only admin_role has SELECT for the audit query.
    """
    role_classes = ("patient_role", "staff_role", "clinician_role", "admin_role")
    for role in role_classes:
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            has = admin_conn.execute(
                "SELECT has_table_privilege(%s, 'audit_log', %s)", (role, priv)
            ).fetchone()[0]
            assert not has, f"{role} has {priv} on audit_log"

    has_select = admin_conn.execute(
        "SELECT has_table_privilege('admin_role', 'audit_log', 'SELECT')"
    ).fetchone()[0]
    assert has_select, "admin_role lost SELECT on audit_log"

    # Clinical roles cannot DELETE directly (no grant + no DELETE policy).
    for role in ("staff", "clinician"):
        with role_conn(role) as conn:
            with pytest.raises(psycopg.Error):
                conn.execute("DELETE FROM audit_log")


@pytest.mark.asyncio
async def test_patient_clinical_endpoints_denied(patient_client, patients):
    """Patient gets 403 on the clinical-only glance and highlights endpoints."""
    patient_id = patients["Alice Tan"]

    resp = await patient_client.get(f"/api/patients/{patient_id}/glance")
    assert resp.status_code == 403, f"patient glance returned {resp.status_code}, expected 403"

    resp = await patient_client.get(f"/api/patients/{patient_id}/highlights")
    assert resp.status_code == 403, f"patient highlights returned {resp.status_code}, expected 403"
