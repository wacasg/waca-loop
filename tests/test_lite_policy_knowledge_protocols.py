"""Lite Policy / Knowledge Store types and Protocols."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime

import pytest

from waca_loop.improvement.candidates import InMemoryCandidateStore
from waca_loop.models import (
    CandidateKind,
    CandidateStatus,
    ExtractionCandidate,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Viewer,
)
from waca_loop.policy import AdoptionRow, PolicyFloor, PolicyGuide, RefreshResult, SafeChannel
from waca_loop.policy.memory import InMemoryAdoptionMapStore, InMemoryPolicyGuideStore
from waca_loop.policy.protocols import assert_candidate_store_surface


def test_policy_floor_maps_to_existing_policy_rule() -> None:
    floor = PolicyFloor(
        row_id="FLOOR-X",
        line="生の個人情報を安全でないツールに入力しない",
        effect="仮名化して安全チャネルへ",
        trigger_kinds=["pii"],
        source_ref="doc://policy#floor",
    )

    rule = floor.to_policy_rule()

    assert rule.row_id == "FLOOR-X"
    assert rule.scope_unit == "org"
    assert rule.level is None
    assert rule.is_floor is True
    assert rule.trigger_kinds == ["pii"]


def test_models_have_no_raw_prompt_or_transcript_fields() -> None:
    forbidden = {"prompt", "raw_text", "raw_prompt", "transcript", "verbatim"}
    model_fields = {
        cls.__name__: {f.name for f in fields(cls)}
        for cls in (PolicyGuide, PolicyFloor, SafeChannel, RefreshResult, AdoptionRow)
    }

    for names in model_fields.values():
        assert names.isdisjoint(forbidden)


def test_adoption_row_rejects_unknown_levels() -> None:
    with pytest.raises(ValueError, match="current_level"):
        AdoptionRow(
            row_id="bad",
            scope_unit="kyozai",
            actor_scope=None,
            business="教材レビュー",
            current_level=3,
        )

    with pytest.raises(ValueError, match="target_level"):
        AdoptionRow(
            row_id="bad-target",
            scope_unit="kyozai",
            actor_scope=None,
            business="教材レビュー",
            current_level=1,
            target_level=-1,
        )


def test_adoption_row_carries_stable_actor_scope_not_raw_identity() -> None:
    row = AdoptionRow(
        row_id="person-1",
        scope_unit="kyozai",
        actor_scope="person:p_123",
        business="教材レビュー",
        current_level=1,
        target_level=2,
        person_intent={"wanted_work": "レビューの観点整理"},
        share_scope="person",
        updated_at=datetime(2026, 8, 28, tzinfo=UTC),
    )

    assert row.actor_scope == "person:p_123"
    assert "@" not in row.actor_scope


def test_existing_candidate_store_matches_knowledge_candidate_protocol() -> None:
    store = assert_candidate_store_surface(InMemoryCandidateStore())
    candidate = ExtractionCandidate(
        kind=CandidateKind.TERM,
        payload={"term": "SFAQ", "definition": "組織内で過去に裁定された質問"},
        proposed_scope=ScopeTags(
            ScopeTier.ORG,
            ScopeAudience.INTERNAL,
            ScopeSensitivity.NORMAL,
        ),
        owner_unit="steward",
    )

    candidate_id = store.add(candidate)
    assert store.get(candidate_id) is candidate
    assert store.list(status=CandidateStatus.PROPOSED.value) == [candidate]

    store.set_status(candidate_id, CandidateStatus.APPROVED)
    assert store.get(candidate_id).status is CandidateStatus.APPROVED  # type: ignore[union-attr]


def test_inmemory_policy_guide_store_serves_org_members_only() -> None:
    store = InMemoryPolicyGuideStore(
        guides=[
            PolicyGuide(
                guide_id="g1",
                source_id="doc1",
                source_type="internal_policy",
                title="AI利用方針",
                summary="安全経路を使う",
                status="trial",
            ),
            PolicyGuide(
                guide_id="draft",
                source_id="doc2",
                source_type="internal_policy",
                title="下書き",
                summary="まだ出さない",
                status="draft",
            ),
        ],
        floors=[
            PolicyFloor(row_id="FLOOR-01", line="生PIIを入れない", effect="仮名化する")
        ],
        safe_channels=[
            SafeChannel(
                channel_id="waca_loop",
                scope_unit="kyozai",
                status="approved",
                allowed_data_kinds=["pseudonymous"],
            )
        ],
    )
    outsider = _viewer("outsider", is_org_member=False)
    member = _viewer("member", units={"members"})
    kyozai = _viewer("kyozai", units={"kyozai"})

    assert store.guides_for(outsider) == []
    assert [g.guide_id for g in store.guides_for(member)] == ["g1"]
    assert [f.row_id for f in store.floors_for(member)] == ["FLOOR-01"]
    assert store.safe_channels_for(member) == []
    assert [c.channel_id for c in store.safe_channels_for(kyozai)] == ["waca_loop"]


def test_inmemory_adoption_rows_are_visible_to_the_person_but_not_other_members() -> None:
    store = InMemoryAdoptionMapStore([
        AdoptionRow(
            row_id="p1-row",
            scope_unit="kyozai",
            actor_scope="person:p1",
            business="教材レビュー",
            current_level=1,
            target_level=2,
            share_scope="person",
        ),
        AdoptionRow(
            row_id="unit-row",
            scope_unit="kyozai",
            actor_scope=None,
            business="部署共通の告知文",
            current_level=2,
            share_scope="unit",
        ),
    ])
    p1 = _viewer("p1", units={"kyozai"})
    p2 = _viewer("p2", units={"kyozai"})
    other_unit = _viewer("p3", units={"secretariat"})

    assert [r.row_id for r in store.rows_for_person(p1)] == ["p1-row", "unit-row"]
    assert [r.row_id for r in store.rows_for_person(p2)] == ["unit-row"]
    assert store.rows_for_person(other_unit) == []


def test_inmemory_adoption_unit_view_respects_share_scope() -> None:
    store = InMemoryAdoptionMapStore([
        AdoptionRow(
            row_id="private",
            scope_unit="kyozai",
            actor_scope="person:p1",
            business="個人メモ",
            current_level=0,
            share_scope="person",
        ),
        AdoptionRow(
            row_id="manager",
            scope_unit="kyozai",
            actor_scope="person:p1",
            business="上長指導対象",
            current_level=1,
            share_scope="manager",
        ),
        AdoptionRow(
            row_id="unit",
            scope_unit="kyozai",
            actor_scope=None,
            business="部署共通",
            current_level=2,
            share_scope="unit",
        ),
    ])
    kyozai_member = _viewer("member", units={"kyozai"})
    other_unit = _viewer("p3", units={"secretariat"})
    steward = _viewer("steward", units={"steward"})

    assert [r.row_id for r in store.rows_for_unit(kyozai_member, "kyozai")] == ["unit"]
    assert store.rows_for_unit(other_unit, "kyozai") == []
    assert [r.row_id for r in store.rows_for_unit(steward, "kyozai")] == [
        "manager",
        "unit",
    ]


def test_inmemory_refresh_is_explicit_noop() -> None:
    store = InMemoryPolicyGuideStore()
    result = store.refresh_source("doc1")

    assert store.refresh_due_sources(datetime(2026, 8, 28, tzinfo=UTC)) == []
    assert result.source_id == "doc1"
    assert result.changed is False
    assert result.success is True


def test_guide_before_effective_at_is_served_as_trial() -> None:
    store = InMemoryPolicyGuideStore(
        guides=[
            PolicyGuide(
                guide_id="future",
                source_id="doc1",
                source_type="internal_policy",
                title="未来発効",
                summary="発効前",
                status="approved",
                effective_at=datetime(2999, 1, 1, tzinfo=UTC),
            )
        ]
    )

    assert store.guides_for(_viewer("p1"))[0].status == "trial"


def _viewer(
    person_id: str,
    *,
    units: set[str] | None = None,
    is_org_member: bool = True,
) -> Viewer:
    return Viewer(
        subject=f"sub:{person_id}",
        person_id=person_id,
        is_org_member=is_org_member,
        units=frozenset(units or set()),
    )
