"""Revision-history micro-tests (RED skeleton).

docs/SECURITY.md §g + docs/PLAN.md Phase 6:

- editing an entry increments its ``version`` and appends a FULL snapshot to
  ``entry_version``;
- reverting restores a prior snapshot as a NEW version (never destructive —
  history stays linear and immutable);
- the audit log records who/what/when with METADATA ONLY (no note content).

The revision/version endpoints do not exist yet, so every request 404s and the
tests fail — the RED state. Remove the xfail markers when the Phase 6 routes
land (then these become the GREEN contract).
"""

import pytest


@pytest.mark.asyncio
async def test_edit_increments_version(clinician_client, db, entries):
    """A successful edit bumps entry.version and writes a full snapshot."""
    entry_id = entries["e3_care_plan"]  # clinician-authored care plan, current version 2
    new_body = "1. Amlodipine 5 mg OD.\n2. Ambulatory BP monitoring.\n3. ECG.\n4. Review."

    resp = await clinician_client.put(
        f"/api/entries/{entry_id}",
        json={"body": new_body, "base_version": 2},
    )
    assert resp.status_code == 200, "clinician edit of own entry failed"

    row = db.execute("SELECT version, body FROM entry WHERE id = %s", (entry_id,)).fetchone()
    assert row[0] == 3, "edit did not increment the entry version"
    assert row[1] == new_body, "edit did not persist the new body"

    snap = db.execute(
        "SELECT body FROM entry_version WHERE entry_id = %s AND version = 3",
        (entry_id,),
    ).fetchone()
    assert snap is not None and snap[0] == new_body, "full snapshot for the new version is missing"


@pytest.mark.asyncio
async def test_revert_restores_prior_state(clinician_client, db, entries):
    """Revert restores a prior snapshot as a NEW version (non-destructive)."""
    entry_id = entries["e3_care_plan"]

    v1_body = db.execute(
        "SELECT body FROM entry_version WHERE entry_id = %s AND version = 1",
        (entry_id,),
    ).fetchone()[0]
    version_before = db.execute(
        "SELECT version FROM entry WHERE id = %s", (entry_id,)
    ).fetchone()[0]

    resp = await clinician_client.post(
        f"/api/entries/{entry_id}/revert",
        json={"target_version": 1},
    )
    assert resp.status_code == 200, "revert request failed"

    row = db.execute("SELECT version, body FROM entry WHERE id = %s", (entry_id,)).fetchone()
    assert row[0] == version_before + 1, "revert did not create a new version"
    assert row[1] == v1_body, "revert did not restore the prior state"


@pytest.mark.asyncio
async def test_audit_log_shows_who_changed_what_metadata_only(
    clinician_client, admin_client, admin_conn, entries
):
    """Audit rows carry actor/action/version metadata and NEVER note content."""
    # (1) Schema guarantee (always true): audit_log has no content columns.
    cols = admin_conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'audit_log'"
    ).fetchall()
    col_names = {c[0] for c in cols}
    assert not {"body", "content", "text", "note"} & col_names, "audit_log schema stores note content"

    entry_id = entries["e3_care_plan"]
    resp = await clinician_client.put(
        f"/api/entries/{entry_id}",
        json={"body": "v3 audit test", "base_version": 2},
    )
    assert resp.status_code == 200, "edit for the audit test failed"

    # (2) API enforcement: admin-only audit endpoint returns who/what/when.
    resp = await admin_client.get(f"/api/audit?target_type=entry&target_id={entry_id}")
    assert resp.status_code == 200
    rows = resp.json().get("rows", [])
    assert any(
        r.get("action") == "update"
        and r.get("actor_role") == "clinician"
        and r.get("version") is not None
        for r in rows
    ), "audit log is missing the who/what metadata for the edit"
    for r in rows:
        assert "body" not in r and "content" not in r, "audit row leaked note content"
