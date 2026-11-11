"""SFAQ ACL — the 3-axis scope gate PLUS the E-3 permission gate (SPEC_phase2 §2.3).

Same double-enforcement design as every other store (§3.3 "二重で効かせる"):
  1. :func:`question_visible_to` — in-process re-check on full rows.
  2. :func:`question_where_clause` — SQL WHERE fragment; forbidden rows never leave BQ.

E-3 permission gate (AND with the scope axes — either failing hides the row):
  provenance=corporate_training → perm_corporate must be true
  provenance=individual         → perm_individual must be true
  provenance=other              → perm_individual must be true (fail closed)

E-3(ii): when corporate_training AND perm_corporate AND NOT perm_individual, the row
is served inside the corporate scope but the author identity is masked —
:func:`mask_author` implements that at serve time (visibility is unchanged).
"""

from __future__ import annotations

from waca_loop.models import (
    Provenance,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Viewer,
)
from waca_loop.retrieval.acl import visible_to

# Statuses that may be served at all. candidate = raw auto-filed, not yet shaped —
# never delivered; provisional/approved/contested are served with their status shown
# (暫定も status を付けて出す。係争中は明示する。SPEC_phase2 §2.4).
SERVABLE_STATUSES = ("provisional", "approved", "contested")


def _perm_ok(row: dict) -> bool:
    prov = row.get("provenance")
    if prov == Provenance.CORPORATE_TRAINING.value:
        return bool(row.get("perm_corporate"))
    # individual and anything unknown: the person's own permission is required (fail closed).
    return bool(row.get("perm_individual"))


def question_visible_to(row: dict, viewer: Viewer) -> bool:
    """True iff ``viewer`` may see this question row. AND of scope axes × E-3 perms."""
    if row.get("status") not in SERVABLE_STATUSES:
        return False
    scope = ScopeTags(
        tier=ScopeTier(row["scope_tier"]),
        audience=ScopeAudience(row["scope_audience"]),
        sensitivity=ScopeSensitivity(row["scope_sensitivity"]),
    )
    return visible_to(scope, row.get("owner_unit"), viewer) and _perm_ok(row)


def mask_author(row: dict) -> bool:
    """E-3(ii): corporate-permitted but individually unpermitted → serve anonymously."""
    return (
        row.get("provenance") == Provenance.CORPORATE_TRAINING.value
        and bool(row.get("perm_corporate"))
        and not bool(row.get("perm_individual"))
    )


def question_where_clause(viewer: Viewer, *, alias: str = "q") -> tuple[str, dict]:
    """SQL twin of :func:`question_visible_to` (Gate 1). Parameterized, no WHERE prefix."""
    from waca_loop.retrieval.acl import acl_where_clause

    a = alias
    scope_sql, params = acl_where_clause(viewer, alias=alias)
    perm_sql = (
        f"(CASE {a}.provenance "
        f"WHEN 'corporate_training' THEN IFNULL({a}.perm_corporate, FALSE) "
        f"ELSE {a}.perm_individual END)"
    )
    status_sql = f"{a}.status IN ('provisional', 'approved', 'contested')"
    return f"({scope_sql}) AND {perm_sql} AND {status_sql}", params
