"""Adoption-map models for Lite (SPEC_lite_policy_knowledge_store_v1).

Sheets are the source of department/person adoption state. Rows here are summaries
and stable identifiers only; raw emails, OAuth subjects, prompts, and transcripts do
not belong in this layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, kw_only=True)
class AdoptionRow:
    """A normalized Sheets adoption row (SPEC_lite_policy_knowledge_store_v1 §3.3)."""

    row_id: str
    scope_unit: str
    actor_scope: str | None
    business: str
    current_level: int
    target_level: int | None = None
    current_usage: str = ""
    safe_practice: str = ""
    issue_summary: str | None = None
    next_step: str | None = None
    owner_label: str | None = None
    manager_guidance: str | None = None
    person_intent: dict[str, str] = field(default_factory=dict)
    share_scope: str = "private"
    updated_at: datetime | None = None
    status: str = "active"
    source_ref: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        if self.current_level not in (0, 1, 2):
            raise ValueError("current_level must be 0, 1, or 2")
        if self.target_level is not None and self.target_level not in (0, 1, 2):
            raise ValueError("target_level must be None, 0, 1, or 2")
