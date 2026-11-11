"""S2 — 収集: submit で response 行が作成（NPS/理由/有用/有害/incident）."""

from __future__ import annotations


def test_submit_records_response(survey_context):
    out = survey_context.submit_survey_response(
        nps_score=3,
        nps_reason="誤読が多い",
        useful="議事録要約",
        harmful="固有名詞を誤った",
        incident_flag=False,
    )
    assert out["response_id"]

    rows = survey_context.survey_store.rows
    assert len(rows) == 1
    row = rows[0]
    assert row["nps_score"] == 3
    assert row["nps_reason"] == "誤読が多い"
    assert row["useful_summary"] == "議事録要約"
    assert row["harmful_summary"] == "固有名詞を誤った"
    assert row["incident_flag"] is False
    assert row["campaign_id"]
    assert row["occurred_at"]


def test_pseudonymous_by_default(survey_context):
    survey_context.submit_survey_response(
        nps_score=10, nps_reason="", useful="", harmful="", incident_flag=False
    )
    row = survey_context.survey_store.rows[0]
    # Default attribution is pseudonymous: a hashed token, no person id.
    assert row["respondent_token"].startswith("sub_")
    assert row["respondent_id"] is None
