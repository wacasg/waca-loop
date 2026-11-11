"""ACL for policy rows (§3.3 applied to the Policy layer, SPEC_phase1 §9).

Policy rows are org-scoped governance guidance. Like §3.3 it is enforced **twice** — an in-process
:func:`policy_visible_to` and a SQL :func:`policy_where_clause` kept in lockstep (change one, change
the other). Only *servable* statuses are delivered: ``approved`` and ``trial``. ``draft`` is
withheld — but ``trial`` MUST be served so trial rows accrue usage before an org decides to adopt
them (計器盤, SPEC_phase1 §9).

Rule:
- Policy rows are internal governance: a non-org-member sees nothing (fail closed — an
  authenticated partner must not receive the org's internal bright-lines).
- ``scope_unit == ORG_UNIT`` (org.toml [units] org) is the org-wide floor: visible to every
  org member.
- Otherwise visible iff the viewer belongs to that unit (``scope_unit in viewer.units``).
"""

from __future__ import annotations

from waca_loop.models import Viewer
from waca_loop.retrieval.acl import ORG_UNIT

_SERVABLE_STATUSES = ("approved", "trial")


def policy_visible_to(row: dict, viewer: Viewer) -> bool:
    """True iff ``viewer`` may be served this policy row (in-process gate)."""
    if not viewer.is_org_member:
        return False
    if row.get("status") not in _SERVABLE_STATUSES:
        return False
    scope_unit = row.get("scope_unit")
    if scope_unit == ORG_UNIT:
        return True
    return scope_unit in viewer.units


def policy_where_clause(viewer: Viewer, *, alias: str | None = None) -> tuple[str, dict]:
    """SQL WHERE fragment mirroring :func:`policy_visible_to` (forbidden rows never leave BQ)."""
    def col(name: str) -> str:
        return f"{alias}.{name}" if alias else name

    params = {
        "policy_is_org_member": viewer.is_org_member,
        "policy_statuses": list(_SERVABLE_STATUSES),
        "policy_units": sorted(viewer.units),
        "policy_org_unit": ORG_UNIT,
    }
    sql = (
        f"(@policy_is_org_member AND {col('status')} IN UNNEST(@policy_statuses) AND ("
        f"{col('scope_unit')} = @policy_org_unit "
        f"OR {col('scope_unit')} IN UNNEST(@policy_units)))"
    )
    return sql, params
