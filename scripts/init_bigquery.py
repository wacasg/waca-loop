#!/usr/bin/env python3
"""Create the datasets and apply sql/ddl/*.sql in order (§2).

Usage:
    python scripts/init_bigquery.py --dry-run   # print the resolved DDL, do nothing
    python scripts/init_bigquery.py             # create datasets + run DDL against BigQuery

``<DS_GROUNDING>`` / ``<DS_LEDGER>`` placeholders in the .sql files are substituted from
the DS_* env vars (see .env / .env.example).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DDL_DIR = REPO_ROOT / "sql" / "ddl"
sys.path.insert(0, str(REPO_ROOT / "src"))


def resolve_ddl(settings) -> list[tuple[str, str]]:
    """Return ``(filename, sql)`` for each DDL file with placeholders substituted."""
    out: list[tuple[str, str]] = []
    for path in sorted(DDL_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        sql = sql.replace("<DS_GROUNDING>", f"{settings.gcp_project}.{settings.ds_grounding}")
        sql = sql.replace("<DS_LEDGER>", f"{settings.gcp_project}.{settings.ds_ledger}")
        sql = sql.replace("<DS_FEEDBACK>", f"{settings.gcp_project}.{settings.ds_feedback}")
        sql = sql.replace("<DS_KNOWLEDGE>", f"{settings.gcp_project}.{settings.ds_knowledge}")
        sql = sql.replace(
            "<DS_INTEGRATION>", f"{settings.gcp_project}.{settings.ds_integration}"
        )
        sql = sql.replace("<BQ_CONNECTION>", settings.bq_connection)
        out.append((path.name, sql))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print resolved DDL only")
    args = parser.parse_args()

    from waca_loop.config import get_settings

    settings = get_settings()
    ddl = resolve_ddl(settings)

    if args.dry_run:
        for name, sql in ddl:
            print(f"-- ===== {name} =====")
            print(sql)
        if not settings.gcp_project:
            print("\n# (GCP_PROJECT unset — placeholders show '.dataset'. Set it in .env.)")
        return 0

    if not settings.gcp_project:
        print("ERROR: GCP_PROJECT is required to apply DDL. Set it in .env.", file=sys.stderr)
        return 2

    from waca_loop.bq import make_client

    client = make_client(settings)
    for dataset in (
        settings.ds_grounding,
        settings.ds_ledger,
        settings.ds_feedback,
        settings.ds_knowledge,
        settings.ds_integration,
    ):
        ref = f"{settings.gcp_project}.{dataset}"
        client.create_dataset(ref, exists_ok=True)
        print(f"dataset ready: {ref}")
    # waca_loop_integration holds customer-derived call content and has NO reading store yet, so
    # the dataset ACL is the only control (§3.3 gates need a store to run). Strip the
    # inherited projectReaders grant so project-wide viewers cannot SELECT sensitive rows.
    # Owners/Writers stay for the loader/admin.
    integration = client.get_dataset(f"{settings.gcp_project}.{settings.ds_integration}")
    trimmed = [
        e
        for e in integration.access_entries
        if not (e.entity_type == "specialGroup" and e.entity_id == "projectReaders")
    ]
    if len(trimmed) != len(integration.access_entries):
        integration.access_entries = trimmed
        client.update_dataset(integration, ["access_entries"])
        print(f"dataset ACL tightened: {settings.ds_integration} (projectReaders removed)")
    for name, sql in ddl:
        print(f"applying {name} ...")
        try:
            client.query(sql).result()
        except Exception as exc:  # noqa: BLE001
            # A vector index needs >=5000 rows (§2.3: brute-force VECTOR_SEARCH is fine while few).
            # Don't abort provisioning on an empty terms table — create the index later.
            if "vector_index" in name:
                print(
                    f"  WARN: skipped {name} — create it once terms grow "
                    f"(VECTOR_SEARCH works without an index meanwhile). {type(exc).__name__}: {exc}"
                )
            else:
                raise
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
