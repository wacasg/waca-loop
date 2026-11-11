"""ACL for improvement tasks (§3.3 applied to the feedback surface).

Improvement tasks derive from survey feedback; incident-kind tasks carry sensitive content, so
`list_improvement_tasks` must not return everything to everyone. Tasks don't carry the full 3-axis
scope tags that grounding rows do, so the rule is
narrower but follows the same shape — and, like §3.3, it is enforced TWICE: an in-process
:func:`task_visible_to` and a SQL :func:`task_acl_where_clause` kept in lockstep.

Rule:
- The list is internal governance, so a non-org-member sees nothing.
- A task is unit-restricted when it is ``incident`` kind **or** carries an ``owner_unit``:
  visible only to viewers in that unit (or holding an explicit grant for it), and fail-closed
  when ``owner_unit`` is None. Keying on kind alone is not enough — a single submission with
  ``incident_flag=True`` also derives ``harmful`` / ``nps_detractor`` tasks whose ``evidence``
  is the same text, and those would go out org-wide through the "other kinds" branch.
  ``make_tasks_from_response`` stamps the owner on EVERY task it derives — not just the
  sensitive ones — and this gate honors it.
- Everything else — a task with no owner — is org-internal. Nothing derived from a survey
  response lands here: the reporter is TOLD the owning unit when they submit, so
  publishing their words wider than that breaks a promise the tool already made. This branch
  only covers tasks filed by some future non-survey path.
- A **steward sees every task**, whatever its owner. Stamping the owner on all derived tasks
  would otherwise leave no one with a view of the backlog as a whole; the overview is the
  governance role's actual job, and it is a handful of people, not an audience.
"""

from __future__ import annotations

from waca_loop.models import TaskKind, Viewer
from waca_loop.retrieval.acl import STEWARD_UNIT

_INCIDENT = TaskKind.INCIDENT.value


def task_visible_to(row: dict, viewer: Viewer) -> bool:
    """True iff ``viewer`` may see this improvement-task row (in-process gate)."""
    if not viewer.is_org_member:
        return False
    # 横断視界. Stamping the owner on EVERY derived task closes the org-wide leak, but it
    # would also leave NOBODY able to see the improvement backlog as a whole — the governance
    # role included. A steward's job is exactly that overview (SPEC_phase2 §2.4
    # 承認は人＝スチュワード), and it is a small role, not an audience. So the reach is
    # granted HERE, narrowly, instead of by loosening the owner rule to org-wide.
    #
    # Note what this does NOT do: it does not widen `retrieval/acl.py`. Grounding rows, SFAQ
    # and persons stay on the 3-axis gate. This is the improvement backlog only.
    if STEWARD_UNIT in viewer.units:
        return True
    owner_unit = row.get("owner_unit") or None   # '' と None を同じに扱う（SQL 側の NULLIF と対）
    if row.get("kind") == _INCIDENT or owner_unit:
        if not owner_unit:
            return False
        return owner_unit in viewer.units or owner_unit in viewer.allowed_sensitive_units
    return True


def task_acl_where_clause(viewer: Viewer, *, alias: str | None = None) -> tuple[str, dict]:
    """SQL WHERE fragment mirroring :func:`task_visible_to` (forbidden rows never leave BQ).

    Returns ``(sql_fragment, params)``. Same rule as the in-process gate — change one, change
    the other. ``params`` must be bound as query parameters by the caller.
    """
    def col(name: str) -> str:
        return f"{alias}.{name}" if alias else name

    units = sorted(viewer.units | viewer.allowed_sensitive_units)
    params = {
        "task_is_org_member": viewer.is_org_member,
        "task_units": units,
        # 横断視界 — mirrors the steward short-circuit in `task_visible_to`. Bound as a
        # parameter (not spliced) so the two gates cannot drift on WHO counts as a steward.
        "task_is_steward": STEWARD_UNIT in viewer.units,
    }
    # NULLIF(owner_unit, '') so an empty string means "unowned" on both sides — without it
    # the in-process gate (falsy '') and this one ('' IS NOT NULL) disagree.
    owner = f"NULLIF({col('owner_unit')}, '')"
    sql = (
        "(@task_is_org_member AND (@task_is_steward "
        f"OR ({col('kind')} != 'incident' AND {owner} IS NULL) "
        f"OR ({owner} IS NOT NULL AND {owner} IN UNNEST(@task_units))))"
    )
    return sql, params
