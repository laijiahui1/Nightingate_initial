"""M6 self-learning + data-decay micro-tests.

Covers the learning-loop closure (accept boosts, reject depresses) and the
admin-only data-decay / restore cycle.
"""

import pytest


@pytest.mark.asyncio
async def test_suggestions_boost_after_accept(clinician_client, db, patients, entries):
    """A fresh entity scores 0 before any accept, then > 0 after one accept."""
    patient_id = patients["Alice Tan"]
    ai_entry = entries["e2_ai_doctor"]  # mentions amlodipine; entity is seeded

    before = await clinician_client.get(
        f"/api/patients/{patient_id}/highlights/suggestions?q=amlodipine"
    )
    assert before.status_code == 200
    ranked = before.json()["suggestions"]
    assert ranked, "no suggestions returned for amlodipine"
    assert ranked[0]["feature_key"] == "entity:medication:amlodipine"
    assert ranked[0]["score"] == 0, "fresh entity should score 0 before any accept"

    gen = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/generate",
        json={"entry_id": str(ai_entry)},
    )
    assert gen.status_code == 200
    hl = gen.json()
    accept = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/{hl['id']}/accept", json={}
    )
    assert accept.status_code == 200

    after = await clinician_client.get(
        f"/api/patients/{patient_id}/highlights/suggestions?q=amlodipine"
    )
    assert after.status_code == 200
    ranked = after.json()["suggestions"]
    assert ranked, "no suggestions returned after accept"
    assert ranked[0]["feature_key"] == "entity:medication:amlodipine"
    assert ranked[0]["score"] > 0, "accepted entity should score > 0"


@pytest.mark.asyncio
async def test_reject_drives_weight_negative(clinician_client, db, patients, entries):
    """Rejecting a highlight depresses the entry entity's learned weight."""
    patient_id = patients["Alice Tan"]
    dizzy_entry = entries["e8_session_dizziness"]  # entity: symptom/dizziness

    gen = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/generate",
        json={"entry_id": str(dizzy_entry)},
    )
    assert gen.status_code == 200
    hl = gen.json()
    reject = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/{hl['id']}/reject", json={}
    )
    assert reject.status_code == 200

    row = db.execute(
        "SELECT weight, negative_count FROM learning_weight WHERE feature_key = %s",
        ("entity:symptom:dizziness",),
    ).fetchone()
    assert row is not None, "reject did not persist a learning weight"
    assert row[0] < 0 and row[1] >= 1, "reject did not drive the weight negative"


@pytest.mark.asyncio
async def test_patient_suggestions_forbidden(patient_client, patients):
    """Patients can never read learned-weight suggestions."""
    patient_id = patients["Alice Tan"]
    resp = await patient_client.get(
        f"/api/patients/{patient_id}/highlights/suggestions?q=amlodipine"
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_decay_and_restore(admin_client, db, entries):
    """Decay archives an open-work-free entry; restore brings its body back."""
    entry_id = entries["e1_intake"]

    decay = await admin_client.post("/api/admin/decay", json={"days": 0})
    assert decay.status_code == 200
    assert decay.json()["decayed"] >= 1

    row = db.execute("SELECT is_decayed, body FROM entry WHERE id = %s", (entry_id,)).fetchone()
    assert row is not None, "decayed entry vanished"
    assert row[0] is True, "entry should be flagged decayed"
    assert row[1] == "", "decayed entry body should be blanked"

    restore = await admin_client.post(f"/api/entries/{entry_id}/restore", json={})
    assert restore.status_code == 200
    assert restore.json()["body"], "restored body should be non-empty"

    row2 = db.execute("SELECT is_decayed, body FROM entry WHERE id = %s", (entry_id,)).fetchone()
    assert row2 is not None
    assert row2[0] is False, "entry should be un-decayed after restore"
    assert row2[1], "entry body should be restored after restore"


@pytest.mark.asyncio
async def test_staff_decay_forbidden(staff_client):
    """Non-admin roles cannot trigger data decay."""
    resp = await staff_client.post("/api/admin/decay", json={"days": 0})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_decay_is_clinic_scoped(admin_client, admin_conn, clinic_b_id):
    """A Meridian admin's decay must never archive a Harbourview entry.

    Regression for the M6 verifier HIGH finding: ``decay_old_entries`` ran
    SECURITY DEFINER with no ``clinic_id = app_clinic_id()`` predicate, so an
    admin at one clinic blanked every clinic's stale entries. The function is
    now clinic-scoped; the other clinic's rows must be untouched.
    """
    harbourview_entry = admin_conn.execute(
        "SELECT id FROM entry WHERE clinic_id = %s AND is_decayed = false LIMIT 1",
        (clinic_b_id,),
    ).fetchone()
    assert harbourview_entry is not None, "seed produced no live Harbourview entry"
    hb_id = harbourview_entry[0]

    resp = await admin_client.post("/api/admin/decay", json={"days": 0})
    assert resp.status_code == 200, f"decay failed: {resp.text}"

    row = admin_conn.execute(
        "SELECT is_decayed, body FROM entry WHERE id = %s", (hb_id,)
    ).fetchone()
    assert row is not None
    assert row[0] is False, "cross-clinic entry was decayed by another clinic's admin"
    assert row[1], "cross-clinic entry body was blanked"


@pytest.mark.asyncio
async def test_restore_then_redacay_is_idempotent(admin_client, db, entries):
    """restore -> decay -> restore cycle must not raise UniqueViolation.

    Regression for the M6 verifier MEDIUM finding: ``restore_entry`` deleted
    the ``entry_archive`` row but left ``entry_version_archive`` rows behind,
    so a second decay re-inserted them and hit the primary key. The decay
    inserts are now idempotent and restore clears both archives.
    """
    entry_id = entries["e1_intake"]

    first = await admin_client.post("/api/admin/decay", json={"days": 0})
    assert first.status_code == 200
    restore1 = await admin_client.post(f"/api/entries/{entry_id}/restore", json={})
    assert restore1.status_code == 200, f"first restore failed: {restore1.text}"

    # Second decay + restore cycle must succeed (no archive unique violation).
    second = await admin_client.post("/api/admin/decay", json={"days": 0})
    assert second.status_code == 200, f"second decay failed: {second.text}"
    restore2 = await admin_client.post(f"/api/entries/{entry_id}/restore", json={})
    assert restore2.status_code == 200, f"second restore failed: {restore2.text}"
    assert restore2.json()["body"], "restored body should be non-empty after cycle"
