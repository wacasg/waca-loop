"""Content identification (特定) — link free-text feedback to a concrete target.

Reuses `retrieval.hybrid_search` (§3.2): the same lexical+semantic+RRF matcher that powers
`lookup_terms`. A self-reported reference (the usage_log row the user attached) always wins —
it needs no inference. ACL is preserved because hybrid_search filters by the viewer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from waca_loop.retrieval.hybrid import hybrid_search

if TYPE_CHECKING:
    from waca_loop.embeddings import Embedder
    from waca_loop.models import Viewer
    from waca_loop.retrieval.hybrid import TermStore


@dataclass
class Identified:
    target_kind: str   # term | usage_log | none
    target_id: str | None
    score: float


def identify_content(
    text: str,
    viewer: Viewer,
    *,
    term_store: TermStore,
    embedder: Embedder,
    explicit_log_id: str | None = None,
    k: int = 3,
) -> Identified:
    """Best-effort identify what a piece of feedback refers to.

    1. If the user attached a `usage_log` reference, use it directly (highest confidence).
    2. Otherwise match the text against terms via `hybrid_search` (ACL-filtered for the viewer).
    3. If nothing matches, return ``target_kind='none'`` → caller routes to manual triage.
    """
    if explicit_log_id:
        return Identified("usage_log", explicit_log_id, 1.0)
    if text and text.strip():
        hits = hybrid_search(text, viewer, k, store=term_store, embedder=embedder)
        if hits:
            top = hits[0]
            return Identified("term", top.term_id, top.score)
    return Identified("none", None, 0.0)
