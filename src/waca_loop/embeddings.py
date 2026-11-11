"""Embeddings (§2.3) — provider-pluggable, with a dependency-free default.

The production embedding model is a deployment decision (``bq_ml`` via
``ML.GENERATE_EMBEDDING`` or an ``external`` API). To keep the
hybrid search path fully functional offline and in tests, the default
:class:`HashingEmbedder` is a deterministic, pure-Python embedder — good enough for
semantic *matching* (not evaluation). Swap it for a real model in production by
setting ``EMBEDDING_PROVIDER`` (see :func:`build_embedder`).
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]:
        ...


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; 0.0 if either vector is empty/zero."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _l2_normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0.0:
        return v
    return [x / norm for x in v]


class HashingEmbedder:
    """Deterministic feature-hashing embedder over character n-grams.

    Pure stdlib, no network, reproducible — so ``hybrid_search`` and tests T3/T4
    work without a live embedding service. Not for ranking *quality*; it exists so
    paraphrase/表記揺れ queries land near their terms by shared n-grams.
    """

    def __init__(self, dim: int = 768, ngram: tuple[int, int] = (2, 3)) -> None:
        self.dim = dim
        self._lo, self._hi = ngram

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        norm = (text or "").strip().lower()
        if not norm:
            return vec
        padded = f"^{norm}$"
        for n in range(self._lo, self._hi + 1):
            if len(padded) < n:
                continue
            for i in range(len(padded) - n + 1):
                gram = padded[i : i + n]
                h = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(h[:4], "big") % self.dim
                sign = 1.0 if h[4] & 1 else -1.0
                vec[idx] += sign
        return _l2_normalize(vec)


class BqMlQueryEmbedder:
    """Marker embedder for the ``bq_ml`` path (§2.3).

    With ``bq_ml`` the corpus embeddings live in ``terms.embedding`` (filled by the backfill)
    and the *query* embedding is generated **inside BigQuery** via ``ML.GENERATE_EMBEDDING``
    within ``VECTOR_SEARCH`` — see ``BigQueryTermStore.semantic_candidates``. So there is no
    Python-side vector to compute; :meth:`embed` exists to satisfy the :class:`Embedder`
    protocol but must not be called (the BigQuery store embeds in SQL instead).
    """

    provider = "bq_ml"

    def __init__(self, dim: int = 768, model: str | None = None) -> None:
        self.dim = dim
        self.model = model

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError(
            "bq_ml generates query embeddings in BigQuery (ML.GENERATE_EMBEDDING); "
            "use BigQueryTermStore.semantic_candidates, not Python embed()."
        )


def build_embedder(provider: str | None, *, dim: int = 768, model: str | None = None) -> Embedder:
    """Select an embedder from ``EMBEDDING_PROVIDER`` (§2.3).

    - ``None`` / empty  -> :class:`HashingEmbedder` (offline default; used in tests).
    - ``"bq_ml"``       -> :class:`BqMlQueryEmbedder`; embeddings (corpus AND query) are
                            generated in BigQuery via ``ML.GENERATE_EMBEDDING``. No Python vector.
    - ``"external"``    -> call an external embedding API (not implemented; connect a client here).
    """
    if not provider:
        return HashingEmbedder(dim=dim)
    provider = provider.lower()
    if provider == "bq_ml":
        return BqMlQueryEmbedder(dim=dim, model=model)
    if provider == "external":
        raise NotImplementedError(
            "EMBEDDING_PROVIDER='external' selected but no client is wired yet. "
            "Implement it here (§2.3) or unset EMBEDDING_PROVIDER to use HashingEmbedder."
        )
    raise ValueError(f"Unknown EMBEDDING_PROVIDER: {provider!r}")
