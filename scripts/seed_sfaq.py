#!/usr/bin/env python3
"""Load an SFAQ pre-staged seed (questions + feedback) into `<DS_KNOWLEDGE>` (SPEC_phase2 §4).

Input JSON: {"meta": {...}, "questions": [{question_id, question_text, attributes?,
feedback: [{fb_type, body}], provenance, owner_unit?, scope:{tier,audience,sensitivity},
status?, source?, perm_corporate?, perm_individual?}]}.

Idempotent: deletes the question_ids (and their feedback) in the file, then loads.
Uses load jobs (not streaming inserts) so the embedding backfill UPDATE can run
immediately (same reason as seed_terms.py).

Migration defaults (S2/S4) are honored fail-closed: perm_corporate defaults to None
(unconfirmed = hidden), perm_individual to False, status to provisional.

Usage:
    python scripts/seed_sfaq.py sfaq_seed.json --dry-run
    python scripts/seed_sfaq.py sfaq_seed.json
    python scripts/seed_sfaq.py --rebuild-embed-text    # embed_text = 本文＋見出し
    python scripts/seed_sfaq.py --backfill-embeddings   # ML.GENERATE_EMBEDDING for questions
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


def _rows(payload: dict) -> tuple[list[dict], list[dict]]:
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    q_rows: list[dict] = []
    fb_rows: list[dict] = []
    for q in payload["questions"]:
        scope = q["scope"]
        q_rows.append({
            "question_id": q["question_id"],
            "question_text": q["question_text"],
            "attributes": (
                json.dumps(q["attributes"], ensure_ascii=False) if q.get("attributes") else None
            ),
            "cluster_id": q.get("cluster_id"),
            "auto_merge": bool(q.get("auto_merge", False)),      # S6 default off
            "provenance": q["provenance"],
            "owner_unit": q.get("owner_unit"),
            "scope_tier": scope["tier"],
            "scope_audience": scope["audience"],
            "scope_sensitivity": scope["sensitivity"],
            "perm_corporate": q.get("perm_corporate"),           # None = unconfirmed (hidden)
            "perm_individual": bool(q.get("perm_individual", False)),
            "status": q.get("status", "provisional"),            # S4 migration default
            "source": q.get("source"),
            # 検索が見るのは見出しではなく「見出し＋本文」。seed が本文を持って
            # いれば先に埋め、無ければ --rebuild-embed-text が feedback から組み直す
            "embed_text": q.get("embed_text"),
            "embedding": [],                                     # backfill picks these up
            "created_at": now,
            "updated_at": now,
        })
        for fb in q.get("feedback", []):
            fb_rows.append({
                "feedback_id": f"fb_{uuid.uuid4().hex[:12]}",
                "question_id": q["question_id"],
                "fb_type": fb["fb_type"],
                "body": fb.get("body"),
                "author_id": fb.get("author_id"),
                "created_at": now,
            })
    return q_rows, fb_rows


def _backfill_sql(settings, *, regenerate: bool = False) -> str:
    """Embed **body + headline**, matching waca_loop.knowledge.embed_text.

    The lesson body lives in `feedback` (fb_type='decision'), one JOIN away, so the
    concatenation happens in SQL — the InMemory twin builds the same string in Python.
    Keep SUBSTR in step with ``embed_text.MAX_BODY_CHARS`` and the separator with
    ``embed_text.SEPARATOR``. **本文と見出しを個別に TRIM する** — Python 側は
    ``embedding_text`` が各要素を strip するので、全体だけ TRIM すると
    `"  本文  "` + `"  見出し  "` で双子が別の文字列を埋め込む。

    Two statements, not one: BigQuery refuses a correlated subquery that reaches another
    table from inside ML.GENERATE_EMBEDDING ("Correlated subqueries that reference other
    tables are not supported"), so the join happens in a plain SELECT, the embeddings land
    in a temp table, and the UPDATE reads from that.

    ``regenerate`` recomputes every row instead of only the un-embedded ones; needed once
    when the embedding target changes, and after that only when it changes again.

    ``updated_at`` も更新する。しないと「いつ作り直したか」が行から辿れず、実行の有無を
    確かめられず、「再生成が実行されていない」と誤判定される材料になる。
    """
    from waca_loop.knowledge.embed_text import MAX_BODY_CHARS, SEPARATOR

    where = "TRUE" if regenerate else "ARRAY_LENGTH(q.embedding) = 0"
    return f"""
        CREATE TEMP TABLE _sfaq_embeddings AS
        SELECT id AS question_id, content AS embed_text, ml_generate_embedding_result AS embedding
        FROM ML.GENERATE_EMBEDDING(
          MODEL `{settings.embedding_model_ref}`,
          (
            SELECT q.question_id AS id,
                   TRIM(CONCAT(
                     IFNULL(
                       CONCAT(TRIM(SUBSTR(f.body, 1, {MAX_BODY_CHARS})), {SEPARATOR!r}),
                       ''),
                     TRIM(q.question_text))) AS content
            FROM `{settings.questions_table}` AS q
            LEFT JOIN (
              SELECT question_id,
                     ARRAY_AGG(body ORDER BY created_at LIMIT 1)[OFFSET(0)] AS body
              FROM `{settings.sfaq_feedback_table}`
              WHERE fb_type = 'decision' AND body IS NOT NULL
              GROUP BY question_id
            ) AS f
            ON f.question_id = q.question_id
            WHERE {where}
          ),
          STRUCT('RETRIEVAL_DOCUMENT' AS task_type)
        );

        UPDATE `{settings.questions_table}` AS q
        SET embed_text = e.embed_text,
            embedding = e.embedding,
            updated_at = CURRENT_TIMESTAMP()
        FROM _sfaq_embeddings AS e
        WHERE q.question_id = e.question_id;
    """


def _rebuild_embed_text_sql(settings) -> str:
    """既存行の `embed_text` を正典の「本文＋見出し」で組み直す。

    本文は feedback(decision) にあるので JOIN が要る。embedding は空に戻し、
    次の `--backfill-embeddings` で同じ文字列から作り直す。
    """
    from waca_loop.knowledge.embed_text import MAX_BODY_CHARS, SEPARATOR

    return f"""
        UPDATE `{settings.questions_table}` AS q
        SET embed_text = (
              SELECT TRIM(CONCAT(
                IFNULL(CONCAT(TRIM(SUBSTR(f.body, 1, {MAX_BODY_CHARS})), {SEPARATOR!r}), ''),
                TRIM(q.question_text)))
              FROM (
                SELECT ARRAY_AGG(body ORDER BY created_at LIMIT 1)[OFFSET(0)] AS body
                FROM `{settings.sfaq_feedback_table}`
                WHERE question_id = q.question_id AND fb_type = 'decision'
                      AND body IS NOT NULL
              ) AS f
            ),
            embedding = [],
            updated_at = CURRENT_TIMESTAMP()
        WHERE EXISTS (
          SELECT 1 FROM `{settings.sfaq_feedback_table}` AS f
          WHERE f.question_id = q.question_id AND f.fb_type = 'decision'
                AND f.body IS NOT NULL
        )
    """


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file", nargs="?", help="SFAQ seed JSON (meta + questions)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--backfill-embeddings", action="store_true",
        help="run ML.GENERATE_EMBEDDING over questions missing an embedding",
    )
    parser.add_argument(
        "--regenerate", action="store_true",
        help="with --backfill-embeddings: recompute EVERY row, not only the un-embedded "
             "ones (needed once, when the embedding target changes)",
    )
    parser.add_argument(
        "--rebuild-embed-text", action="store_true",
        help="既存行の embed_text を「本文＋見出し」で組み直す（embedding は空に戻る）",
    )
    args = parser.parse_args()
    if not args.dry_run:
        # 実データに書く前に、組織設定（org.toml）が埋まっているかを確かめる。
        from waca_loop.org import exit_if_incomplete

        exit_if_incomplete()

    from waca_loop.config import get_settings

    settings = get_settings()

    if args.rebuild_embed_text:
        if not settings.gcp_project:
            print("ERROR: GCP_PROJECT required. Set it in .env.", file=sys.stderr)
            return 2
        if args.dry_run:
            print(_rebuild_embed_text_sql(settings))
            return 0
        from waca_loop.bq import make_client

        job = make_client(settings).query(_rebuild_embed_text_sql(settings))
        job.result()
        print(f"rebuilt embed_text for {job.num_dml_affected_rows} questions.\n"
              f"Next: python scripts/seed_sfaq.py --backfill-embeddings")
        return 0

    if args.backfill_embeddings:
        if not settings.gcp_project:
            print("ERROR: GCP_PROJECT required. Set it in .env.", file=sys.stderr)
            return 2
        from waca_loop.bq import make_client

        sql = _backfill_sql(settings, regenerate=args.regenerate)
        if args.dry_run:
            print(sql)
            return 0
        client = make_client(settings)
        job = client.query(sql)
        job.result()
        print(f"embedded {job.num_dml_affected_rows} questions.")
        return 0

    if not args.json_file:
        parser.error("json_file is required unless --backfill-embeddings/--rebuild-embed-text")
    payload = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    q_rows, fb_rows = _rows(payload)

    if args.dry_run:
        print(f"-- would upsert {len(q_rows)} questions / {len(fb_rows)} feedback rows "
              f"into {settings.questions_table} / {settings.sfaq_feedback_table}:")
        for r in q_rows[:10]:
            print(f"   {r['status']:11s} {r['provenance']:18s} {r['question_text'][:44]}")
        if len(q_rows) > 10:
            print(f"   ... and {len(q_rows) - 10} more")
        return 0

    if not settings.gcp_project:
        print("ERROR: GCP_PROJECT required. Set it in .env.", file=sys.stderr)
        return 2

    from google.cloud import bigquery

    from waca_loop.bq import make_client

    client = make_client(settings)
    ids = [r["question_id"] for r in q_rows]
    for sql in (
        f"DELETE FROM `{settings.sfaq_feedback_table}` WHERE question_id IN UNNEST(@ids)",
        f"DELETE FROM `{settings.questions_table}` WHERE question_id IN UNNEST(@ids)",
    ):
        client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)]
            ),
        ).result()

    for table_ref, rows in (
        (settings.questions_table, q_rows),
        (settings.sfaq_feedback_table, fb_rows),
    ):
        table = client.get_table(table_ref)
        client.load_table_from_json(
            rows, table,
            job_config=bigquery.LoadJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND, schema=table.schema
            ),
        ).result()
    print(f"seeded {len(q_rows)} questions / {len(fb_rows)} feedback rows.\n"
          f"Next: python scripts/seed_sfaq.py --backfill-embeddings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
