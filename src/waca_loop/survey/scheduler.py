"""Survey scheduling — who is due, and which campaign period applies.

Cadence is monthly by default (admin/user adjustable). Due detection reuses the
positive-list set-difference idea (§7): active people who haven't responded this period.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


def current_campaign_id(cadence: str = "monthly", *, now: datetime | None = None) -> str:
    """Stable id for the current survey period (e.g. ``2026-06`` monthly, ``2026-Q2`` quarterly)."""
    now = now or datetime.now(UTC)
    cadence = (cadence or "monthly").lower()
    if cadence == "quarterly":
        return f"{now.year}-Q{(now.month - 1) // 3 + 1}"
    if cadence == "weekly":
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return f"{now.year}-{now.month:02d}"  # monthly default


class _PersonSource(Protocol):
    def active_actor_ids(self) -> set[str]:
        ...


class _ResponseSource(Protocol):
    def responded_tokens(self, campaign_id: str) -> set[str]:
        ...


def due_respondents(
    persons: _PersonSource,
    survey: _ResponseSource,
    *,
    campaign_id: str,
    token_of: dict[str, str] | None = None,
) -> set[str]:
    """Active people (`persons.status='active'`) who have not responded to ``campaign_id``.

    Identity is pseudonymous, so responses are keyed by ``respondent_token`` while the
    expected set is keyed by ``person_id``; ``token_of`` maps person_id → token to compare.
    Without the map (e.g. identified mode), tokens are assumed to equal person ids.
    """
    expected = persons.active_actor_ids()
    responded = survey.responded_tokens(campaign_id)
    token_of = token_of or {}
    return {pid for pid in expected if token_of.get(pid, pid) not in responded}
