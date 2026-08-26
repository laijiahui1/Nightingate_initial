"""M2 Care Note vertical-slice assertions (GREEN contract).

Focused on the end-to-end Care Note: page bundle scoping, glance top-card,
AI-scribe ingestion (redaction runs before the LLM), dev login, and
cross-clinic isolation. Persistence is proven by reading rows back through the
``db`` fixture (raw SQL, as the restricted role) beyond the API response.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_page_bundle_shapes_and_scoping(
    clinician_client, patient_client, patients, entries, comments
):
    """Clinician sees the full bundle; patient sees only patient_visible content."""
    patient_id = patients["Alice Tan"]

    resp = await clinician_client.get(f"/api/patients/{patient_id}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["entries"], "clinician bundle should have entries"
    for entry in payload["entries"]:
        assert "id" in entry and "body" in entry and "author_role" in entry

    resp = await patient_client.get(f"/api/patients/{patient_id}")
    assert resp.status_code == 200
    payload = resp.json()
    entry_ids = {entry["id"] for entry in payload["entries"]}
    comment_ids = {comment["id"] for comment in payload["comments"]}
    assert str(entries["e_patient_discharge"]) in entry_ids
    assert str(entries["e2_ai_doctor"]) not in entry_ids
    assert str(comments["c4_patient"]) in comment_ids


@pytest.mark.asyncio
async def test_glance_top_card(clinician_client, patient_client, patients):
    """Clinical roles read the seeded glance; patient role is denied."""
    patient_id = patients["Alice Tan"]

    resp = await clinician_client.get(f"/api/patients/{patient_id}/glance")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["top_items"], "seeded glance should have top_items"
    assert payload["computed_at"] is not None

    resp = await patient_client.get(f"/api/patients/{patient_id}/glance")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_ai_scribe_ingestion(clinician_client, db, patients):
    """AI-scribe ingests a PHI-bearing transcript with redaction-before-LLM."""
    patient_id = patients["Alice Tan"]
    transcript = "Patient Alice Tan called from +65 9123 4567 to report feeling better."

    resp = await clinician_client.post(
        f"/api/patients/{patient_id}/ai-scribe",
        json={"scribe_type": "ai_doctor_consult_summary", "transcript": transcript},
    )
    assert resp.status_code == 201, resp.text
    result = resp.json()
    assert result["redaction_confirmed"] is True
    assert result["redaction_mask"], "redaction_mask should list categories"

    entry_id = result["entry_id"]
    row = db.execute(
        "SELECT redaction_confirmed, redaction_mask FROM ai_scribed_note WHERE entry_id = %s",
        (entry_id,),
    ).fetchone()
    assert row is not None, "ai_scribed_note row was not persisted"
    assert row[0] is True
    name_count = next((m["count"] for m in row[1] if m["category"] == "NAME"), 0)
    assert name_count > 0, "redaction_mask should record a NAME redaction"

    resp = await clinician_client.get(f"/api/patients/{patient_id}")
    assert resp.status_code == 200
    payload = resp.json()
    new_entry = next((e for e in payload["entries"] if e["id"] == entry_id), None)
    assert new_entry is not None, "new AI entry missing from the bundle"
    assert new_entry["ai"] is not None and new_entry["ai"]["model_name"]


@pytest.mark.asyncio
async def test_dev_login_roundtrip(test_client, seed_db):
    """Dev login issues a token for a seeded user; unknown email is 401."""
    from app.core.security import decode_access_token

    resp = await test_client.post("/api/auth/login", json={"email": "priya.nair@meridian.demo"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["role"] == "staff"
    claims = decode_access_token(body["access_token"])
    assert claims["role"] == "staff"

    resp = await test_client.post("/api/auth/login", json={"email": "nobody@example.com"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_cross_clinic_glance_404(clinician_client, patients_b):
    """Meridian clinician cannot read a Harbourview patient's glance."""
    mei_patient_id = patients_b["Mei Ling Chua"]
    resp = await clinician_client.get(f"/api/patients/{mei_patient_id}/glance")
    assert resp.status_code == 404
