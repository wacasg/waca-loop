"""Improvement tasks (課題化) — store + derive tasks from a survey response.

A response can spawn several tasks: harmful answer, potential incident, NPS detractor. Each is
linked (via 特定) to a concrete target when possible, else flagged for manual triage.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from waca_loop.models import (
    ImprovementTask,
    SurveyResponse,
    TaskKind,
    Viewer,
    nps_band,
)
from waca_loop.retrieval.acl import STEWARD_UNIT

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings
    from waca_loop.improvement.identify import Identified


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _task_to_row(t: ImprovementTask, task_id: str, created_at: datetime) -> dict:
    return {
        "task_id": task_id,
        "created_at": created_at.isoformat(),
        "updated_at": created_at.isoformat(),
        "source_response_id": t.source_response_id,
        "kind": t.kind.value,
        "evidence": t.evidence,
        "target_kind": t.target_kind,
        "target_id": t.target_id,
        "status": t.status.value,
        "priority": t.priority,
        "owner_unit": t.owner_unit,
    }


def make_tasks_from_response(
    response: SurveyResponse,
    response_id: str,
    *,
    identify: Callable[[str, str | None], Identified],
) -> list[ImprovementTask]:
    """Derive improvement tasks from one response.

    ``identify(text, explicit_log_id)`` performs 特定 and returns an :class:`Identified`.
    Priorities: incident (3) > harmful (2) > NPS detractor (1).
    """
    tasks: list[ImprovementTask] = []

    # Fail closed HERE, not at the one caller that happens to resolve a unit (§3.3).
    # `SurveyResponse.owner_unit` still defaults to None, so a future caller that forgets to
    # stamp it would silently produce org-wide tasks. The
    # stewards are the right fallback: it is where an un-routable report already goes
    # (`_survey_owner_unit`), and it is narrow. Never None, never `org`.
    owner_unit = response.owner_unit or STEWARD_UNIT

    if response.incident_flag:
        ident = identify(response.incident_summary or response.harmful_summary, response.ref_log_id)
        tasks.append(
            ImprovementTask(
                kind=TaskKind.INCIDENT,
                source_response_id=response_id,
                evidence=response.incident_summary or "(incident flagged, no summary)",
                target_kind=ident.target_kind,
                target_id=ident.target_id,
                priority=3,
                owner_unit=owner_unit,
            )
        )

    if response.harmful_summary and response.harmful_summary.strip():
        ident = identify(response.harmful_summary, response.ref_log_id)
        tasks.append(
            ImprovementTask(
                kind=TaskKind.HARMFUL,
                source_response_id=response_id,
                evidence=response.harmful_summary,
                target_kind=ident.target_kind,
                target_id=ident.target_id,
                priority=2,
                # ALWAYS the response's owning unit — never None (§3.3).
                #
                # `submit_survey_response` returns `owner_unit` and the tool description
                # orders the model to tell the reporter that this IS who will see it. If the
                # non-sensitive branch left the owner empty, that promise would be false: an
                # unowned task is org-internal to `task_visible_to`, so a reporter told "your
                # department will see this" would have their harmful_summary published
                # verbatim to every member — including anyone the report was about.
                #
                # So the unit is stamped on every derived task. Non-sensitive improvement
                # tasks are not an org-wide feed — that cost is accepted, because a delivery
                # promise that is not kept is the worse failure.
                owner_unit=owner_unit,
            )
        )

    if nps_band(response.nps_score) == "detractor":
        tasks.append(
            ImprovementTask(
                kind=TaskKind.NPS_DETRACTOR,
                source_response_id=response_id,
                evidence=response.nps_reason or f"NPS={response.nps_score}",
                priority=1,
                # Same rule as above — the promised audience is the actual one (§3.3).
                owner_unit=owner_unit,
            )
        )

    return tasks


class InMemoryTaskStore:
    """In-memory improvement-task store (tests / offline)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, task: ImprovementTask) -> str:
        task_id = str(uuid.uuid4())
        self.rows.append(_task_to_row(task, task_id, _utcnow()))
        return task_id

    def list(
        self,
        *,
        viewer: Viewer,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        """List tasks the ``viewer`` may see (§3.3). ``viewer`` is REQUIRED, not defaulted.

        A default of None would mean "no gate at all" — and because that switches off BOTH
        the SQL WHERE and the in-process re-check, one forgetful caller would collapse the
        whole double enforcement at once, silently and fail-OPEN. A default that
        disables a permission check is the wrong shape for the safe path to be the easy one.
        """
        out = self.rows
        if status:
            out = [r for r in out if r["status"] == status]
        if kind:
            out = [r for r in out if r["kind"] == kind]
        from waca_loop.improvement.acl import task_visible_to

        return [r for r in out if task_visible_to(r, viewer)]


class BigQueryTaskStore:
    """Production improvement-task store backed by BigQuery."""

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def add(self, task: ImprovementTask) -> str:
        task_id = str(uuid.uuid4())
        row = _task_to_row(task, task_id, _utcnow())
        errors = self._client.insert_rows_json(self._settings.improvement_tasks_table, [row])
        if errors:
            raise RuntimeError(f"improvement_tasks insert failed: {errors}")
        return task_id

    def list(
        self,
        *,
        viewer: Viewer,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        """List tasks the ``viewer`` may see (§3.3). ``viewer`` is REQUIRED, not defaulted.

        A default of None would mean "no gate at all" — and because that switches off BOTH
        the SQL WHERE and the in-process re-check, one forgetful caller would collapse the
        whole double enforcement at once, silently and fail-OPEN. A default that
        disables a permission check is the wrong shape for the safe path to be the easy one.
        """
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params

        clauses, params = [], []
        if status:
            clauses.append("status = @status")
            params.append(bigquery.ScalarQueryParameter("status", "STRING", status))
        if kind:
            clauses.append("kind = @kind")
            params.append(bigquery.ScalarQueryParameter("kind", "STRING", kind))
        from waca_loop.improvement.acl import task_acl_where_clause

        acl_sql, acl_params = task_acl_where_clause(viewer)
        clauses.append(acl_sql)
        params.extend(scalar_params(acl_params))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT * FROM `{self._settings.improvement_tasks_table}`{where} "
            "ORDER BY priority DESC, created_at DESC"
        )
        job = self._client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
        return [dict(row) for row in job.result()]
