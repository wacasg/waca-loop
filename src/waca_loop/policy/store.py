"""Policy store (SPEC_phase1 §9) — InMemory + BigQuery pair with an identical surface.

``applicable(viewer)`` returns the policy rows the viewer may be served (org floor + their dept
rows, servable statuses only). ACL is enforced twice via :mod:`waca_loop.policy.acl`. BigQuery
is lazy-imported inside the method so importing this module never pulls in cloud deps (CLAUDE.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from waca_loop.models import PolicyRule, Viewer
from waca_loop.policy.acl import policy_visible_to, policy_where_clause

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings


def _policy_to_row(p: PolicyRule) -> dict:
    return {
        "row_id": p.row_id,
        "scope_unit": p.scope_unit,
        "level": p.level,
        "line": p.line,
        "effect": p.effect,
        "is_floor": p.is_floor,
        "status": p.status,
        "version": p.version,
        "trigger_kinds": list(p.trigger_kinds),
    }


def _row_to_policy(row: dict) -> PolicyRule:
    level = row.get("level")
    return PolicyRule(
        row_id=row["row_id"],
        scope_unit=row["scope_unit"],
        level=int(level) if level is not None else None,
        line=row["line"],
        effect=row.get("effect") or "",
        is_floor=bool(row.get("is_floor", False)),
        status=row.get("status", "approved"),
        version=int(row.get("version", 1)),
        trigger_kinds=list(row.get("trigger_kinds") or []),
    )


class InMemoryPolicyStore:
    """In-memory policy store (tests / offline). Same surface as the BigQuery store."""

    def __init__(self, rules: list[PolicyRule] | None = None) -> None:
        self._rules: list[PolicyRule] = list(rules or [])

    def add(self, rule: PolicyRule) -> None:
        self._rules.append(rule)

    def applicable(self, viewer: Viewer) -> list[PolicyRule]:
        return [r for r in self._rules if policy_visible_to(_policy_to_row(r), viewer)]


class BigQueryPolicyStore:
    """Production policy store backed by BigQuery (§9.1). ACL WHERE clause applied in-SQL (§3.3)."""

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def applicable(self, viewer: Viewer) -> list[PolicyRule]:
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params

        where, params = policy_where_clause(viewer, alias="p")
        sql = (
            "SELECT row_id, scope_unit, level, line, effect, is_floor, status, version, "
            "trigger_kinds "
            f"FROM `{self._settings.policy_table}` AS p WHERE {where}"
        )
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=scalar_params(params)),
        )
        rows: list[dict[str, Any]] = [dict(r) for r in job.result()]
        return [_row_to_policy(r) for r in rows]
