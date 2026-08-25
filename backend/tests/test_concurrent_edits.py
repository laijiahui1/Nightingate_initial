"""Concurrent-edits micro-tests (RED skeleton).

docs/PLAN.md Phase 6 + docs/DATA_SCHEMA.md §7:

- two roles editing DIFFERENT sections concurrently must NOT overwrite each
  other — each section is its own ``entry`` row, rows lock independently, and
  both edits persist;
- a SAME-section race resolves DETERMINISTICALLY (server-total-order
  last-writer-wins): the winner's content stays current, the loser is preserved
  verbatim as a ``conflict_flag=true`` child version (never dropped), and the
  conflict is surfaced (audit_log ``conflict_flagged``).

The edit/version endpoints do not exist yet, so the requests 404 and the tests
fail — the RED state. Remove the xfail markers when the Phase 6 routes land.
"""

import pytest


@pytest.mark.asyncio
async def test_different_sections_do_not_clobber_each_other(
    staff_client, clinician_client, db, entries
):
    """Concurrent edits to DIFFERENT sections both survive (no lost update)."""
    staff_entry = entries["e6_lab_handoff"]  # section='staff_handoff', authored by staff
    clin_entry = entries["e9_review"]        # section='plan', authored by clinician

    staff_body_v1 = db.execute(
        "SELECT body FROM entry WHERE id = %s", (staff_entry,)
    ).fetchone()[0]
    clin_body_v1 = db.execute(
        "SELECT body FROM entry WHERE id = %s", (clin_entry,)
    ).fetchone()[0]

    # Both edits are issued "concurrently" — different sections, independent rows.
    staff_resp = await staff_client.put(
        f"/api/entries/{staff_entry}",
        json={"body": staff_body_v1 + "\n- Staff: chased lab, awaiting result.", "base_version": 1},
    )
    clin_resp = await clinician_client.put(
        f"/api/entries/{clin_entry}",
        json={"body": clin_body_v1 + "\n- Cardiology referral scheduled.", "base_version": 1},
    )
    assert staff_resp.status_code == 200, "staff edit failed"
    assert clin_resp.status_code == 200, "clinician edit failed"

    # Neither edit overwrote the other.
    staff_body_v2 = db.execute(
        "SELECT body FROM entry WHERE id = %s", (staff_entry,)
    ).fetchone()[0]
    clin_body_v2 = db.execute(
        "SELECT body FROM entry WHERE id = %s", (clin_entry,)
    ).fetchone()[0]
    assert "- Staff: chased lab" in staff_body_v2, "staff edit was lost"
    assert "- Cardiology referral" in clin_body_v2, "clinician edit was lost"


@pytest.mark.asyncio
async def test_same_section_conflict_resolves_deterministically(
    clinician_client, db, entries, role_conn
):
    """A same-section race resolves deterministically and preserves the loser."""
    entry_id = entries["e3_care_plan"]  # clinician section='plan', current version 2
    winner_body = (
        "1. Amlodipine 5 mg OD.\n"
        "2. Ambulatory BP monitoring.\n"
        "3. ECG at next visit.\n"
        "4. Review in 4 weeks.\n"
        "5. Added 24h Holter monitoring."
    )
    loser_body = (
        "1. Amlodipine 5 mg OD.\n"
        "2. Ambulatory BP monitoring.\n"
        "3. ECG at next visit.\n"
        "4. Refer to cardiology TODAY."
    )

    # Both writers present the SAME base_version (a lost update is inevitable).
    first = await clinician_client.put(
        f"/api/entries/{entry_id}",
        json={"body": winner_body, "base_version": 2},
    )
    second = await clinician_client.put(
        f"/api/entries/{entry_id}",
        json={"body": loser_body, "base_version": 2},
    )
    assert first.status_code == 200, "first same-section edit failed"
    assert second.status_code == 200, "second same-section edit was not accepted"

    # Deterministic resolution: the first commit (later server revision) wins the
    # row; the second writer is preserved as a flagged child version.
    row = db.execute("SELECT version, body FROM entry WHERE id = %s", (entry_id,)).fetchone()
    assert row[0] == 3, "exactly one clean winner should have advanced the version"
    assert row[1] == winner_body, "deterministic winner was not applied"

    flagged = db.execute(
        "SELECT body, conflict_flag, conflict_of FROM entry_version "
        "WHERE entry_id = %s AND conflict_flag = true ORDER BY version DESC LIMIT 1",
        (entry_id,),
    ).fetchone()
    assert flagged is not None, "conflicting edit was dropped instead of preserved"
    assert flagged[0] == loser_body, "loser content must be preserved verbatim"
    assert flagged[2] is not None, "conflict child must point at the winner version"

    # The conflict must be flagged in the audit log for human review.
    with role_conn("admin") as conn:
        n = conn.execute(
            "SELECT count(*) FROM audit_log WHERE action = 'conflict_flagged' AND target_id = %s",
            (entry_id,),
        ).fetchone()[0]
        assert n == 1, "conflict was not flagged in the audit log"
