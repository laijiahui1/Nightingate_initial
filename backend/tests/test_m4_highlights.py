"""M4 highlights tests: generate / list / accept / reject + span-stable provenance.

The generator is deterministic: on the seeded fixture, generating on
``e2_ai_doctor`` skips the already-accepted "Penicillin" span and picks
"BP 148/92". Accept/reject transition ``suggested -> accepted|rejected`` and feed
the M6 self-learning loop (``record_interaction``) with the entry's entity
features, so ``learning_weight`` moves positive on accept and negative on reject.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_accept_transitions_and_learns_positive(clinician_client, db, patients, entries):
    """Accept moves suggested->accepted and pushes entity weights positive."""
    patient_id = patients["Alice Tan"]
    source_entry = entries["e2_ai_doctor"]

    resp = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/generate",
        json={"entry_id": str(source_entry)},
    )
    assert resp.status_code == 200, resp.text
    hl = resp.json()
    assert hl["quoted_text"] == "BP 148/92", "deterministic generator must pick BP 148/92"
    assert hl["status"] == "suggested"

    resp = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/{hl['id']}/accept", json={}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"
    assert resp.json()["resolved_by"] is not None

    row = db.execute(
        "SELECT weight FROM learning_weight WHERE feature_key = %s",
        ("entity:medication:amlodipine",),
    ).fetchone()
    assert row is not None, "accept did not create a weight for the entry's entity feature"
    assert row[0] > 0, "accept must push the entity feature weight positive"


@pytest.mark.asyncio
async def test_reject_transitions_and_learns_negative(clinician_client, db, patients, entries):
    """Reject moves suggested->rejected and pushes entity weights negative."""
    patient_id = patients["Alice Tan"]
    source_entry = entries["e2_ai_doctor"]

    resp = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/generate",
        json={"entry_id": str(source_entry)},
    )
    assert resp.status_code == 200, resp.text
    hl = resp.json()

    resp = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/{hl['id']}/reject", json={}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"

    row = db.execute(
        "SELECT weight FROM learning_weight WHERE feature_key = %s",
        ("entity:medication:amlodipine",),
    ).fetchone()
    assert row is not None, "reject did not create a weight for the entry's entity feature"
    assert row[0] < 0, "reject must push the entity feature weight negative"


@pytest.mark.asyncio
async def test_accept_on_resolved_highlight_is_409(clinician_client, patients, highlights):
    """Accepting an already-accepted highlight is rejected 409."""
    patient_id = patients["Alice Tan"]
    resolved_id = highlights["h2_penicillin"]  # seeded as status='accepted'

    resp = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/{resolved_id}/accept", json={}
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_duplicate_generate_does_not_duplicate_spans(clinician_client, db, patients, entries):
    """Generating twice never creates a second span for the same quoted_text."""
    patient_id = patients["Alice Tan"]
    source_entry = entries["e2_ai_doctor"]

    first = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/generate",
        json={"entry_id": str(source_entry)},
    )
    assert first.status_code == 200, first.text
    assert first.json()["quoted_text"] == "BP 148/92"

    second = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/generate",
        json={"entry_id": str(source_entry)},
    )
    assert second.status_code == 200, second.text
    # The second call picks a different phrase (BP + Penicillin now covered).
    assert second.json()["quoted_text"] != "BP 148/92"

    count = db.execute(
        "SELECT count(*) FROM highlight WHERE entry_id = %s AND quoted_text = %s",
        (source_entry, "BP 148/92"),
    ).fetchone()[0]
    assert count == 1, "duplicate generate created a second BP 148/92 span"


@pytest.mark.asyncio
async def test_patient_role_is_403(patient_client, patients, entries):
    """Patients get 403 on list, generate, and accept."""
    patient_id = patients["Alice Tan"]
    source_entry = entries["e2_ai_doctor"]

    assert (await patient_client.get(f"/api/patients/{patient_id}/highlights")).status_code == 403
    resp = await patient_client.post(
        f"/api/patients/{patient_id}/highlights/generate",
        json={"entry_id": str(source_entry)},
    )
    assert resp.status_code == 403
    resp = await patient_client.post(
        f"/api/patients/{patient_id}/highlights/{source_entry}/accept", json={}
    )
    assert resp.status_code == 403
