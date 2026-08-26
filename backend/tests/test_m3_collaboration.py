"""M3 collaboration tests: comments/@mentions/resolve, tasks, revision history,
cross-clinic isolation, admin edit, and unread-mention roundtrip.

Persistence is proven through the ``db`` fixture (raw SQL as the restricted
role) beyond the API response. All product assertions are English.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_comment_post_with_mention_creates_mention_row(
    clinician_client, db, entries, roles, patients
):
    """Clinician posts a comment mentioning Priya; a mention row is created."""
    entry_id = entries["e9_review"]
    priya_id = roles["staff"][0]
    body = "Please review this. @priya.nair to coordinate."

    resp = await clinician_client.post(f"/api/entries/{entry_id}/comments", json={"body": body})
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["body"] == body
    assert data["author_role"] == "clinician"
    comment_id = data["id"]

    row = db.execute(
        "SELECT comment_id FROM mention WHERE mentioned_user_id = %s AND comment_id = %s",
        (priya_id, comment_id),
    ).fetchone()
    assert row is not None, "mention row was not persisted for Priya"

    patient_id = patients["Alice Tan"]
    resp = await clinician_client.get(f"/api/patients/{patient_id}")
    payload = resp.json()
    assert any(c["id"] == comment_id for c in payload["comments"]), (
        "new comment missing from the clinician bundle"
    )


@pytest.mark.asyncio
async def test_patient_comment_and_visibility_scoping(
    patient_client, staff_client, entries, patients
):
    """Patient comments are forced patient_visible; internal ones stay hidden."""
    entry_id = entries["e_patient_discharge"]
    patient_id = patients["Alice Tan"]

    resp = await patient_client.post(
        f"/api/entries/{entry_id}/comments", json={"body": "I have a question about my meds."}
    )
    assert resp.status_code == 201, resp.text
    patient_comment_id = resp.json()["id"]
    assert resp.json()["visibility"] == "patient_visible"

    resp = await staff_client.post(
        f"/api/entries/{entry_id}/comments", json={"body": "Internal: confirm dosage."}
    )
    assert resp.status_code == 201, resp.text
    internal_id = resp.json()["id"]

    resp = await patient_client.get(f"/api/patients/{patient_id}")
    payload = resp.json()
    comment_ids = {c["id"] for c in payload["comments"]}
    assert patient_comment_id in comment_ids
    assert internal_id not in comment_ids, "internal comment leaked to patient bundle"


@pytest.mark.asyncio
async def test_comment_resolve_rules(
    staff_client, clinician_client, admin_client, entries, comments
):
    """Resolve honors author-role: staff resolves staff, clinician cannot staff, admin any."""
    entry_id = entries["e9_review"]
    resp = await staff_client.post(
        f"/api/entries/{entry_id}/comments", json={"body": "Staff note to resolve."}
    )
    assert resp.status_code == 201
    staff_comment_id = resp.json()["id"]

    resp = await staff_client.post(f"/api/comments/{staff_comment_id}/resolve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"
    assert resp.json()["resolved_by"] is not None

    # Clinician cannot resolve a staff-authored comment.
    resp = await clinician_client.post(f"/api/comments/{comments['c1_resolved']}/resolve")
    assert resp.status_code == 403

    # Admin resolves a clinician-authored comment.
    resp = await admin_client.post(f"/api/comments/{comments['c3_reply']}/resolve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"


@pytest.mark.asyncio
async def test_task_create_list_update(clinician_client, patient_client, patients, roles):
    """Clinician creates/updates a task; patient list is 403."""
    patient_id = patients["Alice Tan"]
    assignee_id = roles["staff"][0]

    resp = await clinician_client.post(
        f"/api/patients/{patient_id}/tasks",
        json={
            "title": "Schedule follow-up call",
            "description": "Call Alice to review BP readings.",
            "assignee_id": str(assignee_id),
            "priority": "high",
        },
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["status"] == "open"
    task_id = task["id"]

    resp = await clinician_client.get(f"/api/patients/{patient_id}/tasks")
    assert resp.status_code == 200
    assert any(t["id"] == task_id for t in resp.json())

    resp = await clinician_client.patch(f"/api/tasks/{task_id}", json={"status": "done"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "done"
    assert resp.json()["completed_at"] is not None

    resp = await patient_client.get(f"/api/patients/{patient_id}/tasks")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_task_assignee_must_be_in_clinic(clinician_client, patients, roles_b):
    """A cross-clinic assignee is rejected 422 (explicit check + trigger)."""
    patient_id = patients["Alice Tan"]
    harbourview_assignee = roles_b["clinician"][0]

    resp = await clinician_client.post(
        f"/api/patients/{patient_id}/tasks",
        json={"title": "Invalid task", "assignee_id": str(harbourview_assignee)},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_revision_history_endpoint(clinician_client, db, entries):
    """PUT bumps a version, history preserves old bodies, revert restores v1."""
    entry_id = entries["e3_care_plan"]
    v1_body = db.execute(
        "SELECT body FROM entry_version WHERE entry_id = %s AND version = 1", (entry_id,)
    ).fetchone()[0]
    v2_body = db.execute(
        "SELECT body FROM entry_version WHERE entry_id = %s AND version = 2", (entry_id,)
    ).fetchone()[0]
    current = db.execute("SELECT version FROM entry WHERE id = %s", (entry_id,)).fetchone()[0]

    resp = await clinician_client.put(
        f"/api/entries/{entry_id}",
        json={"body": "1. Amlodipine 5 mg OD.\n2. Review in 2 weeks.", "base_version": current},
    )
    assert resp.status_code == 200, resp.text

    resp = await clinician_client.get(f"/api/entries/{entry_id}/history")
    assert resp.status_code == 200
    versions = resp.json()
    version_numbers = [v["version"] for v in versions]
    assert current + 1 in version_numbers
    assert any(v["version"] == 2 and v["body"] == v2_body for v in versions), (
        "old body not preserved in history"
    )

    resp = await clinician_client.post(
        f"/api/entries/{entry_id}/revert", json={"target_version": 1}
    )
    assert resp.status_code == 200, resp.text
    body_after = db.execute("SELECT body FROM entry WHERE id = %s", (entry_id,)).fetchone()[0]
    assert body_after == v1_body

    resp = await clinician_client.get(f"/api/entries/{entry_id}/history")
    assert any(v["change_summary"] == "reverted to version 1" for v in resp.json())


@pytest.mark.asyncio
async def test_cross_clinic_edit_is_404(clinician_client, admin_conn, clinic_b_id):
    """A Meridian clinician editing a Harbourview entry gets 404 (no leak)."""
    hb_entry = admin_conn.execute(
        "SELECT id FROM entry WHERE clinic_id = %s LIMIT 1", (clinic_b_id,)
    ).fetchone()
    assert hb_entry is not None, "no Harbourview entry found to test against"

    resp = await clinician_client.put(
        f"/api/entries/{hb_entry[0]}",
        json={"body": "attempted cross-clinic edit", "base_version": 1},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_edit_entry(admin_client, db, entries):
    """Admin can edit a clinician-authored entry; version bumps (enum-cast fix)."""
    entry_id = entries["e3_care_plan"]
    resp = await admin_client.put(
        f"/api/entries/{entry_id}",
        json={"body": "Admin-edited plan body.", "base_version": 2},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 3


@pytest.mark.asyncio
async def test_unread_mentions_roundtrip(staff_client, db, comments):
    """Staff sees the seeded mention from Marcus, then marks it read."""
    m_c3 = db.execute(
        "SELECT id FROM mention WHERE comment_id = %s", (comments["c3_reply"],)
    ).fetchone()
    assert m_c3 is not None, "seeded mention m_c3 not found"
    mention_id = m_c3[0]

    resp = await staff_client.get("/api/mentions/unread")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert str(mention_id) in [m["id"] for m in items]
    mine = next(m for m in items if m["id"] == str(mention_id))
    assert mine["context"]["comment_author_role"] == "clinician"

    resp = await staff_client.post(f"/api/mentions/{mention_id}/read")
    assert resp.status_code == 200, resp.text

    resp = await staff_client.get("/api/mentions/unread")
    assert str(mention_id) not in [m["id"] for m in resp.json()]
