"""PersonalCoach pure logic."""

from __future__ import annotations

from dataclasses import fields

from waca_loop.models import Viewer
from waca_loop.policy import (
    AdoptionRow,
    CoachRequest,
    PersonalCoach,
    PolicyFloor,
    SafeChannel,
)
from waca_loop.policy.memory import InMemoryAdoptionMapStore, InMemoryPolicyGuideStore


def test_personal_coach_prioritizes_person_intent_and_marks_not_for_evaluation() -> None:
    coach = PersonalCoach(
        policy_guides=InMemoryPolicyGuideStore(),
        adoption_map=InMemoryAdoptionMapStore([
            AdoptionRow(
                row_id="unit",
                scope_unit="kyozai",
                actor_scope=None,
                business="部署共通告知",
                current_level=1,
                target_level=2,
                share_scope="unit",
            ),
            AdoptionRow(
                row_id="person",
                scope_unit="kyozai",
                actor_scope="person:p1",
                business="教材レビュー",
                current_level=1,
                target_level=2,
                next_step="観点リストをAIで下書きする",
                person_intent={"try_next": "レビュー観点の整理"},
                share_scope="person",
            ),
        ]),
    )

    out = coach.recommend(CoachRequest(viewer=_viewer("p1"), max_items=1)).as_dict()

    assert out["not_for_evaluation"] is True
    assert out["target_person_id"] == "p1"
    assert out["recommendations"][0]["source_adoption_row_ids"] == ["person"]
    assert "本人が試したい" in out["recommendations"][0]["reason"]


def test_personal_coach_does_not_show_private_person_row_to_another_person() -> None:
    coach = PersonalCoach(
        policy_guides=InMemoryPolicyGuideStore(),
        adoption_map=InMemoryAdoptionMapStore([
            AdoptionRow(
                row_id="private",
                scope_unit="kyozai",
                actor_scope="person:p1",
                business="個人の改善希望",
                current_level=1,
                target_level=2,
                person_intent={"wanted_work": "問い合わせ返信"},
                share_scope="person",
            )
        ]),
    )

    out = coach.recommend(CoachRequest(viewer=_viewer("p2"))).as_dict()

    assert out["recommendations"] == []


def test_personal_coach_recommends_l0_only_with_a_safe_path() -> None:
    coach = PersonalCoach(
        policy_guides=InMemoryPolicyGuideStore(
            floors=[
                PolicyFloor(
                    row_id="FLOOR-01",
                    line="生の個人情報を安全でないツールに入力しない",
                    effect="仮名化して安全チャネルへ",
                )
            ],
            safe_channels=[
                SafeChannel(
                    channel_id="waca_loop",
                    scope_unit="kyozai",
                    status="approved",
                    required_controls=["仮名化", "人が確認"],
                )
            ],
        ),
        adoption_map=InMemoryAdoptionMapStore([
            AdoptionRow(
                row_id="blocked",
                scope_unit="kyozai",
                actor_scope="person:p1",
                business="名簿分析",
                current_level=0,
                target_level=1,
                share_scope="person",
            ),
            AdoptionRow(
                row_id="safe",
                scope_unit="kyozai",
                actor_scope="person:p1",
                business="アンケート傾向整理",
                current_level=0,
                target_level=1,
                safe_practice="自由記述を匿名化・集計化して扱う",
                next_step="10件だけ匿名化して要約を試す",
                share_scope="person",
            ),
        ]),
    )

    out = coach.recommend(CoachRequest(viewer=_viewer("p1"), max_items=5)).as_dict()
    ids = [r["source_adoption_row_ids"][0] for r in out["recommendations"]]

    assert ids == ["safe", "blocked"]
    blocked = out["recommendations"][1]
    assert blocked["source_policy_row_ids"] == ["FLOOR-01"]
    assert blocked["required_controls"] == ["仮名化", "人が確認"]
    assert blocked["detour"] == "安全チャネルで必要な統制を満たして実行する"


def test_suspended_channel_row_is_not_recommended() -> None:
    coach = PersonalCoach(
        policy_guides=InMemoryPolicyGuideStore(
            safe_channels=[
                SafeChannel(channel_id="waca_loop", scope_unit="kyozai", status="suspended")
            ],
        ),
        adoption_map=InMemoryAdoptionMapStore([
            AdoptionRow(
                row_id="row",
                scope_unit="kyozai",
                actor_scope="person:p1",
                business="名簿分析",
                current_level=1,
                target_level=2,
                share_scope="person",
            )
        ]),
    )

    out = coach.recommend(CoachRequest(viewer=_viewer("p1"))).as_dict()

    assert out["recommendations"] == []


def test_floors_match_trigger_kinds_not_only_level_zero() -> None:
    coach = PersonalCoach(
        policy_guides=InMemoryPolicyGuideStore(
            floors=[
                PolicyFloor(
                    row_id="FLOOR-PII",
                    line="生PIIを入れない",
                    effect="仮名化する",
                    trigger_kinds=["pii"],
                )
            ],
        ),
        adoption_map=InMemoryAdoptionMapStore([
            AdoptionRow(
                row_id="row",
                scope_unit="kyozai",
                actor_scope="person:p1",
                business="PIIを含むアンケート傾向分析",
                current_level=1,
                target_level=2,
                share_scope="person",
            )
        ]),
    )

    out = coach.recommend(CoachRequest(viewer=_viewer("p1"))).as_dict()

    assert out["recommendations"][0]["source_policy_row_ids"] == ["FLOOR-PII"]


def test_personal_coach_response_models_have_no_raw_prompt_fields() -> None:
    forbidden = {"prompt", "raw_text", "raw_prompt", "transcript", "verbatim"}
    from waca_loop.policy import CoachRecommendation, CoachRequest, CoachResponse

    for cls in (CoachRequest, CoachRecommendation, CoachResponse):
        assert {f.name for f in fields(cls)}.isdisjoint(forbidden)


def _viewer(person_id: str) -> Viewer:
    return Viewer(
        subject=f"sub:{person_id}",
        person_id=person_id,
        is_org_member=True,
        units=frozenset({"kyozai"}),
    )
