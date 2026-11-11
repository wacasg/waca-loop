"""S4 — 特定: harmful 記述から関連 term が紐づく／自己申告参照が最優先."""

from __future__ import annotations

from waca_loop.improvement.identify import identify_content


def test_identifies_term_from_feedback_text(term_store, embedder, viewer_member):
    ident = identify_content(
        "サンプル協会の定義をAIが誤って説明した",
        viewer_member,
        term_store=term_store,
        embedder=embedder,
    )
    assert ident.target_kind == "term"
    assert ident.target_id == "t_org"


def test_explicit_log_ref_wins(term_store, embedder, viewer_member):
    ident = identify_content(
        "何か問題があった",
        viewer_member,
        term_store=term_store,
        embedder=embedder,
        explicit_log_id="log-123",
    )
    assert ident.target_kind == "usage_log"
    assert ident.target_id == "log-123"
    assert ident.score == 1.0


def test_no_match_routes_to_manual(term_store, embedder, viewer_member):
    ident = identify_content(
        "", viewer_member, term_store=term_store, embedder=embedder
    )
    assert ident.target_kind == "none"
    assert ident.target_id is None
