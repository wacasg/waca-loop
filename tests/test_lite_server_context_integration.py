"""ServerContext integration for Lite policy knowledge features."""

from __future__ import annotations

import pytest

from waca_loop.grounding.terms import InMemoryTermStore
from waca_loop.improvement.candidates import InMemoryCandidateStore
from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.models import CandidateKind, Viewer
from waca_loop.policy import AdoptionRow, PolicyFloor, SafeChannel
from waca_loop.policy.memory import InMemoryAdoptionMapStore, InMemoryPolicyGuideStore
from waca_loop.server import ServerContext


def test_server_context_get_personal_coach_returns_structured_response(term_store, embedder):
    ctx = _ctx(term_store, embedder)

    out = ctx.get_personal_coach(max_items=1)

    assert out["not_for_evaluation"] is True
    assert out["target_person_id"] == "p1"
    assert out["recommendations"][0]["source_adoption_row_ids"] == ["row1"]


def test_server_context_export_personal_recommendations_does_not_write(term_store, embedder):
    ctx = _ctx(term_store, embedder)

    out = ctx.export_personal_recommendations(max_items=1)

    assert out["target_person_id"] == "p1"
    assert set(out["rows"][0]) == {
        "recommendation_id",
        "created_at",
        "target_person_id",
        "not_for_evaluation",
        "title",
        "next_step",
        "safety_path",
        "required_controls",
        "source_policy_row_ids",
        "source_adoption_row_ids",
        "status",
    }


def test_personal_recommendation_outputs_are_clamped(term_store, embedder):
    ctx = _ctx(term_store, embedder)
    ctx.adoption_map_store = InMemoryAdoptionMapStore([
        AdoptionRow(
            row_id=f"row{i}",
            scope_unit="kyozai",
            actor_scope="person:p1",
            business=f"教材レビュー{i}",
            current_level=1,
            target_level=2,
            share_scope="person",
        )
        for i in range(30)
    ])

    assert len(ctx.get_personal_coach(max_items=100_000)["recommendations"]) == 10
    assert len(ctx.export_personal_recommendations(max_items=100_000)["rows"]) == 10


def test_server_context_second_gate_drops_cross_person_recommendation(term_store, embedder):
    class LeakyAdoptionStore:
        def rows_for_person(self, viewer):
            return [
                AdoptionRow(
                    row_id="bob-row",
                    scope_unit="kyozai",
                    actor_scope="person:bob",
                    business="他人の行",
                    current_level=1,
                    target_level=2,
                    share_scope="person",
                )
            ]

    ctx = _ctx(term_store, embedder)
    ctx.adoption_map_store = LeakyAdoptionStore()

    out = ctx.get_personal_coach(max_items=3)

    assert out["recommendations"] == []


def test_server_context_second_gate_drops_cross_email_recommendation(term_store, embedder):
    class LeakyAdoptionStore:
        def rows_for_person(self, viewer):
            return [
                AdoptionRow(
                    row_id="mail-row",
                    scope_unit="kyozai",
                    actor_scope="bob@example.net",
                    business="他人のメール行",
                    current_level=1,
                    target_level=2,
                    share_scope="person",
                )
            ]

    ctx = _ctx(term_store, embedder)
    ctx.adoption_map_store = LeakyAdoptionStore()

    out = ctx.get_personal_coach(max_items=3)

    assert out["recommendations"] == []


def test_server_context_mines_knowledge_candidates(term_store, embedder):
    ctx = _ctx(term_store, embedder)

    out = ctx.mine_knowledge_candidates(
        "red_team",
        "rt:1",
        "安全チャネル不明の迂回が見つかった",
        policy_row_ids=["FLOOR-01"],
        owner_unit="steward",
    )

    assert len(out["candidate_ids"]) == 1
    candidate = ctx.candidate_store.get(out["candidate_ids"][0])
    assert candidate.kind is CandidateKind.SFAQ
    assert out["policy_candidates"][0]["policy_row_ids"] == ["FLOOR-01"]


def test_lookup_terms_zero_hit_mines_candidate_internally(term_store, embedder):
    ctx = _ctx(term_store, embedder)
    ctx.term_store = InMemoryTermStore([], embedder)

    out = ctx.lookup_terms("zz-no-such-lite-term-4417")

    assert out["hits"] == []
    assert [c.kind for c in ctx.candidate_store.list()] == [
        CandidateKind.TERM,
        CandidateKind.SFAQ,
    ]


def test_server_context_fails_closed_when_lite_stores_are_not_configured(term_store, embedder):
    ctx = ServerContext(
        term_store=term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=_viewer(),
    )

    with pytest.raises(RuntimeError, match="policy_guide_store"):
        ctx.get_personal_coach()


def test_policy_workbench_uses_recent_logs_without_generic_lower_bound_warning(
    term_store, embedder
):
    ctx = _ctx(term_store, embedder)
    ctx.log_usage(
        "l1",
        ["internal"],
        "研修システム運用の手順整理",
        "test",
        policy_row_ids=["SEC-5"],
        org_context="サンプル協会事務局のシステム運用",
    )

    out = ctx.start_policy_workbench(
        goal_hint_summary="研修システム運用のチェックリストを作る",
        artifact_type="checklist",
    )

    assert out["not_for_evaluation"] is True
    assert "研修システム運用の手順整理" in out["current_state"]
    assert out["recommended_artifact"]["type"] == "checklist"
    assert out["source_log_ids"]
    assert "SEC-5" in out["source_policy_row_ids"]
    assert "lower_bound_note" not in out
    assert "氏名" not in out["policy_explanation"]
    assert "メール" not in out["policy_explanation"]
    assert out["first_question"].count("？") <= 1


def test_policy_workbench_adds_contextual_lower_bound_only_when_relevant(
    term_store, embedder
):
    ctx = _ctx(term_store, embedder)
    ctx.log_usage(
        "l1",
        ["pii"],
        "会員番号を含む問い合わせ対応の手順整理",
        "test",
        policy_row_ids=["SEC-2", "FLOOR-01"],
        org_context="サンプル協会事務局の会員対応",
    )

    out = ctx.start_policy_workbench(artifact_type="manual")

    assert "lower_bound_note" in out
    assert "架空例または要約" in out["lower_bound_note"]
    assert "FLOOR-01" in out["source_policy_row_ids"]
    assert out["policy_explanation"] == out["lower_bound_note"]


def test_policy_workbench_partial_when_lite_stores_are_not_configured(term_store, embedder):
    ctx = ServerContext(
        term_store=term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=_viewer(),
    )

    out = ctx.start_policy_workbench(goal_hint_summary="手順書を作る")

    assert out["diagnostic"] == "partial"
    assert "policy_guide_store" not in str(out)
    assert out["first_question"]


def test_usage_summary_reader_only_returns_the_viewers_own_rows(term_store, embedder):
    usage = InMemoryUsageLogStore()
    own = ServerContext(
        term_store=term_store,
        usage_store=usage,
        embedder=embedder,
        viewer=_viewer(),
    )
    other_viewer = Viewer(
        subject="sub:p2",
        person_id="p2",
        is_org_member=True,
        units=frozenset({"kyozai"}),
    )
    other = ServerContext(
        term_store=term_store,
        usage_store=usage,
        embedder=embedder,
        viewer=other_viewer,
    )
    own.log_usage("l1", ["internal"], "本人の記録", "test", org_context="サンプル協会")
    other.log_usage("l1", ["internal"], "他人の記録", "test", org_context="サンプル協会")

    rows = usage.recent_summaries_for_actor(other_viewer, "p1", limit=10)

    assert rows == []


@pytest.mark.asyncio
async def test_mine_knowledge_candidates_is_not_public_mcp_tool(term_store, embedder):
    from waca_loop.server import build_mcp

    tools = await build_mcp(_ctx(term_store, embedder)).list_tools()

    assert "mine_knowledge_candidates" not in {t.name for t in tools}


@pytest.mark.asyncio
async def test_policy_workbench_is_public_mcp_tool(term_store, embedder):
    from waca_loop.server import build_mcp

    tools = await build_mcp(_ctx(term_store, embedder)).list_tools()

    assert "start_policy_workbench" in {t.name for t in tools}


def _ctx(term_store, embedder) -> ServerContext:
    return ServerContext(
        term_store=term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=_viewer(),
        candidate_store=InMemoryCandidateStore(),
        policy_guide_store=InMemoryPolicyGuideStore(
            floors=[
                PolicyFloor(
                    row_id="FLOOR-01",
                    line="生PIIを入れない",
                    effect="仮名化する",
                )
            ],
            safe_channels=[
                SafeChannel(
                    channel_id="waca_loop",
                    scope_unit="kyozai",
                    status="approved",
                    required_controls=["仮名化"],
                )
            ],
        ),
        adoption_map_store=InMemoryAdoptionMapStore([
            AdoptionRow(
                row_id="row1",
                scope_unit="kyozai",
                actor_scope="person:p1",
                business="教材レビュー",
                current_level=1,
                target_level=2,
                next_step="観点をAIで下書きする",
                person_intent={"try_next": "レビュー観点の整理"},
                share_scope="person",
            )
        ]),
    )


def _viewer() -> Viewer:
    return Viewer(
        subject="sub:p1",
        person_id="p1",
        is_org_member=True,
        units=frozenset({"kyozai", "steward"}),
    )
