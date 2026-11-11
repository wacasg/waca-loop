"""SFAQ question/feedback stores — InMemory + BigQuery pair, identical surface.

Mirrors the terms/policy store pattern: the BigQuery store binds
:func:`waca_loop.knowledge.acl.question_where_clause` in-SQL (Gate 1) and callers
re-check rows with :func:`question_visible_to` in-process (Gate 2). BigQuery is
lazy-imported so importing this module never pulls cloud deps (CLAUDE.md).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from waca_loop.embeddings import Embedder, cosine
from waca_loop.knowledge.acl import question_visible_to, question_where_clause
from waca_loop.knowledge.embed_text import embedding_text
from waca_loop.models import (
    FbType,
    Feedback,
    Provenance,
    Question,
    QuestionStatus,
    ScopeAudience,
    ScopeSensitivity,
    ScopeTags,
    ScopeTier,
    Viewer,
)
from waca_loop.retrieval.hybrid import rrf

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from google.cloud import bigquery

    from waca_loop.config import Settings


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_datetime(value: object | None) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return _utcnow()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def question_to_row(q: Question) -> dict:
    """Full dict row — Gate 2 always sees every field Gate 1 does."""
    return {
        "question_id": q.question_id,
        "question_text": q.question_text,
        "attributes": q.attributes,
        "cluster_id": q.cluster_id,
        "auto_merge": q.auto_merge,
        "provenance": q.provenance.value,
        "owner_unit": q.owner_unit,
        "scope_tier": q.scope.tier.value,
        "scope_audience": q.scope.audience.value,
        "scope_sensitivity": q.scope.sensitivity.value,
        "perm_corporate": q.perm_corporate,
        "perm_individual": q.perm_individual,
        "status": q.status.value,
        "source": q.source,
        "embed_text": q.embed_text,
        "created_at": q.created_at.isoformat(),
        "updated_at": q.updated_at.isoformat(),
    }


def _row_to_question(row: dict) -> Question:
    import json as _json

    attrs = row.get("attributes")
    if isinstance(attrs, str):
        attrs = _json.loads(attrs) if attrs else None
    return Question(
        question_id=row["question_id"],
        question_text=row["question_text"],
        scope=ScopeTags(
            tier=ScopeTier(row["scope_tier"]),
            audience=ScopeAudience(row["scope_audience"]),
            sensitivity=ScopeSensitivity(row["scope_sensitivity"]),
        ),
        provenance=Provenance(row["provenance"]),
        owner_unit=row.get("owner_unit"),
        attributes=dict(attrs) if attrs else None,
        cluster_id=row.get("cluster_id"),
        auto_merge=bool(row.get("auto_merge", False)),
        perm_corporate=row.get("perm_corporate"),
        perm_individual=bool(row.get("perm_individual", False)),
        status=QuestionStatus(row.get("status", "candidate")),
        source=row.get("source"),
        embed_text=row.get("embed_text"),
        embedding=list(row["embedding"]) if row.get("embedding") else [],
        created_at=_as_datetime(row.get("created_at")),
        updated_at=_as_datetime(row.get("updated_at")),
    )


def feedback_to_row(fb: Feedback) -> dict:
    """Full dict row for ``<DS_KNOWLEDGE>.feedback`` (SPEC_phase3 §2.2 freshness input)."""
    return {
        "feedback_id": fb.feedback_id,
        "question_id": fb.question_id,
        "fb_type": fb.fb_type.value,
        "body": fb.body,
        "author_id": fb.author_id,
        "created_at": fb.created_at.isoformat(),
    }


def _row_to_feedback(row: dict) -> Feedback:
    return Feedback(
        feedback_id=row["feedback_id"],
        question_id=row["question_id"],
        fb_type=FbType(row["fb_type"]),
        body=row.get("body"),
        author_id=row.get("author_id"),
        created_at=_as_datetime(row.get("created_at")),
    )


def _lex_score(query: str, text: str) -> float:
    """Tiny lexical scorer for the offline store (exact > substring > token overlap)."""
    q = (query or "").lower().strip()
    t = (text or "").lower()
    if not q or not t:
        return 0.0
    score = 0.0
    if q == t:
        score += 10.0
    if q in t:
        score += 5.0
    score += sum(1.0 for tok in q.split() if tok and tok in t)
    return score


class InMemoryQuestionStore:
    """Offline store (tests / staging). Same surface as the BigQuery store."""

    def __init__(
        self,
        questions: list[Question] | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self._questions: list[Question] = list(questions or [])
        self._feedback: list[Feedback] = []
        self._embedder = embedder
        if embedder is not None:
            for q in self._questions:
                if not q.embedding:
                    self._embed(q)

    # --- embedding target: body + headline, never the headline alone ---
    def _decision_body(self, question_id: str) -> str | None:
        """The lesson body this question is a headline over (first DECISION feedback)."""
        for fb in self._feedback:
            if fb.question_id == question_id and fb.fb_type is FbType.DECISION and fb.body:
                return fb.body
        return None

    def _embed(self, q: Question) -> None:
        if self._embedder is None:
            return
        body = self._decision_body(q.question_id)
        if body and q.embed_text is None:
            q.embed_text = embedding_text(q.question_text, body)
        q.embedding = self._embedder.embed(q.search_text())

    # --- write ---
    def add_question(self, q: Question) -> None:
        now = _utcnow()
        q.created_at = now
        q.updated_at = now
        if self._embedder is not None and not q.embedding:
            self._embed(q)
        self._questions.append(q)

    def add_feedback(self, fb: Feedback) -> None:
        fb.created_at = _utcnow()
        self._feedback.append(fb)
        # The body IS the data (embed_text). A question embedded before its lesson arrived
        # carries a headline-only vector; re-embed so the twin matches the BigQuery backfill,
        # which JOINs feedback at compute time and therefore never sees that intermediate state.
        if fb.fb_type is FbType.DECISION and fb.body:
            for q in self._questions:
                if q.question_id == fb.question_id:
                    self._embed(q)
                    break

    def set_status(self, question_id: str, status: QuestionStatus) -> bool:
        for q in self._questions:
            if q.question_id == question_id:
                q.status = status
                return True
        return False

    def set_scope(
        self,
        question_id: str,
        tier: ScopeTier,
        sensitivity: ScopeSensitivity | None = None,
        *,
        viewer: Viewer | None = None,
    ) -> bool:
        """Move a question between tiers (§3.3). The gate lives in the caller, not here.

        `add_sfaq_question` files everything as `dept` because one person's unaccepted
        account should not land org-wide. This is how a judgment that turns out to matter
        org-wide is widened afterwards (the steward-only gate is in `set_sfaq_scope`).
        """
        for q in self._questions:
            if q.question_id == question_id:
                q.scope = ScopeTags(
                    tier=tier,
                    audience=q.scope.audience,
                    sensitivity=sensitivity or q.scope.sensitivity,
                )
                return True
        return False

    def delete_questions(self, question_ids: list[str], *, source: str) -> int:
        """Physically remove source-owned SFAQ rows plus their feedback.

        A source's opt-out/RTBF is not a ledger event. The SFAQ row is corpus content, so leaving
        the
        lesson body in ``feedback`` and merely hiding the question would preserve the withdrawn
        material in the knowledge table. Keep the blast radius source-bound and make the second
        run a no-op.
        """
        qids = set(question_ids)
        if not qids:
            return 0
        target_ids = {
            q.question_id
            for q in self._questions
            if q.question_id in qids and q.source == source
        }
        if not target_ids:
            return 0
        before = len(self._questions)
        self._feedback = [fb for fb in self._feedback if fb.question_id not in target_ids]
        self._questions = [q for q in self._questions if q.question_id not in target_ids]
        return before - len(self._questions)

    def find_by_candidate_id(self, candidate_id: str) -> str | None:
        """Reverse link ``attributes.candidate_id`` → ``question_id`` (SPEC_phase2 §2.4 / S8).

        The idempotency key for candidate promotion: it answers "has this candidate already
        minted a question?" so ``approve_candidate`` never mints a twin. Deliberately
        viewer-independent, exactly like ``set_status``:
        it is a write-path guard, not a read path, and returns no content.

        **The id is not inert**: ``set_sfaq_status`` is visibility-blind, so an echoed id is
        a usable write handle. Callers must therefore re-check visibility with :meth:`get`
        before returning the id to anyone (see ``ServerContext.approve_candidate``).
        """
        for q in self._questions:  # insertion order == creation order (twin: oldest wins)
            if (q.attributes or {}).get("candidate_id") == candidate_id:
                return q.question_id
        return None

    # --- read (Gate 1: same rule as the SQL clause, applied in-process) ---
    def search(self, query: str, viewer: Viewer, limit: int) -> list[Question]:
        pool = [q for q in self._questions if question_visible_to(question_to_row(q), viewer)]
        # 字面側も search_text を見る。見出しだけを部分一致にかけると、である体で聞いた
        # ときにまず当たらない — 2本の腕の両方が正規表現の出来に左右されてしまう
        lex = sorted(pool, key=lambda q: _lex_score(query, q.search_text()), reverse=True)
        lex = [q for q in lex if _lex_score(query, q.search_text()) > 0.0][: limit * 2]
        ranked = [q.question_id for q in lex]
        if self._embedder is not None:
            qvec = self._embedder.embed(query)
            sem = sorted(pool, key=lambda q: cosine(qvec, q.embedding), reverse=True)[: limit * 2]
            fused = rrf(ranked, [q.question_id for q in sem])
        else:
            fused = rrf(ranked)
        by_id = {q.question_id: q for q in pool}
        ordered = sorted(fused, key=fused.get, reverse=True)
        return [by_id[qid] for qid in ordered if qid in by_id][:limit]

    def get(self, question_id: str, viewer: Viewer) -> Question | None:
        for q in self._questions:
            if q.question_id == question_id:
                if question_visible_to(question_to_row(q), viewer):
                    return q
                return None
        return None

    def feedback_for(self, question_id: str, viewer: Viewer) -> list[Feedback]:
        """その問いに積もったフィードバック（§3.3 二重ゲートの内側）。

        ``viewer`` は**必須**。既定値を持たせると、呼び忘れた新しい経路で ACL ゲートが
        黙って無効になる。
        """
        return self.feedback_for_many([question_id], viewer).get(question_id, [])

    def feedback_for_many(
        self, question_ids: list[str], viewer: Viewer
    ) -> dict[str, list[Feedback]]:
        """複数の問いのフィードバックをまとめて返す（BigQuery 側の N+1 を避けるため）。"""
        visible = {qid for qid in question_ids if self.get(qid, viewer) is not None}
        out: dict[str, list[Feedback]] = {qid: [] for qid in visible}
        for f in self._feedback:
            if f.question_id in visible:
                out[f.question_id].append(f)
        return out


class BigQueryQuestionStore:
    """Production store. ACL+perm gate applied in-SQL (Gate 1, §3.3/E-3)."""

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

    def _rows(self, sql: str, params: dict, extra_qp: list | None = None) -> list[dict]:
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params

        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[*(extra_qp or []), *scalar_params(params)]
            ),
        )
        return [dict(r) for r in job.result()]

    # --- write ---
    def add_question(self, q: Question) -> None:
        import json as _json

        row = question_to_row(q)
        row["attributes"] = _json.dumps(q.attributes, ensure_ascii=False) if q.attributes else None
        row["embedding"] = list(q.embedding or [])
        now = _utcnow()
        q.created_at = now
        q.updated_at = now
        row["created_at"] = now.isoformat()
        row["updated_at"] = now.isoformat()
        errors = self._client.insert_rows_json(self._settings.questions_table, [row])
        if errors:
            raise RuntimeError(f"questions insert failed: {errors}")

    def add_feedback(self, fb: Feedback) -> None:
        fb.created_at = _utcnow()
        row = feedback_to_row(fb)
        errors = self._client.insert_rows_json(self._settings.sfaq_feedback_table, [row])
        if errors:
            raise RuntimeError(f"feedback insert failed: {errors}")
        # 本文が届いたら、その問いの埋め込みを作り直す（InMemory 双子と同じ挙動）。
        # これが無いと、オフラインでは即座にベクトルが更新されるのに本番では次の backfill
        # まで古いまま — テストは緑なのに本番の検索だけ黙って劣化する。
        # best-effort: 失敗しても記録そのものは落とさない。
        if fb.fb_type is FbType.DECISION and fb.body:
            try:
                self._reembed(fb.question_id, fb.body)
            except Exception as exc:   # noqa: BLE001 — 埋め込みの失敗で記録を巻き戻さない
                # **黙って諦めない。** ここが失敗しても本文は入っているので機能は続くが、
                # 失敗を握り潰すと「オフラインは緑・本番の検索だけ静かに劣化」という、
                # 避けたい形になる。とくに add_question の直後に呼ばれた場合、
                # questions 行は streaming buffer にあり BigQuery は UPDATE を拒む —
                # その場合は次の backfill が拾う。
                _log.warning(
                    "re-embedding deferred for %s (%s); the next backfill will pick it up",
                    fb.question_id, type(exc).__name__,
                )

    def _reembed(self, question_id: str, fallback_body: str) -> None:
        """Recompute one question's embedding over body + headline (embed_text の双子).

        埋め込む本文は **最初の DECISION**（`ORDER BY created_at LIMIT 1`）— InMemory 双子
        と backfill SQL の正典がそれだからである。いま届いた本文を無条件に使うと、2件目の
        DECISION が来たときだけ本番のベクトルが正典から外れ、次の backfill で戻る、という
        揺れ方をする。
        まだ読めない（streaming buffer）場合だけ、いま届いた本文で代用する。

        連結は Python 側（= 正典の embedding_text）で組み、ML.GENERATE_EMBEDDING には
        文字列をパラメータで渡す。入力に他テーブルを参照する相関サブクエリを書くと
        BigQuery が拒否する。

        **不変条件: この関数は本文を呼び出し元へ返さない・ログにも出さない。** feedback を
        viewer 無しで読む（ACL を通らない2つ目の読み口）ので、出口を作った瞬間に漏えいに
        なる。診断のためでも `content` をログに足さないこと。

        なお、渡した文字列は BigQuery の**ジョブ履歴**（INFORMATION_SCHEMA.JOBS）に残る。
        そこは行レベルの scope tag も acl_where_clause もかからない面で、`bigquery.jobs.get`
        を持つ人は SFAQ の scope に関係なく本文を読める。ML.GENERATE_EMBEDDING に文字列を
        渡す以上避けられないため、**BigQuery 側の権限分離の課題**である（`bigquery.jobs.get`
        の付与範囲で制御する）。
        """
        from google.cloud import bigquery

        rows = self._rows(
            f"SELECT question_text FROM `{self._settings.questions_table}` "
            "WHERE question_id = @qid",
            {"qid": question_id},
        )
        if not rows:
            return
        first = self._rows(
            f"SELECT body FROM `{self._settings.sfaq_feedback_table}` "
            "WHERE question_id = @qid AND fb_type = 'decision' AND body IS NOT NULL "
            "ORDER BY created_at LIMIT 1",
            {"qid": question_id},
        )
        body = first[0]["body"] if first else fallback_body
        content = embedding_text(rows[0]["question_text"], body)
        self._client.query(
            f"""
            UPDATE `{self._settings.questions_table}` AS q
            SET embedding = (
              SELECT ml_generate_embedding_result
              FROM ML.GENERATE_EMBEDDING(
                MODEL `{self._settings.embedding_model_ref}`,
                (SELECT @content AS content))
            ), embed_text = @content, updated_at = CURRENT_TIMESTAMP()
            WHERE q.question_id = @qid
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("content", "STRING", content),
                bigquery.ScalarQueryParameter("qid", "STRING", question_id),
            ]),
        ).result()

    def set_status(self, question_id: str, status: QuestionStatus) -> bool:
        from google.cloud import bigquery

        job = self._client.query(
            f"UPDATE `{self._settings.questions_table}` "
            "SET status = @status, updated_at = CURRENT_TIMESTAMP() "
            "WHERE question_id = @qid",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("status", "STRING", status.value),
                    bigquery.ScalarQueryParameter("qid", "STRING", question_id),
                ]
            ),
        )
        job.result()
        return (job.num_dml_affected_rows or 0) > 0

    def set_scope(
        self,
        question_id: str,
        tier: ScopeTier,
        sensitivity: ScopeSensitivity | None = None,
        *,
        viewer: Viewer | None = None,
    ) -> bool:
        """Move a question between tiers (§3.3, store twin).

        ``viewer`` puts the ACL in the WHERE clause too. Every other write here is gated
        only by the caller's earlier read, which is a TOCTOU across two queries — tolerable
        while the write changed `status`, much less so for the first write whose *effect* is
        visibility. With this the database itself refuses a blind rescope, mirroring the
        double enforcement the read path has.
        """
        from google.cloud import bigquery

        from waca_loop.bq import scalar_params

        sets = ["scope_tier = @tier", "updated_at = CURRENT_TIMESTAMP()"]
        params = [
            bigquery.ScalarQueryParameter("tier", "STRING", tier.value),
            bigquery.ScalarQueryParameter("qid", "STRING", question_id),
        ]
        if sensitivity is not None:
            sets.insert(1, "scope_sensitivity = @sens")
            params.append(
                bigquery.ScalarQueryParameter("sens", "STRING", sensitivity.value)
            )
        where = "question_id = @qid"
        if viewer is not None:
            gate_sql, gate_params = question_where_clause(viewer)
            where = f"{where} AND {gate_sql}"
            params.extend(scalar_params(gate_params))
        job = self._client.query(
            f"UPDATE `{self._settings.questions_table}` SET {', '.join(sets)} WHERE {where}",
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        )
        job.result()
        return (job.num_dml_affected_rows or 0) > 0

    def delete_questions(self, question_ids: list[str], *, source: str) -> int:
        """Physically delete source-owned questions and feedback (store twin).

        R6 uses append-only withdrawal rows because usage_log must hide a just-streamed ledger
        entry immediately. SFAQ is different: a source's withdrawal/RTBF means the corpus content
        should
        leave ``questions`` and ``feedback``. If BigQuery refuses DML for a still-buffered row, the
        caller must fail closed and retry after the buffer clears rather than claim success.
        """
        from google.cloud import bigquery

        qids = list(dict.fromkeys(question_ids))
        if not qids:
            return 0
        params = [
            bigquery.ArrayQueryParameter("qids", "STRING", qids),
            bigquery.ScalarQueryParameter("source", "STRING", source),
        ]
        target = (
            f"SELECT question_id FROM `{self._settings.questions_table}` "
            "WHERE source = @source AND question_id IN UNNEST(@qids)"
        )
        self._client.query(
            f"DELETE FROM `{self._settings.sfaq_feedback_table}` "
            f"WHERE question_id IN ({target})",
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        ).result()
        job = self._client.query(
            f"DELETE FROM `{self._settings.questions_table}` "
            "WHERE source = @source AND question_id IN UNNEST(@qids)",
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        )
        job.result()
        return job.num_dml_affected_rows or 0

    def find_by_candidate_id(self, candidate_id: str) -> str | None:
        """Store-pair twin of :meth:`InMemoryQuestionStore.find_by_candidate_id` (INV-3).

        No viewer clause on purpose — same rationale as ``set_status`` above: this is the
        write-path idempotency guard for the steward-gated promotion, and it returns only
        the opaque question_id.
        """
        rows = self._rows(
            f"SELECT question_id FROM `{self._settings.questions_table}` "
            "WHERE JSON_VALUE(attributes, '$.candidate_id') = @cid "
            "ORDER BY created_at LIMIT 1",  # deterministic if two rows share the candidate_id
            {"cid": candidate_id},
        )
        return rows[0]["question_id"] if rows else None

    # --- read ---
    def search(self, query: str, viewer: Viewer, limit: int) -> list[Question]:
        gate_sql, params = question_where_clause(viewer, alias="q")
        # 字面側も embed_text（見出し＋本文）を見る。InMemory 双子の search_text() と
        # 同じ対象でなければ、二つのストアが違う結果を返す。
        lex = self._rows(
            f"""
            SELECT q.*,
              ( IF(STRPOS(LOWER(COALESCE(q.embed_text, q.question_text)), @sq) > 0, 5.0, 0.0) )
                AS lex_score
            FROM `{self._settings.questions_table}` AS q
            WHERE {gate_sql}
              AND STRPOS(LOWER(COALESCE(q.embed_text, q.question_text)), @sq) > 0
            ORDER BY lex_score DESC
            LIMIT @lim
            """,
            {"sq": (query or "").lower(), "lim": limit * 2, **params},
        )
        sem = self._semantic(query, limit * 2)
        fused = rrf(
            [r["question_id"] for r in lex],
            [r["question_id"] for r in sem],
        )
        by_id: dict[str, dict] = {}
        for r in [*lex, *sem]:
            by_id.setdefault(r["question_id"], r)
        ordered = sorted(fused, key=fused.get, reverse=True)
        return [_row_to_question(by_id[qid]) for qid in ordered if qid in by_id][:limit]

    def _semantic(self, query_text: str, limit: int) -> list[dict]:
        from google.cloud import bigquery

        gate_sql, params = question_where_clause(self._viewer, alias="q")
        if self._settings.embedding_provider.lower() == "bq_ml":
            sql = f"""
                SELECT base.* EXCEPT(embedding)
                FROM VECTOR_SEARCH(
                  (SELECT * FROM `{self._settings.questions_table}` AS q
                   WHERE {gate_sql} AND ARRAY_LENGTH(q.embedding) > 0),
                  'embedding',
                  (SELECT ml_generate_embedding_result AS embedding
                   FROM ML.GENERATE_EMBEDDING(
                     MODEL `{self._settings.embedding_model_ref}`,
                     (SELECT @sq AS content))),
                  top_k => @lim,
                  distance_type => 'COSINE'
                )
            """
            extra = [bigquery.ScalarQueryParameter("sq", "STRING", query_text)]
            return self._rows(sql, {"lim": limit, **params}, extra_qp=extra)
        qvec = self._embedder.embed(query_text)
        sql = f"""
            SELECT base.* EXCEPT(embedding)
            FROM VECTOR_SEARCH(
              (SELECT * FROM `{self._settings.questions_table}` AS q
               WHERE {gate_sql} AND ARRAY_LENGTH(q.embedding) > 0),
              'embedding',
              (SELECT @qvec AS embedding),
              top_k => @lim,
              distance_type => 'COSINE'
            )
        """
        extra = [bigquery.ArrayQueryParameter("qvec", "FLOAT64", qvec)]
        return self._rows(sql, {"lim": limit, **params}, extra_qp=extra)

    def get(self, question_id: str, viewer: Viewer) -> Question | None:
        gate_sql, params = question_where_clause(viewer, alias="q")
        rows = self._rows(
            f"SELECT q.* FROM `{self._settings.questions_table}` AS q "
            f"WHERE q.question_id = @qid AND {gate_sql}",
            {"qid": question_id, **params},
        )
        return _row_to_question(rows[0]) if rows else None

    def feedback_for(self, question_id: str, viewer: Viewer) -> list[Feedback]:
        """フィードバック本文を返す。``viewer`` は必須で、**SQL 側でも**ゲートする。"""
        return self.feedback_for_many([question_id], viewer).get(question_id, [])

    def feedback_for_many(
        self, question_ids: list[str], viewer: Viewer
    ) -> dict[str, list[Feedback]]:
        """複数の問いのフィードバックを **1本のクエリ**でまとめて返す。

        §3.3 の二重ゲートは「見えない行はそもそもDBから出ない」半分と「取ってから
        再確認する」半分でできている。前者が無いと、question_id さえ
        分かれば誰の判断でも本文と author_id が取れる読み口になる。questions と EXISTS
        で突き合わせて同じ WHERE 句を効かせる。

        一括にしてあるのは、`search_sfaq` が1件ずつ呼ぶと **k件ぶんのクエリ**になるため
        （feedback テーブルは question_id でクラスタリングされていないので、そのたびに
        走査が走る）。
        """
        from google.cloud import bigquery

        if not question_ids:
            return {}
        gate_sql, gate_params = question_where_clause(viewer, alias="q")
        sql = (
            f"SELECT f.* FROM `{self._settings.sfaq_feedback_table}` AS f "
            "WHERE f.question_id IN UNNEST(@qids) "
            f"AND EXISTS (SELECT 1 FROM `{self._settings.questions_table}` AS q "
            f"WHERE q.question_id = f.question_id AND {gate_sql}) "
            "ORDER BY f.question_id, f.created_at"
        )
        extra = [bigquery.ArrayQueryParameter("qids", "STRING", list(question_ids))]
        rows = self._rows(sql, gate_params, extra_qp=extra)
        out: dict[str, list[Feedback]] = {}
        for r in rows:
            fb = _row_to_feedback(r)
            out.setdefault(fb.question_id, []).append(fb)
        return out
