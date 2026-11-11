"""T3 — ハイブリッド: 表記揺れ・同義の質問でも VECTOR_SEARCH 経由で hit (§8).

"色の設計" shares no token/substring with "色彩設計" (lexical scores 0), but shares
character n-grams (設計 …) so the semantic leg surfaces it. Demonstrates recall the
lexical leg alone would miss.
"""

from __future__ import annotations


def test_paraphrase_misses_lexical_but_hits_semantic(term_store, embedder, viewer_member):
    from waca_loop.retrieval.hybrid import hybrid_search

    query = "色の設計"

    lexical = term_store.lexical_search(query, 5)
    assert all(t.term != "色彩設計" for t in lexical), "lexical leg should NOT find it"

    semantic = term_store.vector_search(embedder.embed(query), 5)
    assert any(t.term == "色彩設計" for t in semantic), "semantic leg should find it"

    hits = hybrid_search(query, viewer_member, 5, store=term_store, embedder=embedder)
    assert any(h.term == "色彩設計" for h in hits), "hybrid should surface the paraphrase match"
