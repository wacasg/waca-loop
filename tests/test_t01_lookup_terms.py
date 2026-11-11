"""T1 — lookup_terms 正常: 既知用語で hit を返す。定義・スコープを含む (§8)."""

from __future__ import annotations


def test_lookup_known_term_returns_definition_and_scope(context):
    out = context.lookup_terms("サンプル協会", k=5)
    hits = out["hits"]
    assert hits, "known term should return at least one hit"
    top = hits[0]
    assert top["term"] == "サンプル協会"
    assert top["definition"]
    assert set(top["scope"]) == {"tier", "audience", "sensitivity"}
    assert top["kind"] == "term"


def test_lookup_respects_k(context):
    out = context.lookup_terms("社内", k=1)
    assert len(out["hits"]) <= 1
