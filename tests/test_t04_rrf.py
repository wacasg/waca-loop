"""T4 — RRF融合: lexical/semantic 両方の上位が適切に統合される (§8)."""

from __future__ import annotations

from waca_loop.retrieval.hybrid import rrf


def test_item_in_both_lists_outranks_single_list_items():
    lexical = ["a", "b", "c"]
    semantic = ["b", "d", "e"]
    scores = rrf(lexical, semantic)
    # "b" appears in both -> should outrank items appearing in only one.
    assert scores["b"] > scores["a"]
    assert scores["b"] > scores["d"]


def test_higher_rank_contributes_more():
    scores = rrf(["x", "y"])
    assert scores["x"] > scores["y"]


def test_empty_inputs():
    assert rrf() == {}
    assert rrf([], []) == {}
