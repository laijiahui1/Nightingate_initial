"""Redaction completeness battery (M5 §1).

Pure-Python tests against ``app.services.redaction`` — no database. The suite
proves the No-PHI chokepoint at the LLM boundary: every realistic PHI shape
(name, honourific, label, NRIC/FIN, ID, phone) collapses to a deterministic
``[REDACTED:<CATEGORY>:<N>]`` token, ``restore`` is a true round-trip, the mask
is metadata-only, and the logging scrubber emits nothing a downstream log sink
could exfiltrate.

Every parameterized case asserts three things at once:
  1. the correct category token is emitted (NAME vs NRIC vs PHONE, not conflated),
  2. the emitted categories are EXACTLY the expected category (no stray category),
  3. ``contains_phi`` is False on the redacted output (zero PHI survives).
"""

from __future__ import annotations

import logging

import pytest

from app.services.redaction import (
    PHI_CATEGORIES,
    contains_phi,
    redact,
    redaction_mask,
    restore,
    scrub,
)

FULL_NAMES = [
    "Alice Tan",
    "Bala Kumar",
    "Mei Ling Chua",
    "Priya Nair",
    "Marcus Lee",
    "Sandra Ho",
    "David Ong",
    "Amelia Wong",
    "Jason Lim",
]

FIRST_NAMES = [
    "Alice",
    "Bala",
    "Mei",
    "Priya",
    "Marcus",
    "Sandra",
    "David",
    "Amelia",
    "Jason",
]

HONORIFIC_CASES = [
    "Dr. Marcus Lee",
    "Dr Marcus Lee",
    "Mr Bala Kumar",
    "Mdm Mei Ling Chua",
    "Uncle Jason",
    # Multi-word Malay/Malay-adjacent names (up to 3 capitalized words).
    "Dr. Fatimah Binte Abdullah",
    "Mr Tan Ah Kow",
    # The pattern must stop at the next sentence word, not swallow it.
    "Dr. Marcus Lee walked in",
]

LABELLED_CASES = [
    "Patient: Alice Tan",
    "Name: Alice Tan",
    "Nurse: Sandra Ho",
    "Doctor: David Ong",
    # Label stops at the name — must not swallow the following sentence.
    "Patient: Alice Tan said something",
]

NRIC_FIN_CASES = [
    "S9123456D",
    "T1234567A",
    "G9876543B",
    "800101-14-5678",
]

ID_NUMBER_CASES = [
    "E12345678",
    "123456789",
]

PHONE_CASES = [
    "+65 9123 4567",
    "+65-8123-4567",
    "91234567",
    "81234567",
    "61234567",
    "+60 12 345 6789",
    # Split 4+4 with a space or hyphen, first group starting 6/8/9.
    "9123 4567",
    "8123-4567",
    "9455 1234",
]

# (text, name_substring) — the name is embedded at a boundary / adjacency point.
BOUNDARY_CASES = [
    ("Alice Tan called the clinic.", "Alice Tan"),       # name at string start
    ("The patient is Alice Tan", "Alice Tan"),           # name at string end
    ("Alice Tan.", "Alice Tan"),                          # followed by "."
    ("Alice Tan,", "Alice Tan"),                          # followed by ","
    ("Alice Tan;", "Alice Tan"),                          # followed by ";"
    ("Ask Priya to follow up.", "Priya"),                 # informal first-name reference
    ("alice tan", "alice tan"),                           # lowercase (IGNORECASE master)
]

NO_PHI_BODIES = [
    "The patient reports no new symptoms.",
    "Blood pressure is stable at 120/80.",
    "Follow up in four weeks with the care team.",
    # Date-looking 4+4 that must NOT be treated as a phone (starts with 2).
    "Recorded on 2026 0826.",
    # Clinical measures must never be redacted.
    "BP 148/92",
]


def _assert_redacted(text: str, category: str) -> str:
    """Redact ``text`` and assert the expected category token + zero PHI."""
    out, mapping = redact(text)
    token = f"[REDACTED:{category}:0]"
    assert token in out, f"{text!r} did not emit {category} token: {out!r}"
    emitted = {entry["category"] for entry in mapping.values()}
    assert emitted == {category}, f"{text!r} emitted unexpected categories {emitted!r}"
    assert not contains_phi(out), f"{text!r} still contains PHI after redaction: {out!r}"
    return out


# ---------------------------------------------------------------------------
# Parameterized batteries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FULL_NAMES)
def test_lexicon_full_name_redacts(name: str):
    """Every seeded full name redacts to a NAME token with no PHI left behind."""
    _assert_redacted(name, "NAME")


@pytest.mark.parametrize("name", FIRST_NAMES)
def test_lexicon_first_name_redacts(name: str):
    """Every seeded first name (informal reference) redacts to a NAME token."""
    _assert_redacted(name, "NAME")


@pytest.mark.parametrize("text", HONORIFIC_CASES)
def test_honorific_led_name_redacts(text: str):
    """Honourific-led names ("Dr. Marcus Lee", "Mdm Mei Ling Chua") redact."""
    out = _assert_redacted(text, "NAME")
    # The honourific itself must never survive as a standalone token.
    assert "Dr" not in out and "Mdm" not in out and "Mr" not in out and "Uncle" not in out


@pytest.mark.parametrize("text", LABELLED_CASES)
def test_labelled_name_redacts(text: str):
    """Labelled names ("Patient: Alice Tan", "Nurse: Sandra Ho") redact."""
    _assert_redacted(text, "NAME")


@pytest.mark.parametrize("text", NRIC_FIN_CASES)
def test_nric_fin_redacts(text: str):
    """SG NRIC/FIN and MY NRIC redact to an NRIC token."""
    _assert_redacted(text, "NRIC")


@pytest.mark.parametrize("text", ID_NUMBER_CASES)
def test_id_number_redacts(text: str):
    """Passport-style and generic 9-digit IDs redact to an ID_NUMBER token."""
    _assert_redacted(text, "ID_NUMBER")


@pytest.mark.parametrize("text", PHONE_CASES)
def test_phone_redacts(text: str):
    """SG/MY phone formats (mobile + landline) redact to a PHONE token."""
    _assert_redacted(text, "PHONE")


@pytest.mark.parametrize("text,name_substring", BOUNDARY_CASES)
def test_boundary_and_adjacency_redacts(text: str, name_substring: str):
    """Names at start/end, followed by punctuation, or lowercase still redact."""
    out = redact(text)[0]
    assert "[REDACTED:NAME:0]" in out, f"{text!r} did not redact its name: {out!r}"
    assert name_substring not in out.lower(), f"{text!r} leaked its name: {out!r}"
    assert not contains_phi(out), f"{text!r} still contains PHI: {out!r}"


@pytest.mark.parametrize("body", NO_PHI_BODIES)
def test_no_phi_passthrough(body: str):
    """A body with zero PHI is returned unchanged and yields an empty mapping."""
    out, mapping = redact(body)
    assert out == body, f"no-PHI body was modified: {out!r}"
    assert mapping == {}, f"no-PHI body produced a non-empty mapping: {mapping!r}"


# ---------------------------------------------------------------------------
# Invariants (not parameterized)
# ---------------------------------------------------------------------------


def test_contains_phi_detects_all_seeded_cases():
    """contains_phi is True on every PHI-bearing battery input (pre-redaction)."""
    phony = (
        FULL_NAMES + FIRST_NAMES + HONORIFIC_CASES + LABELLED_CASES
        + NRIC_FIN_CASES + ID_NUMBER_CASES + PHONE_CASES
        + [text for text, _ in BOUNDARY_CASES]
    )
    for text in phony:
        assert contains_phi(text), f"contains_phi missed {text!r}"


def test_determinism_identical_text_and_mapping():
    """redact() on identical input yields identical text AND identical tokens."""
    text = "Alice Tan (S9123456D) called +65 9123 4567 about Priya Nair."
    out1, map1 = redact(text)
    out2, map2 = redact(text)
    assert out1 == out2, "redacted text is not deterministic"
    assert map1 == map2, "redaction mapping is not deterministic"


def test_token_uniqueness_within_category():
    """N distinct NAME instances produce N distinct tokens (0..N-1)."""
    text = "Alice Tan and Bala Kumar and Priya Nair"
    out, mapping = redact(text)
    tokens = sorted(t for t in mapping if t.startswith("[REDACTED:NAME:"))
    assert tokens == [
        "[REDACTED:NAME:0]",
        "[REDACTED:NAME:1]",
        "[REDACTED:NAME:2]",
    ], f"unexpected NAME tokens: {tokens!r} ({out!r})"


def test_restore_roundtrip():
    """restore(redact(t)[0], mapping) == t for a mixed-PHI note."""
    text = "Alice Tan, S9123456D, +65 9123 4567, Priya Nair."
    out, mapping = redact(text)
    assert restore(out, mapping) == text, "restore did not round-trip the original"


def test_redaction_mask_is_metadata_only():
    """redaction_mask yields {category, count} only — never PHI or spans."""
    text = "Alice Tan, S9123456D, +65 9123 4567, Priya Nair"
    _, mapping = redact(text)
    mask = redaction_mask(mapping)
    for item in mask:
        assert set(item.keys()) == {"category", "count"}, f"mask item leaked fields: {item!r}"
        assert item["category"] in PHI_CATEGORIES
        assert isinstance(item["count"], int) and item["count"] >= 1
    by_cat = {item["category"]: item["count"] for item in mask}
    assert by_cat == {"NAME": 2, "NRIC": 1, "PHONE": 1}, f"unexpected mask: {mask!r}"
    # No original value or span may appear anywhere in the serialized mask.
    mask_text = repr(mask)
    for original in ("Alice Tan", "Priya Nair", "S9123456D", "+65 9123 4567"):
        assert original not in mask_text, f"mask leaked original value {original!r}"


def test_category_isolation_name_vs_phone():
    """A name fixture emits NAME (never PHONE); a phone fixture emits PHONE."""
    name_cats = {e["category"] for e in redact("Alice Tan")[1].values()}
    assert name_cats == {"NAME"}
    phone_cats = {e["category"] for e in redact("+65 9123 4567")[1].values()}
    assert phone_cats == {"PHONE"}


def test_scrub_discards_mapping():
    """scrub() returns only the redacted form (mapping discarded)."""
    scrubbed = scrub("Alice Tan +65 9123 4567")
    assert "[REDACTED:NAME:0]" in scrubbed
    assert "[REDACTED:PHONE:0]" in scrubbed
    assert not contains_phi(scrubbed)


def test_log_scrubber_filter_emits_phi_free():
    """The main.py logging filter (reusing scrub) strips a name + NRIC + phone."""
    # Imported lazily so this module never binds the app DB engine at collection
    # time (app.main imports app.db.session, which builds the engine from the
    # current DATABASE_URL).
    from app.main import PHIScrubbingFilter

    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Contact Alice Tan at +65 9123 4567, NRIC S9123456D",
        args=(),
        exc_info=None,
    )
    ph_filter = PHIScrubbingFilter()
    assert ph_filter.filter(record) is True
    scrubbed = record.msg
    for phi in ("Alice Tan", "+65 9123 4567", "S9123456D"):
        assert phi not in scrubbed, f"log scrubber leaked {phi!r}: {scrubbed!r}"
    assert "[REDACTED:NAME:0]" in scrubbed
    assert "[REDACTED:NRIC:0]" in scrubbed
    assert "[REDACTED:PHONE:0]" in scrubbed
    assert not contains_phi(scrubbed)
