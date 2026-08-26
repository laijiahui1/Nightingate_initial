"""Highlight provenance micro-tests (live contract).

docs/PLAN.md Phase 7 + docs/SECURITY.md test mapping:

- generating a highlight from an AI-scribed note produces a
  ``provenance_pointer`` that RESOLVES: the frozen ``quoted_text`` at
  ``(entry_id, offset_start, offset_end)`` must equal the slice of the authored
  source body — clicking a highlight jumps to the exact timeline span;
- every highlight surfaced to a clinician carries a ``risk_reason`` and a
  resolvable provenance pointer.
"""

import pytest


@pytest.mark.asyncio
async def test_generated_highlight_provenance_resolves_to_span(
    clinician_client, db, patients, entries
):
    """A highlight generated from an AI-scribed note must resolve to a real span."""
    patient_id = patients["Alice Tan"]
    source_entry = entries["e2_ai_doctor"]  # AI doctor consult summary (author_role=system)

    resp = await clinician_client.post(
        f"/api/patients/{patient_id}/highlights/generate",
        json={"entry_id": str(source_entry)},
    )
    assert resp.status_code == 200, "highlight generation failed"

    hl = resp.json()
    # Contract: a generated highlight carries a resolvable provenance pointer.
    assert hl.get("provenance_id") is not None, "highlight is missing a provenance pointer"
    assert hl.get("entry_id") is not None, "highlight is missing its source entry"
    assert hl.get("offset_start") is not None and hl.get("offset_end") is not None
    assert hl.get("quoted_text"), "highlight must freeze the quoted text"

    # Resolve: the frozen span must slice the authored source body exactly.
    body = db.execute("SELECT body FROM entry WHERE id = %s", (hl["entry_id"],)).fetchone()[0]
    resolved = body[hl["offset_start"] : hl["offset_end"]]
    assert resolved == hl["quoted_text"], "provenance pointer did not resolve to a real span"


@pytest.mark.asyncio
async def test_highlights_carry_risk_reason_and_resolvable_provenance(
    clinician_client, db, patients
):
    """Every surfaced highlight shows risk_reason and a resolvable span."""
    patient_id = patients["Alice Tan"]

    resp = await clinician_client.get(f"/api/patients/{patient_id}/highlights")
    assert resp.status_code == 200, "highlights list failed"

    highlights = resp.json().get("highlights", [])
    assert highlights, "expected seeded highlights for Alice Tan"
    for hl in highlights:
        assert hl.get("risk_reason"), "highlight must carry a risk_reason"
        assert hl.get("provenance_id") and hl.get("entry_id"), "highlight missing provenance"
        body = db.execute("SELECT body FROM entry WHERE id = %s", (hl["entry_id"],)).fetchone()[0]
        assert body[hl["offset_start"] : hl["offset_end"]] == hl["quoted_text"], (
            "highlight provenance pointer is dangling"
        )
