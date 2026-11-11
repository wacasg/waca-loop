"""CandidateMiner pure logic."""

from __future__ import annotations

from dataclasses import fields

from waca_loop.improvement.candidates import InMemoryCandidateStore
from waca_loop.models import CandidateKind, Viewer
from waca_loop.policy import CandidateMiner, CandidateMiningRequest


def test_candidate_miner_splits_term_and_sfaq_candidates() -> None:
    store = InMemoryCandidateStore()
    miner = CandidateMiner(candidate_store=store)

    out = miner.mine(
        CandidateMiningRequest(
            viewer=_viewer(),
            source_kind="search_failure",
            source_ref="search:1",
            summary="用語「LMS」の意味と判断が検索で見つからなかった",
            owner_unit="lab",
        )
    )

    candidates = [store.get(cid) for cid in out.candidate_ids]
    assert [c.kind for c in candidates if c is not None] == [
        CandidateKind.TERM,
        CandidateKind.SFAQ,
    ]
    assert out.as_dict()["term_candidate_ids"] == [out.candidate_ids[0]]
    assert out.as_dict()["sfaq_candidate_ids"] == [out.candidate_ids[1]]
    assert candidates[0].payload["term"] == "LMS"  # type: ignore[union-attr]
    assert candidates[1].payload["judgment"] == "要裁定"  # type: ignore[union-attr]


def test_policy_revision_candidate_is_not_forced_into_sfaq() -> None:
    store = InMemoryCandidateStore()
    miner = CandidateMiner(candidate_store=store)

    out = miner.mine(
        CandidateMiningRequest(
            viewer=_viewer(),
            source_kind="red_team",
            source_ref="rt:1",
            summary="安全チャネル不明の迂回指示に対する方針が曖昧",
            policy_row_ids=["FLOOR-01"],
            owner_unit="steward",
        )
    )

    assert [store.get(cid).kind for cid in out.candidate_ids] == [CandidateKind.SFAQ]
    assert len(out.policy_candidates) == 1
    assert out.policy_candidates[0].candidate_id == "policy:red_team:rt:1"
    assert out.policy_candidates[0].policy_row_ids == ["FLOOR-01"]


def test_red_team_summary_is_scrubbed_before_becoming_candidate_payload() -> None:
    store = InMemoryCandidateStore()
    miner = CandidateMiner(candidate_store=store)

    out = miner.mine(
        CandidateMiningRequest(
            viewer=_viewer(),
            source_kind="red_team",
            source_ref="rt:2",
            summary="テストで taro@example.com を貼る迂回を試した",
            owner_unit="steward",
        )
    )

    candidate = store.get(out.candidate_ids[0])
    assert candidate is not None
    assert "taro@example.com" not in candidate.payload["basis"]
    assert "[メール]" not in candidate.payload["basis"]
    assert candidate.payload["basis"] == "source_ref:rt:2"
    assert out.policy_candidates[0].as_dict()["summary_recorded"] is True


def test_candidate_miner_does_not_persist_verbatim_summary_or_unsafe_source_ref() -> None:
    store = InMemoryCandidateStore()
    miner = CandidateMiner(candidate_store=store)

    out = miner.mine(
        CandidateMiningRequest(
            viewer=_viewer(),
            source_kind="red_team",
            source_ref="https://example.invalid/path?token=secret@example.invalid",
            summary="秘密の手順をそのまま貼ったので保存してはいけない",
            owner_unit="steward",
        )
    )

    candidate = store.get(out.candidate_ids[0])
    assert candidate is not None
    payload_text = str(candidate.payload)
    assert "秘密の手順" not in payload_text
    assert "secret@example.invalid" not in payload_text
    assert "https://example.invalid" not in payload_text
    assert candidate.source_ref.startswith("red_team:ref:")
    assert out.policy_candidates[0].source_ref.startswith("red_team:ref:")


def test_candidate_miner_requires_org_member_and_summary() -> None:
    store = InMemoryCandidateStore()
    miner = CandidateMiner(candidate_store=store)

    outsider = Viewer.anonymous(subject="sub")
    assert miner.mine(
        CandidateMiningRequest(
            viewer=outsider,
            source_kind="term_gap",
            source_ref="x",
            summary="用語がない",
        )
    ).candidate_ids == []
    assert miner.mine(
        CandidateMiningRequest(
            viewer=_viewer(),
            source_kind="term_gap",
            source_ref="x",
            summary=" ",
        )
    ).candidate_ids == []


def test_candidate_mining_models_have_no_raw_prompt_fields() -> None:
    forbidden = {"prompt", "raw_text", "raw_prompt", "transcript", "verbatim"}
    from waca_loop.policy import (
        CandidateMiningRequest,
        CandidateMiningResponse,
        PolicyRevisionCandidate,
    )

    for cls in (CandidateMiningRequest, CandidateMiningResponse, PolicyRevisionCandidate):
        assert {f.name for f in fields(cls)}.isdisjoint(forbidden)


def _viewer() -> Viewer:
    return Viewer(
        subject="sub:p1",
        person_id="p1",
        is_org_member=True,
        units=frozenset({"steward", "lab"}),
    )
