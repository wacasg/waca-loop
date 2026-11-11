"""Term stores (§2.2 ①): the ``TermStore`` backends used by hybrid_search (§3.2).

- :class:`InMemoryTermStore` — pure-Python, used by tests and offline; exercises the
  real ``bm25_like`` lexical scorer and cosine semantic ranking.
- :class:`BigQueryTermStore` — production; lexical via SQL, semantic via ``VECTOR_SEARCH``.
  The ACL WHERE clause (§3.3) is applied in-SQL so forbidden rows never leave BQ.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from waca_loop.embeddings import Embedder, cosine
from waca_loop.models import (
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Term,
)
from waca_loop.retrieval.acl import acl_where_clause
from waca_loop.retrieval.hybrid import bm25_like

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings
    from waca_loop.models import Viewer


class InMemoryTermStore:
    """In-memory ``TermStore`` over a list of :class:`Term` (tests / offline)."""

    def __init__(self, terms: list[Term], embedder: Embedder) -> None:
        self._terms = list(terms)
        self._embedder = embedder
        # Backfill embeddings for any term missing one, so vector_search works.
        for t in self._terms:
            if not t.embedding:
                t.embedding = embedder.embed(f"{t.term} {t.definition}")

    def add(self, term: Term) -> None:
        if not term.embedding:
            term.embedding = self._embedder.embed(f"{term.term} {term.definition}")
        self._terms.append(term)

    def lexical_search(self, query: str, limit: int) -> list[Term]:
        return [t for t, _ in bm25_like(query, self._terms)[:limit]]

    def vector_search(self, embedding: list[float], limit: int) -> list[Term]:
        scored = [(t, cosine(embedding, t.embedding)) for t in self._terms]
        scored = [(t, s) for t, s in scored if s > 0.0]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [t for t, _ in scored[:limit]]

    def semantic_candidates(self, query: str, embedder: Embedder, limit: int) -> list[Term]:
        # Offline path: embed the query in Python, then cosine over the in-memory corpus.
        return self.vector_search(embedder.embed(query), limit)


def _row_to_term(row: Any) -> Term:
    return Term(
        term_id=row["term_id"],
        term=row["term"],
        definition=row["definition"],
        scope=ScopeTags(
            tier=ScopeTier(row["scope_tier"]),
            audience=ScopeAudience(row["scope_audience"]),
            sensitivity=ScopeSensitivity(row["scope_sensitivity"]),
        ),
        reading=row.get("reading"),
        basis=row.get("basis"),
        attributes=dict(row["attributes"]) if row.get("attributes") else None,
        owner_unit=row.get("owner_unit"),
        embedding=list(row["embedding"]) if row.get("embedding") else [],
        status=row.get("status", "approved"),
        version=row.get("version", 1),
    )


class BigQueryTermStore:
    """Production ``TermStore`` backed by BigQuery (§2.2/§2.3/§3.3).

    Both search methods bind the ACL WHERE clause for ``viewer`` so the database never
    returns forbidden rows; :func:`hybrid_search`'s in-process ``acl_filter`` is then a
    redundant second gate (§3.3 "二重で効かせる").
    """

    def __init__(
        self,
        client: bigquery.Client,
        settings: Settings,
        viewer: Viewer,
        embedder: Embedder,
    ) -> None:
        self._client = client
        self._settings = settings
        self._viewer = viewer
        self._embedder = embedder

    # --- write (用語の即時書き込み) ---
    def add(self, term: Term) -> None:
        """Write one approved term so it is searchable **immediately**.

        Why DML and not ``insert_rows_json``:

        A streaming insert parks the row in the streaming buffer for up to ~90 minutes, and
        BigQuery refuses UPDATE/DELETE against rows sitting there. That is precisely what
        ``scripts/backfill_embeddings.py`` does
        (``UPDATE ... WHERE ARRAY_LENGTH(embedding)=0``) — and it is why ``seed_terms.py``
        uses a load job rather than streaming. Adding a naive streaming insert here would
        make the backfill's UPDATE fail on exactly the newly-approved rows. BigQuery blocks
        DML against buffered rows for roughly 30-90 minutes, not forever — so the precise
        risk is not "permanently un-embedded" but "the one backfill run that happens fails
        on exactly these rows, with no retry and no ledger entry for the skip, and nobody
        notices". That is sufficient: a word that silently never reaches vector search is
        indistinguishable from one that was never approved. Making the backfill re-visit
        buffered rows would fix the symptom by making every backfill run guess when a row left
        the buffer, and approving into ``extraction_candidates`` with promotion by a nightly
        batch would fail the acceptance criterion outright — the word would not be in ``terms``
        at all, and having ``lookup_terms`` read the candidate box instead would put
        un-approved AI-authored text on a read path the ACL clause does not cover.

        DML INSERT writes straight to managed storage: no buffer, so the backfill's UPDATE
        works on the very next run. Approval is a human, low-volume action (a steward
        clicking 承認), so the DML quota is not a constraint here — unlike ``add_feedback``,
        which is per-conversation.

        The embedding is computed inline when the Python embedder can do it, so search is
        whole from the first second. With ``EMBEDDING_PROVIDER=bq_ml`` the vector can only be
        made in SQL, so the row lands with an empty ``embedding`` and the backfill fills it —
        the 「埋め込みは後追いでよい」 case. Either way the row is on the lexical path
        at once, which is what the acceptance criterion asks for.
        """
        import json as _json
        from datetime import UTC, datetime

        from google.cloud import bigquery

        if not term.embedding:
            # bq_ml has no Python-side vector; leave it empty (not NULL) so the backfill's
            # ARRAY_LENGTH()=0 predicate matches, and let vector search skip it until then.
            if (self._settings.embedding_provider or "").lower() != "bq_ml":
                term.embedding = self._embedder.embed(f"{term.term} {term.definition}")

        now = datetime.now(UTC)
        # MERGE（upsert）であって素の INSERT ではない。BigQuery は PRIMARY KEY を強制しない
        # ので、同じ `term_id` で二度呼ぶと素の INSERT なら行が2つできる。承認の経路では
        # id が候補 id から決まる（`_term_id_for`）ため、シンク書き込みと `set_status` の
        # あいだで落ちた再試行はここへ**同じ id で**戻ってくる — そのとき2行目を作らせない
        # のがこの MERGE の役目である。
        #
        # 更新時、埋め込みの元になる文字列（term + definition）が変わっていなければ既存の
        # ベクトルを残し、変わっていれば空に戻して backfill に拾わせる。作り直すべき
        # ベクトルを黙って使い続けない。
        sql = f"""
            MERGE `{self._settings.terms_table}` AS T
            USING (SELECT @term_id AS term_id) AS S
            ON T.term_id = S.term_id
            WHEN MATCHED THEN UPDATE SET
              term = @term,
              reading = @reading,
              definition = @definition,
              basis = @basis,
              attributes = IF(@attributes IS NULL, NULL, PARSE_JSON(@attributes)),
              scope_tier = @scope_tier,
              scope_audience = @scope_audience,
              scope_sensitivity = @scope_sensitivity,
              owner_unit = @owner_unit,
              status = @status,
              embedding = IF(
                T.term = @term AND T.definition = @definition, T.embedding, @embedding
              ),
              updated_at = @updated_at
            WHEN NOT MATCHED THEN INSERT
              (term_id, term, reading, definition, basis, attributes,
               scope_tier, scope_audience, scope_sensitivity, owner_unit,
               embedding, status, version, created_at, updated_at)
            VALUES
              (@term_id, @term, @reading, @definition, @basis,
               IF(@attributes IS NULL, NULL, PARSE_JSON(@attributes)),
               @scope_tier, @scope_audience, @scope_sensitivity, @owner_unit,
               @embedding, @status, @version, @created_at, @updated_at)
        """  # noqa: S608 — table ref comes from settings, every value is a bound parameter
        attrs = (
            _json.dumps(term.attributes, ensure_ascii=False) if term.attributes else None
        )
        params = [
            bigquery.ScalarQueryParameter("term_id", "STRING", term.term_id),
            bigquery.ScalarQueryParameter("term", "STRING", term.term),
            bigquery.ScalarQueryParameter("reading", "STRING", term.reading),
            bigquery.ScalarQueryParameter("definition", "STRING", term.definition),
            bigquery.ScalarQueryParameter("basis", "STRING", term.basis),
            bigquery.ScalarQueryParameter("attributes", "STRING", attrs),
            bigquery.ScalarQueryParameter("scope_tier", "STRING", term.scope.tier.value),
            bigquery.ScalarQueryParameter(
                "scope_audience", "STRING", term.scope.audience.value
            ),
            bigquery.ScalarQueryParameter(
                "scope_sensitivity", "STRING", term.scope.sensitivity.value
            ),
            bigquery.ScalarQueryParameter("owner_unit", "STRING", term.owner_unit),
            bigquery.ArrayQueryParameter("embedding", "FLOAT64", list(term.embedding or [])),
            bigquery.ScalarQueryParameter("status", "STRING", term.status),
            bigquery.ScalarQueryParameter("version", "INT64", term.version),
            bigquery.ScalarQueryParameter("created_at", "TIMESTAMP", now),
            bigquery.ScalarQueryParameter("updated_at", "TIMESTAMP", now),
        ]
        self._client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)
        ).result()

    def _query(self, sql: str, params: dict) -> list[Term]:
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params

        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=scalar_params(params)),
        )
        return [_row_to_term(row) for row in job.result()]

    def lexical_search(self, query: str, limit: int) -> list[Term]:
        acl_sql, acl_params = acl_where_clause(self._viewer, alias="t")
        params = {"q": (query or "").lower(), "lim": limit, **acl_params}
        sql = f"""
            SELECT *,
              ( IF(LOWER(t.term) = @q, 10.0, 0.0)
              + IF(STRPOS(LOWER(t.term), @q) > 0, 5.0, 0.0)
              + IF(STRPOS(LOWER(CONCAT(t.term, ' ', t.definition,
                                       ' ', IFNULL(t.basis, ''))), @q) > 0, 2.0, 0.0)
              ) AS lex_score
            FROM `{self._settings.terms_table}` AS t
            WHERE t.status = 'approved'
              AND {acl_sql}
              AND STRPOS(LOWER(CONCAT(t.term, ' ', IFNULL(t.reading, ''), ' ',
                                      t.definition, ' ', IFNULL(t.basis, ''))), @q) > 0
            ORDER BY lex_score DESC
            LIMIT @lim
        """
        return self._query(sql, params)

    def vector_search(self, embedding: list[float], limit: int) -> list[Term]:
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params

        acl_sql, acl_params = acl_where_clause(self._viewer, alias="t")
        params = {"lim": limit, **acl_params}
        # VECTOR_SEARCH over terms.embedding (§2.3). The query embedding is passed as an
        # ARRAY<FLOAT64> param; ACL is enforced in the base-table subquery.
        sql = f"""
            SELECT base.* EXCEPT(embedding), base.embedding
            FROM VECTOR_SEARCH(
              (SELECT * FROM `{self._settings.terms_table}` AS t
               WHERE t.status = 'approved' AND {acl_sql}
                 AND ARRAY_LENGTH(t.embedding) > 0),
              'embedding',
              (SELECT @qvec AS embedding),
              top_k => @lim,
              distance_type => 'COSINE'
            )
        """
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("qvec", "FLOAT64", embedding),
                    *scalar_params(params),
                ]
            ),
        )
        return [_row_to_term(row) for row in job.result()]

    def semantic_candidates(self, query: str, embedder: Embedder, limit: int) -> list[Term]:
        """Semantic candidates for a raw query (§2.3).

        With ``bq_ml`` the query embedding is generated in BigQuery (no Python vector);
        otherwise a Python ``embedder`` produces the vector and we fall back to
        :meth:`vector_search`.
        """
        if self._settings.embedding_provider.lower() == "bq_ml":
            return self._vector_search_bqml(query, limit)
        return self.vector_search(embedder.embed(query), limit)

    def _vector_search_bqml(self, query_text: str, limit: int) -> list[Term]:
        """VECTOR_SEARCH where the query embedding is generated in-SQL via
        ``ML.GENERATE_EMBEDDING`` over the Vertex-backed model (§2.3 option b). ACL is enforced
        in the base-table subquery, so forbidden rows never enter the search."""
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params

        acl_sql, acl_params = acl_where_clause(self._viewer, alias="t")
        params = {"lim": limit, **acl_params}
        sql = f"""
            SELECT base.* EXCEPT(embedding), base.embedding
            FROM VECTOR_SEARCH(
              (SELECT * FROM `{self._settings.terms_table}` AS t
               WHERE t.status = 'approved' AND {acl_sql}
                 AND ARRAY_LENGTH(t.embedding) > 0),
              'embedding',
              (SELECT ml_generate_embedding_result AS embedding
               FROM ML.GENERATE_EMBEDDING(
                 MODEL `{self._settings.embedding_model_ref}`,
                 (SELECT @q AS content))),
              top_k => @lim,
              distance_type => 'COSINE'
            )
        """
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("q", "STRING", query_text),
                    *scalar_params(params),
                ]
            ),
        )
        return [_row_to_term(row) for row in job.result()]
