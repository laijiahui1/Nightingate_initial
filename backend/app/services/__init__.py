"""Service layer: PHI redaction and the single LLM gateway chokepoint.

Design invariants (docs/ARCHITECTURE.md §6, docs/SECURITY.md §b):

1. ``redaction`` is deterministic (regex + lexicon), runs locally, and is the
   ONLY PHI-cleaning mechanism — no LLM is ever used to redact.
2. ``llm_gateway.chat()`` is the ONLY code path to any LLM. It redacts every
   message before the model is called and never logs PHI.
3. ``LLM_MOCK=1`` (the default) routes every call to a deterministic mock
   keyed by the prompt hash, so tests/demos never depend on a live model.

Re-export the two most common entry points for convenient imports:
``from app.services import redact, chat``.
"""

from app.services.redaction import (
    Redactor,
    contains_phi,
    redact,
    redaction_mask,
    restore,
    scrub,
)
from app.services.llm_gateway import LLMGatewayError, build_prompt, chat

__all__ = [
    "Redactor",
    "contains_phi",
    "redact",
    "redaction_mask",
    "restore",
    "scrub",
    "LLMGatewayError",
    "build_prompt",
    "chat",
]
