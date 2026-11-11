"""Policy knowledge models for Lite (SPEC_lite_policy_knowledge_store_v1).

Docs are the source of guides, floors, and safe-channel conditions. These models
hold normalized summaries and pointers only; they do not copy raw policy documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from waca_loop.models import PolicyRule


def _org_unit() -> str:
    from waca_loop.retrieval.acl import ORG_UNIT

    return ORG_UNIT


@dataclass(frozen=True, kw_only=True)
class PolicyGuide:
    """A Docs-derived guide row (SPEC_lite_policy_knowledge_store_v1 §3.1)."""

    guide_id: str
    source_id: str
    source_type: str  # design_philosophy | internal_policy | external_policy | member_policy
    title: str
    summary: str
    status: str = "trial"  # draft | trial | approved | retired
    version: int = 1
    effective_at: datetime | None = None
    review_frequency: str | None = None
    source_ref: str = ""


@dataclass(frozen=True, kw_only=True)
class PolicyFloor:
    """A common floor row, shaped so it can become ``PolicyRule(is_floor=True)``."""

    row_id: str
    line: str
    effect: str
    trigger_kinds: list[str] = field(default_factory=list)
    source_ref: str = ""
    status: str = "approved"
    version: int = 1

    def to_policy_rule(self, *, scope_unit: str | None = None) -> PolicyRule:
        """Return the existing policy model used by current policy delivery."""
        return PolicyRule(
            row_id=self.row_id,
            scope_unit=scope_unit or _org_unit(),
            line=self.line,
            effect=self.effect,
            level=None,
            is_floor=True,
            status=self.status,
            version=self.version,
            trigger_kinds=list(self.trigger_kinds),
        )


@dataclass(frozen=True, kw_only=True)
class SafeChannel:
    """Whether WACA Loop or another AI environment may handle a data kind safely."""

    channel_id: str
    scope_unit: str
    status: str = "unreviewed"  # unreviewed | trial | approved | suspended
    allowed_data_kinds: list[str] = field(default_factory=list)
    required_controls: list[str] = field(default_factory=list)
    approved_by_role: str | None = None
    approved_at: datetime | None = None
    source_ref: str | None = None


@dataclass(frozen=True, kw_only=True)
class RefreshResult:
    """Result of re-reading a source document or sheet."""

    source_id: str
    changed: bool
    success: bool
    message: str
    refreshed_at: datetime
    source_version: str | None = None
    checksum: str | None = None
