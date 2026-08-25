"""Deterministic No-PHI redaction pipeline (regex + lexicon).

This is the redaction layer enforced at the LLM boundary (docs/ARCHITECTURE.md
§6, docs/SECURITY.md §b). It is deliberately deterministic and local — an LLM
is NEVER used to redact, because a model would reintroduce the very failure
mode it protects against.

What is redacted (in pattern order, first match at a position wins):

- **NAME** — curated lexicon of the seeded synthetic dataset (full names +
  first names), plus regex heuristics for honorific-led names (``Dr. Marcus
  Lee``) and labelled names (``Patient: Alice Tan``).
- **NRIC** — Singapore NRIC/FIN (``S9123456D``) and Malaysian NRIC
  (``800101-14-5678``).
- **ID_NUMBER** — passport/birth-certificate style (``E12345678``) and a
  generic 9-digit fallback.
- **PHONE** — Singapore (``+65 9123 4567``, ``8xxxxxxx``/``9xxxxxxx``/``6xxxxxxx``),
  Malaysian (``+60 12 345 6789``), and mobile/landline digit runs.

Placeholder scheme (docs/ARCHITECTURE.md §6): each hit becomes a deterministic
token ``[REDACTED:<CATEGORY>:<N>]`` where ``N`` is a per-category counter, so
``redact()`` on identical input always yields identical output. The returned
mapping ``{token -> {category, original, start, end}}`` makes the operation
reversible (``restore()``) and gives provenance/audit the exact spans that were
sent to the model — while never persisting PHI in the mapping anywhere other
than the caller's explicit choice.

Decision note: SECURITY.md §b sketched ``{{PHI:0:NAME}}``; ARCHITECTURE.md §6
(which this module implements) specifies ``[REDACTED:<type>:<n>]``. The
ARCHITECTURE scheme is used here as the more specific implementation contract.

Public API: ``redact``, ``restore``, ``scrub``, ``redaction_mask``,
``contains_phi``, and the ``Redactor`` class (for sharing one token space
across several messages in a single LLM call — see ``llm_gateway``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# Token format emitted for every redacted span.
_TOKEN_PATTERN = re.compile(r"\[REDACTED:[A-Z_]+:\d+\]")

# Categories emitted. Order here is also the detection order inside
# ``_category_of`` (only one named group is ever set per match).
PHI_CATEGORIES: tuple[str, ...] = ("NAME", "NRIC", "ID_NUMBER", "PHONE")

# Curated lexicon of the seeded synthetic dataset (backend/app/seed.py). The
# seed only produces these identities, so exact-match coverage is guaranteed
# for every demo record; the regex heuristics below catch honorific/labelled
# variants beyond the plain full names.
_NAME_LEXICON: tuple[str, ...] = (
    # Full names (patients + staff).
    "Alice Tan",
    "Bala Kumar",
    "Mei Ling Chua",
    "Priya Nair",
    "Marcus Lee",
    "Sandra Ho",
    "David Ong",
    "Amelia Wong",
    "Jason Lim",
    # First names — cover informal references inside transcripts.
    "Alice",
    "Bala",
    "Mei",
    "Priya",
    "Marcus",
    "Sandra",
    "David",
    "Amelia",
    "Jason",
)

# Honorifics that may precede a capitalized name (Singapore/Malaysia context).
_HONORIFICS = r"(?:Mr|Ms|Mrs|Mdm|Dr|Sis|Uncle|Auntie|Prof|Sir|Madam)"


def _lexicon_alternation() -> str:
    """Escaped alternation of lexicon names, longest first (best match at a
    shared start position wins in a single alternation pass)."""
    names = sorted({n.strip() for n in _NAME_LEXICON if n.strip()}, key=len, reverse=True)
    return "|".join(re.escape(n) for n in names)


def _name_pattern() -> str:
    # Name words are matched with inline (?-i:...) so they stay
    # case-sensitive-uppercase even though the master pattern is IGNORECASE.
    # Otherwise every word after an honorific/label would be swallowed as a
    # name (e.g. "Dr. Marcus Lee walked" -> "...Lee walked").
    return (
        # 1. Lexicon exact match, whole-word.
        rf"(?:\b(?:{_lexicon_alternation()})\b)"
        # 2. Honorific-led names, e.g. "Dr. Marcus Lee", "Dr. Fatimah Binte
        #    Abdullah" (up to 3 capitalized words; \b stops at the next word).
        rf"|(?:\b{_HONORIFICS}\.?\s+(?-i:[A-Z][a-z]+)(?:\s+(?-i:[A-Z][a-z]+)){{0,2}}\b)"
        # 3. Labelled names, e.g. "Patient: Alice Tan".
        rf"|(?:\b(?:Name|Patient|Nurse|Doctor)\s*:\s*(?-i:[A-Z][a-z]+)(?:\s+(?-i:[A-Z][a-z]+)){{0,2}}\b)"
    )


def _nric_pattern() -> str:
    return (
        # Singapore NRIC/FIN: prefix letter + 7 digits + checksum letter.
        rf"(?:\b[STFGM]\d{{7}}[A-Za-z]\b)"
        # Malaysian NRIC: 6-2-4 digits (YYYYMMDD-PB-XXXX).
        rf"|(?:\b\d{{6}}-\d{{2}}-\d{{4}}\b)"
    )


def _id_pattern() -> str:
    return (
        # Passport / birth-certificate style: letter + 8 digits.
        rf"(?:\b[A-Z]\d{{8}}\b)"
        # Generic 9-digit identifier fallback.
        rf"|(?:\b\d{{9}}\b)"
    )


def _phone_pattern() -> str:
    return (
        # Singapore international: +65 9123 4567 / +65-8123-4567.
        rf"(?:\+?65[-\s]?\d{{4}}[-\s]?\d{{4}})"
        # Malaysia international: +60 12 345 6789.
        rf"|(?:\+?60[-\s]?\d{{1,2}}[-\s]?\d{{3,4}}[-\s]?\d{{3,4}})"
        # Singapore mobile: 8xxxxxxx / 9xxxxxxx.
        rf"|(?:(?<!\d)(?:8|9)\d{{7}}(?!\d))"
        # Singapore landline: 6xxxxxxx.
        rf"|(?:(?<!\d)6\d{{7}}(?!\d))"
        # Split 4+4 with a space/hyphen, first group starting 6/8/9 (SG
        # mobile/landline written as "9123 4567"). The [689] guard keeps
        # dates/quantities like "2026 0826" or "7981 4523" out.
        rf"|(?:(?<!\d)(?:[689]\d{{3}})[-\s]\d{{4}}(?!\d))"
    )


# One deterministic master pattern with a named group per category. Only one
# named group is ever populated per match (mutually exclusive alternatives), so
# ``finditer`` yields non-overlapping matches in left-to-right order.
_MASTER_PATTERN = re.compile(
    rf"(?P<NAME>{_name_pattern()})"
    rf"|(?P<NRIC>{_nric_pattern()})"
    rf"|(?P<ID_NUMBER>{_id_pattern()})"
    rf"|(?P<PHONE>{_phone_pattern()})",
    re.IGNORECASE,
)


def _category_of(match: re.Match[str]) -> str | None:
    """Return the category name of a master-pattern match, or None."""
    for name in PHI_CATEGORIES:
        if match.group(name) is not None:
            return name
    return None


class Redactor:
    """Shared token-space redactor.

    Use one instance across all messages of a single LLM call so token numbers
    (``[REDACTED:NAME:0]``, ``[REDACTED:NAME:1]``, ...) are globally unique and
    ``restore`` can always resolve them unambiguously.
    """

    def __init__(self) -> None:
        self.mapping: dict[str, dict[str, object]] = {}
        self._counters: dict[str, int] = {}

    def redact(self, text: str) -> str:
        """Return ``text`` with every PHI span replaced by a deterministic token."""
        pieces: list[str] = []
        cursor = 0
        for match in _MASTER_PATTERN.finditer(text):
            category = _category_of(match)
            if category is None:  # defensive; every named group is a category
                continue
            start, end = match.span()
            pieces.append(text[cursor:start])
            n = self._counters.get(category, 0)
            token = f"[REDACTED:{category}:{n}]"
            self._counters[category] = n + 1
            self.mapping[token] = {
                "category": category,
                "original": match.group(0),
                "start": start,
                "end": end,
            }
            pieces.append(token)
            cursor = end
        pieces.append(text[cursor:])
        return "".join(pieces)

    def restore(self, text: str) -> str:
        """Reverse ``redact`` for ``text`` using this instance's mapping.

        Unknown tokens (not in the mapping) are left untouched rather than
        silently replaced — an unrecognized placeholder is flagged, per
        docs/ARCHITECTURE.md §6.
        """

        def _replace(m: re.Match[str]) -> str:
            entry = self.mapping.get(m.group(0))
            return str(entry["original"]) if entry else m.group(0)

        return _TOKEN_PATTERN.sub(_replace, text)

    @property
    def mask(self) -> list[dict[str, object]]:
        """Per-category counts (metadata only — never the PHI itself)."""
        return redaction_mask(self.mapping)


def redact(text: str) -> tuple[str, dict[str, dict[str, object]]]:
    """Redact PHI from ``text``.

    Returns ``(redacted_text, mapping)`` where ``mapping`` is
    ``{token: {category, original, start, end}}``. Deterministic: identical
    input produces identical tokens and mapping. ``restore(redact(t)[0], mapping)
    == t`` for any input that does not already contain a redaction token.
    """
    redactor = Redactor()
    return redactor.redact(text), redactor.mapping


def restore(
    redacted_text: str, mapping: Mapping[str, dict[str, object]] | dict[str, dict[str, object]]
) -> str:
    """Restore a redacted string from its mapping (deterministic reverse)."""
    if not mapping:
        return redacted_text

    def _replace(m: re.Match[str]) -> str:
        entry = mapping.get(m.group(0))
        return str(entry["original"]) if entry else m.group(0)

    return _TOKEN_PATTERN.sub(_replace, redacted_text)


def scrub(text: str) -> str:
    """Return only the redacted form (mapping discarded).

    This is the form that is safe for logging (docs/SECURITY.md §d): even a
    developer mistake that embeds a PHI pattern is scrubbed before the line is
    written.
    """
    return redact(text)[0]


def redaction_mask(
    mapping: Mapping[str, dict[str, object]] | dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    """Summarize a mapping as ``[{category, count}]`` metadata (never PHI).

    Stored in ``ai_scribed_note.redaction_mask`` to prove the pipeline ran
    without persisting the redacted values themselves.
    """
    counts: dict[str, int] = {}
    for entry in mapping.values():
        category = str(entry["category"])
        counts[category] = counts.get(category, 0) + 1
    return [{"category": category, "count": counts[category]} for category in sorted(counts)]


def contains_phi(text: str) -> bool:
    """True when any known PHI pattern is present in ``text``.

    Used by the redaction tests to assert the outgoing payload contains zero
    name/NRIC/phone patterns, and by the seed generator to self-verify every
    fixture body redacts cleanly.
    """
    return _MASTER_PATTERN.search(text) is not None


__all__ = [
    "Redactor",
    "PHI_CATEGORIES",
    "_NAME_LEXICON",
    "contains_phi",
    "redact",
    "redaction_mask",
    "restore",
    "scrub",
]
