"""S7 — 督促: 期限内未回答者を missing として検出（monitor 再利用）."""

from __future__ import annotations

from datetime import UTC, datetime

from waca_loop.grounding.persons import hash_subject
from waca_loop.survey.scheduler import current_campaign_id, due_respondents
from waca_loop.survey.store import InMemorySurveyStore


def test_campaign_id_formats():
    now = datetime(2026, 6, 28, tzinfo=UTC)
    assert current_campaign_id("monthly", now=now) == "2026-06"
    assert current_campaign_id("quarterly", now=now) == "2026-Q2"


def test_active_non_responders_are_due(person_store):
    survey = InMemorySurveyStore()
    campaign = "2026-06"
    # p_sato (sub-sato) responds; p_tanaka (active) does not; p_frozen is inactive.
    subs = {"sub-sato": "p_sato", "sub-tanaka": "p_tanaka"}
    token_of = {pid: hash_subject(sub) for sub, pid in subs.items()}
    survey.record(_resp(campaign, token_of["p_sato"]))
    due = due_respondents(person_store, survey, campaign_id=campaign, token_of=token_of)
    assert "p_tanaka" in due
    assert "p_sato" not in due
    assert "p_frozen" not in due  # not active -> not expected


def _resp(campaign: str, token: str):
    from waca_loop.models import SurveyResponse

    return SurveyResponse(
        nps_score=9,
        nps_reason="",
        useful_summary="",
        harmful_summary="",
        incident_flag=False,
        campaign_id=campaign,
        respondent_token=token,
    )
