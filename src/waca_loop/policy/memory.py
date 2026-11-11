"""In-memory Lite policy/adoption stores.

These stores are the offline twin for future Sheets/BigQuery backends. They keep
backend details out of coach/candidate logic and make ACL behavior testable first.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from waca_loop.models import Viewer
from waca_loop.policy.adoption import AdoptionRow
from waca_loop.policy.knowledge import PolicyFloor, PolicyGuide, RefreshResult, SafeChannel
from waca_loop.retrieval.acl import ORG_UNIT, STEWARD_UNIT

_SERVABLE_GUIDE_STATUSES = frozenset({"trial", "approved"})
_SERVABLE_CHANNEL_STATUSES = frozenset({"trial", "approved", "suspended"})
_SERVABLE_ADOPTION_STATUSES = frozenset({"active", "trial", "approved"})
_STEWARD_UNIT = STEWARD_UNIT
_ORG_UNIT = ORG_UNIT


def _is_steward(viewer: Viewer) -> bool:
    return _STEWARD_UNIT in viewer.units


def _servable_status(status: str) -> bool:
    return status in _SERVABLE_GUIDE_STATUSES


def _as_served_guide(guide: PolicyGuide, now: datetime) -> PolicyGuide:
    if guide.status == "approved" and guide.effective_at is not None and guide.effective_at > now:
        return replace(guide, status="trial")
    return guide


def _can_read_managed_person_rows(viewer: Viewer, target_unit: str) -> bool:
    _ = target_unit
    return _is_steward(viewer)


def _unit_visible(scope_unit: str, viewer: Viewer) -> bool:
    if not viewer.is_org_member:
        return False
    if scope_unit == _ORG_UNIT:
        return True
    return _is_steward(viewer) or scope_unit in viewer.units


def _person_key(person_id: str | None) -> set[str]:
    if not person_id:
        return set()
    return {person_id, f"person:{person_id}"}


def _actor_matches(row: AdoptionRow, viewer: Viewer) -> bool:
    return row.actor_scope is not None and row.actor_scope in _person_key(viewer.person_id)


def _adoption_visible_to_person(row: AdoptionRow, viewer: Viewer) -> bool:
    if row.status not in _SERVABLE_ADOPTION_STATUSES:
        return False
    if not _unit_visible(row.scope_unit, viewer):
        return False
    if row.actor_scope is None:
        return row.share_scope in {"unit", "org", "manager", "admin"}
    return _actor_matches(row, viewer)


def _adoption_visible_to_unit(row: AdoptionRow, viewer: Viewer, scope_unit: str | None) -> bool:
    if row.status not in _SERVABLE_ADOPTION_STATUSES:
        return False
    target_unit = scope_unit or row.scope_unit
    if row.scope_unit != target_unit:
        return False
    if not _unit_visible(target_unit, viewer):
        return False
    if row.actor_scope is None:
        return row.share_scope in {"unit", "org", "manager", "admin"}
    if _actor_matches(row, viewer):
        return True
    if row.share_scope == "unit":
        return _is_steward(viewer) or target_unit in viewer.units
    if row.share_scope in {"manager", "admin"}:
        return _can_read_managed_person_rows(viewer, target_unit)
    return False


class InMemoryPolicyGuideStore:
    """Docs-derived guides/floors/safe-channel status for tests and local wiring."""

    def __init__(
        self,
        *,
        guides: list[PolicyGuide] | None = None,
        floors: list[PolicyFloor] | None = None,
        safe_channels: list[SafeChannel] | None = None,
    ) -> None:
        self._guides = list(guides or [])
        self._floors = list(floors or [])
        self._safe_channels = list(safe_channels or [])

    def guides_for(self, viewer: Viewer) -> list[PolicyGuide]:
        if not viewer.is_org_member:
            return []
        now = datetime.now(UTC)
        return [_as_served_guide(g, now) for g in self._guides if _servable_status(g.status)]

    def floors_for(self, viewer: Viewer) -> list[PolicyFloor]:
        if not viewer.is_org_member:
            return []
        return [f for f in self._floors if f.status in _SERVABLE_GUIDE_STATUSES]

    def safe_channels_for(self, viewer: Viewer) -> list[SafeChannel]:
        return [
            c for c in self._safe_channels
            if c.status in _SERVABLE_CHANNEL_STATUSES and _unit_visible(c.scope_unit, viewer)
        ]

    def refresh_due_sources(self, now: datetime) -> list[str]:
        return []

    def refresh_source(self, source_id: str) -> RefreshResult:
        return RefreshResult(
            source_id=source_id,
            changed=False,
            success=True,
            message="in-memory store has no external source",
            refreshed_at=datetime.now(UTC),
        )


class InMemoryAdoptionMapStore:
    """Sheets-derived adoption rows for tests and local wiring."""

    def __init__(self, rows: list[AdoptionRow] | None = None) -> None:
        self._rows = list(rows or [])

    def rows_for_person(self, viewer: Viewer) -> list[AdoptionRow]:
        if not viewer.is_org_member or viewer.person_id is None:
            return []
        return [r for r in self._rows if _adoption_visible_to_person(r, viewer)]

    def rows_for_unit(self, viewer: Viewer, scope_unit: str | None = None) -> list[AdoptionRow]:
        if not viewer.is_org_member:
            return []
        return [r for r in self._rows if _adoption_visible_to_unit(r, viewer, scope_unit)]

    def refresh_due_sources(self, now: datetime) -> list[str]:
        return []

    def refresh_source(self, source_id: str) -> RefreshResult:
        return RefreshResult(
            source_id=source_id,
            changed=False,
            success=True,
            message="in-memory store has no external source",
            refreshed_at=datetime.now(UTC),
        )
