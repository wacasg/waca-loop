#!/usr/bin/env python3
"""Load AI-policy rows into `<DS_FEEDBACK>.policy` from JSON (SPEC_phase1 §9).

These are the **human-authored** rows WACA Loop only serves (配達係); WACA Loop never
approves/edits them (境界).
Idempotent upsert (delete-by-row_id then load job), like seed_terms.py — safe to re-run.

Input JSON: list of {row_id, scope_unit, level?, line, effect?, is_floor?, status?,
version?, trigger_kinds?} — trigger_kinds lists the data_kinds that fire this row (§9.4).

レベル制: ``level`` is the 採用レベル (0=未使用/1/2) for
business rows; floor rows carry no level (null) and are governed by is_floor. Each business
row's level is an explicit 管理者 authorization. A ``color`` key in the seed is ignored.

Usage:
    python scripts/seed_policy.py policy_seed.json --dry-run
    python scripts/seed_policy.py policy_seed.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


def _validate_levels(rules: list[dict]) -> None:
    """Level-migration seed contract: floors carry no level;
    business rows carry an explicit 0/1/2 — a silently-null business row would vanish
    from the level-reading server's by_level grouping."""
    for r in rules:
        level = r.get("level")
        if r.get("is_floor"):
            if level is not None:
                raise SystemExit(f"seed: floor row {r['row_id']} must not carry level")
        elif level not in (0, 1, 2):
            raise SystemExit(
                f"seed: business row {r['row_id']} needs level 0/1/2 (got {level!r})"
            )


def _rows(rules: list[dict]) -> list[dict]:
    from datetime import UTC, datetime

    _validate_levels(rules)
    now = datetime.now(UTC).isoformat()
    out: list[dict] = []
    for r in rules:
        out.append(
            {
                "row_id": r["row_id"],
                "scope_unit": r["scope_unit"],
                "level": r.get("level"),  # None for floors; 0/1/2 for business rows
                "line": r["line"],
                "effect": r.get("effect", ""),
                "is_floor": bool(r.get("is_floor", False)),
                "status": r.get("status", "approved"),
                "version": int(r.get("version", 1)),
                "trigger_kinds": list(r.get("trigger_kinds") or []),
                "created_at": now,
                "updated_at": now,
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--prune", action="store_true",
        help="JSON に無い row_id を表から削除する。正典（シート）から消えた行を消すために要る "
             "— row_id 単位の upsert だけでは**消えた行が残り続ける**（行ID体系を"
             "入れ替えると、古い ID の行が新しい ID の行と並んで配達される）",
    )
    args = parser.parse_args()
    if not args.dry_run:
        # 実データに書く前に、組織設定（org.toml）が埋まっているかを確かめる。
        from waca_loop.org import exit_if_incomplete

        exit_if_incomplete()

    rules = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    rows = _rows(rules)

    from waca_loop.config import get_settings

    settings = get_settings()

    if args.dry_run:
        print(f"-- would upsert {len(rows)} policy rows into {settings.policy_table}:")
        for r in rows:
            floor = " 禁止床" if r["is_floor"] else ""
            level = "床" if r["level"] is None else f"L{r['level']}"
            print(f"   {r['row_id']:9s} {level:3s} {r['scope_unit']:14s}{floor}  {r['line']}")
        if args.prune:
            print(f"-- and would DELETE every row whose row_id is not among the {len(rows)} above")
        return 0

    if not settings.gcp_project:
        print("ERROR: GCP_PROJECT required. Set it in .env.", file=sys.stderr)
        return 2

    from google.cloud import bigquery

    from waca_loop.bq import make_client

    client = make_client(settings)
    table = client.get_table(settings.policy_table)
    ids = [r["row_id"] for r in rows]
    where = "row_id NOT IN UNNEST(@ids)" if args.prune else "row_id IN UNNEST(@ids)"
    deleted = client.query(
        f"DELETE FROM `{settings.policy_table}` WHERE {where}",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)]
        ),
    )
    deleted.result()
    if args.prune:
        print(f"pruned {deleted.num_dml_affected_rows or 0} row(s) absent from the seed.")
    client.query(
        f"DELETE FROM `{settings.policy_table}` WHERE row_id IN UNNEST(@ids)",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)]
        ),
    ).result()
    client.load_table_from_json(
        rows, table,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND, schema=table.schema
        ),
    ).result()
    print(f"seeded {len(rows)} policy rows into {settings.policy_table}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
