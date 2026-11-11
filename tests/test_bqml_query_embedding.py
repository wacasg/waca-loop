"""bq_ml query-time embedding (§2.3).

Verifies the wiring without live BigQuery: a fake client captures the SQL/params so we can
assert the query embedding is generated in-SQL (ML.GENERATE_EMBEDDING) for bq_ml, that ACL is
still applied, and that the non-bq_ml path still passes a Python vector.
"""

from __future__ import annotations

import pytest

from waca_loop.config import Settings
from waca_loop.embeddings import BqMlQueryEmbedder, HashingEmbedder, build_embedder
from waca_loop.grounding.terms import BigQueryTermStore


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return iter(self._rows)


class _FakeBQClient:
    """Captures the last query + job_config instead of hitting BigQuery."""

    def __init__(self):
        self.captured: list[tuple] = []

    def query(self, sql, job_config=None):
        self.captured.append((sql, job_config))
        return _FakeJob([])


def _settings(provider: str) -> Settings:
    s = Settings()
    s.gcp_project = "demo"
    s.ds_grounding = "waca_loop_grounding"
    s.embedding_provider = provider
    s.embedding_model = "text-multilingual-embedding-002"
    return s


def test_build_embedder_bqml_returns_marker_and_refuses_python_embed():
    emb = build_embedder("bq_ml")
    assert isinstance(emb, BqMlQueryEmbedder)
    assert emb.provider == "bq_ml"
    with pytest.raises(NotImplementedError):
        emb.embed("色彩設計")  # bq_ml embeds in SQL, not in Python


def test_external_still_unwired():
    with pytest.raises(NotImplementedError):
        build_embedder("external")


def test_inmemory_semantic_candidates_uses_python_embedder(term_store, embedder):
    hits = term_store.semantic_candidates("色の設計", embedder, 5)
    assert any(h.term == "色彩設計" for h in hits)


def test_bqml_semantic_candidates_embeds_in_sql_with_acl(viewer_member):
    client = _FakeBQClient()
    store = BigQueryTermStore(client, _settings("bq_ml"), viewer_member, build_embedder("bq_ml"))

    store.semantic_candidates("色彩設計とは", build_embedder("bq_ml"), 5)

    sql, job_config = client.captured[-1]
    assert "ML.GENERATE_EMBEDDING" in sql            # query embedded in BigQuery
    assert "waca_loop_grounding.embedding_model" in sql      # via the Vertex-backed model
    assert "scope_tier" in sql and "scope_audience" in sql  # ACL still applied (§3.3)
    names = {p.name for p in job_config.query_parameters}
    assert "q" in names                               # query string param
    assert "qvec" not in names                        # NOT a pre-computed vector


def test_nonbqml_semantic_candidates_passes_python_vector(viewer_member):
    client = _FakeBQClient()
    # Empty provider -> HashingEmbedder vectors, classic @qvec VECTOR_SEARCH path.
    store = BigQueryTermStore(client, _settings(""), viewer_member, HashingEmbedder(dim=64))

    store.semantic_candidates("色彩設計", HashingEmbedder(dim=64), 5)

    sql, job_config = client.captured[-1]
    assert "ML.GENERATE_EMBEDDING" not in sql
    names = {p.name for p in job_config.query_parameters}
    assert "qvec" in names
