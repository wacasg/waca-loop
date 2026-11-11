"""4D output signature (§6) — system-prompt rules + output wrapping helpers."""

from waca_loop.signature.prompt import (
    DELEGATION_AI,
    DELEGATION_HUMAN,
    SIGNATURE_LINE,
    SYSTEM_PROMPT_RULES,
    delegation_tag,
    has_signature,
    signature_line,
    wrap_output,
)

__all__ = [
    "DELEGATION_AI",
    "DELEGATION_HUMAN",
    "SIGNATURE_LINE",
    "SYSTEM_PROMPT_RULES",
    "delegation_tag",
    "has_signature",
    "signature_line",
    "wrap_output",
]
