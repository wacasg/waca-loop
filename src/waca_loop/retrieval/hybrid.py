"""Hybrid search (§3.2): BM25-like lexical + VECTOR_SEARCH semantic, fused by RRF.

Language-agnostic interface (§1.2 — Rust-extraction candidate). The store backends
(in-memory for tests, BigQuery for production) live in ``waca_loop.grounding``;
this module only depends on the :class:`TermStore` protocol and pure functions.
"""

from __future__ import annotations

import re
from typing import Protocol

from waca_loop.embeddings import Embedder
from waca_loop.models import Kind, Term, TermHit, Viewer
from waca_loop.retrieval.acl import acl_filter

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class TermStore(Protocol):
    """Backend over the ``terms`` table. Implementations: InMemory / BigQuery."""

    def lexical_search(self, query: str, limit: int) -> list[Term]:
        """Exact/partial (BM25-like) candidates ordered best-first."""
        ...

    def vector_search(self, embedding: list[float], limit: int) -> list[Term]:
        """VECTOR_SEARCH over a pre-computed query vector, ordered best-first."""
        ...

    def semantic_candidates(self, query: str, embedder: Embedder, limit: int) -> list[Term]:
        """Semantic candidates for a raw query string.

        The store decides how the query is embedded: the offline store uses the Python
        ``embedder``; the BigQuery store may embed in-SQL (``bq_ml``). This is the seam
        ``hybrid_search`` calls so the embedding strategy stays inside the backend.
        """
        ...


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def bm25_like(query: str, terms: list[Term]) -> list[tuple[Term, float]]:
    """Lightweight lexical scorer (§3.2 step 1): exact/substring + token overlap.

    Not full BM25 — a transparent proxy used by the in-memory store and as a reference
    for the BigQuery store's SQL ranking. Returns ``(term, score)`` sorted desc.
    """
    q = (query or "").strip().lower()
    q_tokens = set(_tokens(q))
    scored: list[tuple[Term, float]] = []
    for t in terms:
        haystack_fields = [t.term, t.reading or "", t.definition, t.basis or ""]
        haystack = " ".join(haystack_fields).lower()
        score = 0.0
        if q and t.term.lower() == q:
            score += 10.0                      # exact term match
        elif q and q in t.term.lower():
            score += 5.0                       # term substring
        if q and q in haystack:
            score += 2.0                       # appears anywhere
        if q_tokens:
            overlap = q_tokens & set(_tokens(haystack))
            score += len(overlap) / len(q_tokens)
        if score > 0.0:
            scored.append((t, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def rrf(*ranked_lists: list[str], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion (§3.2 step 3).

    Each input is a list of ids ordered best-first. Returns ``{id: fused_score}``;
    an id ranked highly in either list rises. ``k`` is the standard RRF constant.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def hybrid_search(
    query: str,
    viewer: Viewer,
    k: int,
    *,
    store: TermStore,
    embedder: Embedder,
    candidate_multiplier: int = 4,
) -> list[TermHit]:
    """Hybrid retrieve + mandatory ACL filter (§3.2).

    1. lexical  = store.lexical_search(query)
    2. semantic = store.semantic_candidates(query, embedder)   # Python vector or in-SQL (bq_ml)
    3. fused    = rrf(lexical, semantic)
    4. return     acl_filter(fused, viewer)   ★ never optional

    Over-fetches candidates (``k * candidate_multiplier``) so ACL drops don't starve
    the top-k.
    """
    fetch = max(k * candidate_multiplier, k)
    lexical = store.lexical_search(query, fetch)
    semantic = store.semantic_candidates(query, embedder, fetch)

    by_id: dict[str, Term] = {t.term_id: t for t in (*lexical, *semantic)}
    fused = rrf(
        [t.term_id for t in lexical],
        [t.term_id for t in semantic],
    )

    ordered_ids = sorted(fused, key=lambda tid: fused[tid], reverse=True)
    hits = [
        TermHit(
            term_id=t.term_id,
            term=t.term,
            definition=t.definition,
            scope=t.scope,
            basis=t.basis,
            kind=Kind.TERM,
            owner_unit=t.owner_unit,
            score=fused[tid],
        )
        for tid in ordered_ids
        if (t := by_id.get(tid)) is not None
    ]
    return acl_filter(hits, viewer)[:k]
