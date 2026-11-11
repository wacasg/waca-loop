"""Survey response store — InMemory + BigQuery, same surface.

Summary-only: like the ledger (§2.2③/T6), responses carry summaries, never verbatim prompts.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from waca_loop.models import ScopeSensitivity, SurveyResponse

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _response_to_row(r: SurveyResponse, response_id: str, occurred_at: datetime) -> dict:
    return {
        "response_id": response_id,
        "occurred_at": occurred_at.isoformat(),
        "campaign_id": r.campaign_id,
        "respondent_token": r.respondent_token,
        "respondent_id": r.respondent_id,
        "nps_score": r.nps_score,
        "nps_reason": r.nps_reason,
        "useful_summary": r.useful_summary,
        "harmful_summary": r.harmful_summary,
        "incident_flag": r.incident_flag,
        "incident_summary": r.incident_summary,
        "contact_opt_in": r.contact_opt_in,
        "ref_log_id": r.ref_log_id,
        "ref_request_id": r.ref_request_id,
        "scope_sensitivity": r.sensitivity.value,
        "owner_unit": r.owner_unit,
        # Escalation (§3.3): keep the 迂回 intent so a steward cannot mistake owner_unit for
        # "unresolved" and bounce the report back to the reporter's own unit. NULL in BQ
        # means "no intent recorded" — readers must not read it as false.
        "escalated": r.escalated,
        "created_at": occurred_at.isoformat(),
    }


class InMemorySurveyStore:
    """In-memory survey store (tests / offline)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, response: SurveyResponse, *, occurred_at: datetime | None = None) -> str:
        response_id = str(uuid.uuid4())
        self.rows.append(_response_to_row(response, response_id, occurred_at or _utcnow()))
        return response_id

    def responded_tokens(self, campaign_id: str) -> set[str]:
        return {r["respondent_token"] for r in self.rows if r["campaign_id"] == campaign_id}

    def has_responded(self, token: str, campaign_id: str) -> bool:
        return token in self.responded_tokens(campaign_id)


class BigQuerySurveyStore:
    """Production survey store backed by BigQuery."""

    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def record(self, response: SurveyResponse, *, occurred_at: datetime | None = None) -> str:
        response_id = str(uuid.uuid4())
        row = _response_to_row(response, response_id, occurred_at or _utcnow())
        errors = self._client.insert_rows_json(self._settings.survey_responses_table, [row])
        if errors:
            raise RuntimeError(f"survey_responses insert failed: {errors}")
        return response_id

    def responded_tokens(self, campaign_id: str) -> set[str]:
        from google.cloud import bigquery

        sql = (
            f"SELECT DISTINCT respondent_token FROM `{self._settings.survey_responses_table}` "
            "WHERE campaign_id = @cid"
        )
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("cid", "STRING", campaign_id)]
            ),
        )
        return {row["respondent_token"] for row in job.result()}

    def has_responded(self, token: str, campaign_id: str) -> bool:
        return token in self.responded_tokens(campaign_id)


def incident_sensitivity(incident_flag: bool, escalated: bool = False) -> ScopeSensitivity:
    """Incident reports default to sensitive scope (§3.3).

    ``escalated`` counts too: asking to route a report past your own department IS the
    statement that it is sensitive, whether or not the model also set ``incident_flag``.
    Without this, an escalated report with ``incident_flag=False`` would be routed to the
    stewards but tagged ``normal``, and its derived tasks would go out org-wide carrying the
    accusation verbatim — the opposite of what the reporter was promised.
    """
    return (
        ScopeSensitivity.SENSITIVE
        if (incident_flag or escalated)
        else ScopeSensitivity.NORMAL
    )
