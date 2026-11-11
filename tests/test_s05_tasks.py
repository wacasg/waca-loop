"""S5 — 課題化: 特定できた harmful/incident に improvement_task が起票される."""

from __future__ import annotations


def test_harmful_creates_linked_task(survey_context):
    out = survey_context.submit_survey_response(
        nps_score=8,
        nps_reason="おおむね良い",
        useful="",
        harmful="サンプル協会の定義をAIが誤って説明した",
        incident_flag=False,
    )
    kinds = {t["kind"] for t in out["tasks"]}
    assert "harmful" in kinds
    harmful_task = next(t for t in out["tasks"] if t["kind"] == "harmful")
    assert harmful_task["target_kind"] == "term"
    assert harmful_task["target_id"] == "t_org"
    assert out["identified"] is True


def test_incident_is_high_priority(survey_context):
    out = survey_context.submit_survey_response(
        nps_score=2,
        nps_reason="不安",
        useful="",
        harmful="",
        incident_flag=True,
        incident_summary="個人情報が回答に紛れた",
    )
    by_kind = {t["kind"]: t for t in out["tasks"]}
    assert "incident" in by_kind
    assert "nps_detractor" in by_kind                      # NPS=2 -> detractor
    assert by_kind["incident"]["priority"] > by_kind["nps_detractor"]["priority"]


def test_promoter_with_no_issues_creates_no_tasks(survey_context):
    out = survey_context.submit_survey_response(
        nps_score=10, nps_reason="最高", useful="色々", harmful="", incident_flag=False
    )
    assert out["tasks"] == []
    assert out["identified"] is False
