#!/usr/bin/env python3
"""Bulk-load organization terms from a JSON file into `<DS_GROUNDING>.terms` (§2.2①), then backfill
embeddings with scripts/backfill_embeddings.py.

Uses a BigQuery **load job** (not a streaming insert) so the
subsequent embedding UPDATE (scripts/backfill_embeddings.py, ML.GENERATE_EMBEDDING WHERE
ARRAY_LENGTH(embedding)=0) is not blocked by the streaming buffer. Rows are written with an empty
`embedding` array so the backfill picks them up.

Input JSON: a list of objects like
    {"term_id","term","reading","definition","basis",
     "scope":{"tier","audience","sensitivity"},"owner_unit"?, "status"?, "attributes"?}

Usage:
    python scripts/seed_terms.py terms_seed.json --dry-run   # print rows, write nothing
    python scripts/seed_terms.py terms_seed.json             # load into BigQuery
    python scripts/backfill_embeddings.py                         # then compute embeddings
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


def _rows(terms: list[dict]) -> list[dict]:
    import json as _json
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    out: list[dict] = []
    for t in terms:
        scope = t["scope"]
        out.append(
            {
                "term_id": t["term_id"],
                "term": t["term"],
                "reading": t.get("reading"),
                "definition": t["definition"],
                "basis": t.get("basis"),
                # attributes は JSON 列。load job には文字列で渡す。
                # intent / lifecycle など、専用の列を持たない属性はここに入る。
                "attributes": (
                    _json.dumps(t["attributes"], ensure_ascii=False)
                    if t.get("attributes")
                    else None
                ),
                "scope_tier": scope["tier"],
                "scope_audience": scope["audience"],
                "scope_sensitivity": scope["sensitivity"],
                "owner_unit": t.get("owner_unit"),
                "embedding": [],  # empty (not NULL) so backfill's ARRAY_LENGTH=0 matches it
                "status": t.get("status", "approved"),
                "version": t.get("version", 1),
                "created_at": now,
                "updated_at": now,
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file", help="path to the terms JSON (list of term objects)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run:
        # 実データに書く前に、組織設定（org.toml）が埋まっているかを確かめる。
        from waca_loop.org import exit_if_incomplete

        exit_if_incomplete()

    terms = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    rows = _rows(terms)

    from waca_loop.config import get_settings

    settings = get_settings()

    if args.dry_run:
        print(f"-- would load {len(rows)} terms into {settings.terms_table}:")
        for r in rows:
            print(
                f"   {r['status']:8s} {r['scope_tier']}/{r['scope_audience']}/"
                f"{r['scope_sensitivity']}  {r['term']}  —  {r['definition'][:48]}"
            )
        return 0

    if not settings.gcp_project:
        print("ERROR: GCP_PROJECT required. Set it in .env.", file=sys.stderr)
        return 2

    from google.cloud import bigquery

    from waca_loop.bq import make_client

    client = make_client(settings)
    table = client.get_table(settings.terms_table)  # reuse the table's schema for typing

    # Idempotent upsert: delete any existing rows with these term_ids first, then load. Safe to
    # re-run. DELETE works because we write via a load job (below), not a streaming insert, so
    # rows never sit in the streaming buffer. Scoped strictly to the ids in this file.
    ids = [r["term_id"] for r in rows]
    client.query(
        f"DELETE FROM `{settings.terms_table}` WHERE term_id IN UNNEST(@ids)",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)]
        ),
    ).result()

    job = client.load_table_from_json(
        rows,
        table,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            schema=table.schema,
        ),
    )
    job.result()
    n_draft = sum(1 for r in rows if r["status"] == "draft")
    print(
        f"loaded {len(rows)} terms into {settings.terms_table} "
        f"({n_draft} as draft — review/edit those [要確認] definitions, "
        f"then promote to approved).\n"
        f"Next: python scripts/backfill_embeddings.py   # compute embeddings so search works"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
