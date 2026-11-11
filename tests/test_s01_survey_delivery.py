"""S1 — 配信: 対象者に未回答キャンペーンが提示され、回答後は none（スキップ可）."""

from __future__ import annotations


def test_pending_survey_offered_then_cleared(survey_context):
    pending = survey_context.get_pending_survey()
    assert "campaign_id" in pending
    q_ids = {q["id"] for q in pending["questions"]}
    assert {"nps", "nps_reason", "useful", "harmful", "incident_flag"} <= q_ids

    survey_context.submit_survey_response(
        nps_score=9, nps_reason="便利", useful="用語解決", harmful="", incident_flag=False
    )
    after = survey_context.get_pending_survey()
    assert after.get("none") is True  # already answered this period


def test_survey_skippable_when_not_configured(context):
    # A context without a survey store simply reports none (delivery is optional/skippable).
    assert context.get_pending_survey()["none"] is True
