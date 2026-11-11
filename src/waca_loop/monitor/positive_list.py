"""ポジティブリスト監視 (§7).

Everyone is expected to run the same MCP/hook, so a person who produces no ledger rows
is treated as unconnected / shadow-AI. We detect *silence as anomaly* rather than
patching holes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class MonitorReport:
    window_days: int
    expected: set[str]
    seen: set[str]
    missing: set[str]

    @property
    def coverage(self) -> float:
        if not self.expected:
            return 1.0
        return len(self.expected & self.seen) / len(self.expected)


def positive_list_check(expected_actors: set[str], seen_actors: set[str]) -> set[str]:
    """Pure set difference (§7): expected − seen = missing (unconnected actors)."""
    return set(expected_actors) - set(seen_actors)


class _PersonSource(Protocol):
    def active_actor_ids(self) -> set[str]:
        ...


class _UsageSource(Protocol):
    def distinct_actors(self, window_days: int) -> set[str]:
        ...


def run_positive_list_check(
    persons: _PersonSource,
    usage: _UsageSource,
    *,
    window_days: int,
) -> MonitorReport:
    """Build the report (§7.2): expected from ``persons.status='active'``; seen from the
    distinct actors in ``usage_log`` over the lookback window."""
    expected = persons.active_actor_ids()
    seen = usage.distinct_actors(window_days)
    return MonitorReport(
        window_days=window_days,
        expected=expected,
        seen=seen,
        missing=positive_list_check(expected, seen),
    )
