"""Store Protocols for Lite Policy / Knowledge Store (SPEC_lite_policy_knowledge_store_v1 §4).

These are deliberately tiny and mirror existing store surfaces where possible.
Backend-specific details such as BigQuery SQL or Sheets ranges stay behind the
implementing store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from waca_loop.improvement.candidates import InMemoryCandidateStore
from waca_loop.models import CandidateStatus, ExtractionCandidate, Viewer
from waca_loop.policy.adoption import AdoptionRow
from waca_loop.policy.knowledge import PolicyFloor, PolicyGuide, RefreshResult, SafeChannel


class PolicyGuideStore(Protocol):
    """Docs-derived guides, floors, and safe-channel status."""

    def guides_for(self, viewer: Viewer) -> list[PolicyGuide]: ...
    def floors_for(self, viewer: Viewer) -> list[PolicyFloor]: ...
    def safe_channels_for(self, viewer: Viewer) -> list[SafeChannel]: ...
    def refresh_due_sources(self, now: datetime) -> list[str]: ...
    def refresh_source(self, source_id: str) -> RefreshResult: ...


class AdoptionMapStore(Protocol):
    """Sheets-derived department/person adoption rows."""

    def rows_for_person(self, viewer: Viewer) -> list[AdoptionRow]: ...
    def rows_for_unit(self, viewer: Viewer, scope_unit: str | None = None) -> list[AdoptionRow]: ...
    def refresh_due_sources(self, now: datetime) -> list[str]: ...
    def refresh_source(self, source_id: str) -> RefreshResult: ...


class KnowledgeCandidateStore(Protocol):
    """Candidate store surface shared by InMemory and BigQuery implementations."""

    def add(self, candidate: ExtractionCandidate) -> str: ...
    def get(self, candidate_id: str) -> ExtractionCandidate | None: ...
    def list(self, *, status: str | None = None) -> list[ExtractionCandidate]: ...
    def set_status(self, candidate_id: str, status: CandidateStatus) -> None: ...


def assert_candidate_store_surface(store: KnowledgeCandidateStore) -> KnowledgeCandidateStore:
    """Tiny runtime hook used by tests and wiring to catch surface drift early."""
    return store


__all__ = [
    "AdoptionMapStore",
    "InMemoryCandidateStore",
    "KnowledgeCandidateStore",
    "PolicyGuideStore",
    "assert_candidate_store_surface",
]
