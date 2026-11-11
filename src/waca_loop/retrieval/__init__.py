"""Retrieval — hybrid (lexical + semantic) search with mandatory ACL filtering (§3).

Defined language-agnostically so the hot path can later be extracted to Rust (§1.2).
"""

from waca_loop.retrieval.acl import acl_filter, acl_where_clause, visible_to
from waca_loop.retrieval.hybrid import bm25_like, hybrid_search, rrf

__all__ = [
    "acl_filter",
    "acl_where_clause",
    "visible_to",
    "bm25_like",
    "hybrid_search",
    "rrf",
]
