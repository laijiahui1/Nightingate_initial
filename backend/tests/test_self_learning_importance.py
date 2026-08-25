"""Self-learning importance micro-test (M6, live).

Accepting a highlight on an AI-scribed note records a ``learning_interaction``
and persists a POSITIVE ``learning_weight`` for the note's extracted entities
(``entity:<type>:<value>``), which then boosts the ``suggestions`` endpoint's
ranking for matching content.
"""

import pytest


@pytest.mark.asyncio
async def test_pinning_highlight_boosts_similar_suggestions(
    clinician_client, db, patients, entries
):
    """Accepting a highlight on an AI note boosts later suggestions for similar content."""
    patient_id = patients["Alice Tan"]
    ai_entry = entries["e2_ai_doctor"]  # AI doctor consult summary mentioning amlodipine

    # 1) Generate a highlight suggestion on the AI-scribed note, then ACCEPT it
    #    (accepting/pinning is the learning signal).
    gen = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/generate",
        json={"entry_id": str(ai_entry)},
    )
    assert gen.status_code == 200, "highlight generation failed"
    hl = gen.json()
    accept = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/{hl['id']}/accept",
        json={},
    )
    assert accept.status_code == 200, "highlight accept failed"

    # 2) Persistence contract: an interaction AND a positive weight must exist
    #    for the features extracted from that note (feature key format follows
    #    DATA_SCHEMA.md §3.11: 'entity:<type>:<value>').
    weight = db.execute(
        "SELECT weight, positive_count, total_interactions FROM learning_weight "
        "WHERE feature_key = %s",
        ("entity:medication:amlodipine",),
    ).fetchone()
    assert weight is not None, "pinning did not persist a learning weight"
    assert weight[0] > 0 and weight[1] >= 1 and weight[2] >= 1, (
        "pinning did not persist a positive weight"
    )
    interactions = db.execute(
        "SELECT count(*) FROM learning_interaction "
        "WHERE entry_id = %s AND interaction_type = 'highlight_accept'",
        (ai_entry,),
    ).fetchone()[0]
    assert interactions >= 1, "pinning did not log a learning interaction"

    # 3) Boost contract (conceptual): a NEW AI note mentioning amlodipine should
    #    now rank higher than it would have before the pin.
    sugg = await clinician_client.get(
        f"/api/patients/{patient_id}/highlights/suggestions?q=amlodipine"
    )
    assert sugg.status_code == 200, "suggestions endpoint failed"
    ranked = sugg.json().get("suggestions", [])
    assert ranked, "no suggestions returned for similar content"
    assert ranked[0]["score"] > 0, "pinned content did not get increased priority"
