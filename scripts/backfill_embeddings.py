#!/usr/bin/env python3
"""Backfill embeddings for ``terms`` (§2.3) or SFAQ ``questions`` (SPEC_phase2 §3).

Embeddings are computed at ingest/backfill time so search just calls VECTOR_SEARCH.
The embedder is selected by EMBEDDING_PROVIDER (§2.3):

  - unset    -> HashingEmbedder (offline default; fine for dev/validation datasets)
  - bq_ml    -> generate in-SQL via ML.GENERATE_EMBEDDING (this script prints the SQL)
  - external -> wire your embedding client in embeddings.build_embedder

``--table questions`` covers rows filed via add_sfaq_question, which land without an
embedding (NULL) and would otherwise never surface in semantic search after promotion.

Usage:
    python scripts/backfill_embeddings.py --dry-run
    python scripts/backfill_embeddings.py                    # terms missing an embedding
    python scripts/backfill_embeddings.py --table questions  # SFAQ questions
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# 対象テーブルごとの (settings 属性, id 列, content 式)。content は埋め込み入力。
TARGETS = {
    "terms": ("terms_table", "term_id", "CONCAT(t.term, ' ', t.definition)"),
    "questions": ("questions_table", "question_id", "t.question_text"),
}
# add_sfaq_question 経由の行は embedding が NULL で入る — ARRAY_LENGTH は NULL を返し
# `= 0` に一致しないため、NULL も明示的に対象へ含める。
MISSING = "(t.embedding IS NULL OR ARRAY_LENGTH(t.embedding) = 0)"


def _bq_ml_sql(settings, table: str) -> str:
    """The in-SQL path (§2.3 option b): requires a CREATE MODEL over a Vertex connection."""
    attr, id_col, content = TARGETS[table]
    table_ref = getattr(settings, attr)
    return f"""
        UPDATE `{table_ref}` AS t
        SET embedding = (
          SELECT ml_generate_embedding_result
          FROM ML.GENERATE_EMBEDDING(
            MODEL `{settings.embedding_model_ref}`,
            (SELECT t.{id_col} AS id, {content} AS content)
          )
        )
        WHERE {MISSING};
    """.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--table", choices=sorted(TARGETS), default="terms")
    args = parser.parse_args()

    from waca_loop.config import get_settings
    from waca_loop.embeddings import build_embedder

    settings = get_settings()
    provider = settings.embedding_provider.lower() if settings.embedding_provider else ""
    attr, id_col, content = TARGETS[args.table]
    table_ref = getattr(settings, attr)

    if provider == "bq_ml":
        sql = _bq_ml_sql(settings, args.table)
        if args.dry_run:
            print(sql)
            return 0
        from waca_loop.bq import make_client

        make_client(settings).query(sql).result()
        print(f"bq_ml embedding backfill complete ({args.table}).")
        return 0

    # Python-side backfill (HashingEmbedder or a wired external embedder).
    embedder = build_embedder(provider or None, dim=settings.embedding_dim)
    if args.dry_run:
        print(
            f"Would compute embeddings with {type(embedder).__name__} (dim={embedder.dim}) "
            f"for rows in {table_ref} where embedding is NULL or empty."
        )
        return 0

    if not settings.gcp_project:
        print("ERROR: GCP_PROJECT required for live backfill.", file=sys.stderr)
        return 2

    from google.cloud import bigquery

    from waca_loop.bq import make_client

    client = make_client(settings)
    select_cols = f"t.{id_col} AS id, {content} AS content"
    rows = list(
        client.query(
            f"SELECT {select_cols} FROM `{table_ref}` AS t WHERE {MISSING}"
        ).result()
    )
    print(f"computing {len(rows)} embeddings ...")
    for row in rows:
        vec = embedder.embed(row["content"])
        client.query(
            f"UPDATE `{table_ref}` SET embedding = @vec WHERE {id_col} = @id",  # noqa: S608
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("vec", "FLOAT64", vec),
                    bigquery.ScalarQueryParameter("id", "STRING", row["id"]),
                ]
            ),
        ).result()
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
