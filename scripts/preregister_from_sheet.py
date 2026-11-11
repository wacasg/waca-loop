#!/usr/bin/env python3
"""Pre-register staff in `oauth_identity_map` from the org sheet's email column (§5).

Without this, onboarding is: the user tries to connect → **it fails** → an admin digs their
Google `sub` out of Cloud Logging → inserts a row → the user tries again. That first failure
is not a bug, it is the design: the allowlist is keyed by `sub`, and nobody knows a person's
`sub` until they have logged in once.

This closes that loop from the other side. A person whose address appears in the sheet is
seeded with the sentinel `pending:<person_id>:<email>` (see grounding/persons.py). On their
first login the Google callback rebinds that row to their real `sub` — provided Google asserts
`email_verified` — and they are in. One try, no admin round-trip.

The address alone is never enough: an unverified email claims nothing, the claim consumes the
pending row so it fires at most once, and an address that is not in the sheet has no row to
claim. Removing someone is a delete from `oauth_identity_map`.

順序: `sync_persons_from_sheet.py` を先に流すこと。`persons` に行が無い person_id を
事前登録すると、本人はトークンを得たあと全ツールが 403 になる（fail closed で漏洩は無いが
原因が分かりにくい）。

Usage:
    python scripts/preregister_from_sheet.py --sheet-id <ID> --range 'Sheet1!A1:M300'
    python scripts/preregister_from_sheet.py --sheet-id <ID> --range '...' --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")


def build_rows(people: list[dict]) -> list[dict]:
    """People with an email → pending identity_map rows (pure, offline-testable)."""
    from waca_loop.grounding.persons import normalize_email, pending_sub

    rows = []
    for p in people:
        email = normalize_email(p.get("_email") or "")
        if not email:
            continue   # 氏名しか無い人は事前登録できない（本人の初回ログイン時に管理者登録）
        if not EMAIL_RE.fullmatch(email):
            # The sheet is editable by more people than hold BigQuery write access. An address
            # that is not an address has no business reaching a DML statement.
            raise ValueError(f"不正なメールアドレス（シートを直してください）: {email!r}")
        rows.append(
            {
                "oauth_sub": pending_sub(email, p["person_id"]),
                "person_id": p["person_id"],
                "email": email,
                "display_name": p["display_name"],   # 表示用（テーブルには入れない）
            }
        )
    dupes = {r["email"] for r in rows if [x["email"] for x in rows].count(r["email"]) > 1}
    if dupes:
        # Two people on one address: the claim cannot tell which person is logging in, and
        # would hand one of them the other's unit scope. Refuse rather than guess.
        raise ValueError(f"同じメールアドレスが複数人に付いています: {sorted(dupes)}")
    # person_id は表示名から導出されるので、同姓同名は同じ person_id になる。放置すると
    # (a) BigQuery の MERGE は1つの対象行が複数のソース行に当たると**文ごと落ちる**ため
    # 全員分の適用が止まり、(b) 通ったとしても2人が同じ person_id ＝ 同じユニット権限を
    # 共有する。アドレスと同じく、推測せず拒否する。
    id_dupes = {r["person_id"] for r in rows
                if [x["person_id"] for x in rows].count(r["person_id"]) > 1}
    if id_dupes:
        names = sorted({r["display_name"] for r in rows if r["person_id"] in id_dupes})
        raise ValueError(
            f"同姓同名で person_id が衝突しています（シートで区別してください）: {names}"
        )
    return rows


def merge_sql(table: str) -> str:
    """The pre-registration MERGE (shared with ``register_members.py``)."""
    from waca_loop.grounding.persons import PENDING_SUB_PREFIX

    # MERGE, not INSERT: re-running must not duplicate, and it must never touch a row whose
    # oauth_sub is already a real sub (someone who has logged in since). Values ride as a
    # query PARAMETER — the sheet is editable by more people than hold BigQuery write access,
    # so its cells must never be spliced into SQL text.
    # WHEN MATCHED ... THEN UPDATE — the roster's address for someone
    # who has NOT logged in yet is just a plan, and plans change (e.g. an organization moves
    # every staff address to a second domain). Without this the sheet says one thing and the
    # allowlist another, and everyone is rejected at the door.
    #
    # The `pending:` guard is the whole safety of it: a row whose oauth_sub is a REAL sub is
    # a person who has already logged in, and re-pointing that at an address from a sheet —
    # which more people can edit than hold BigQuery write access — would be a takeover of a
    # live account. Such rows are left untouched, and re-pointing them stays an admin act.
    #
    # The LOCAL-PART guard is the second half, and it is what makes this safe for people who
    # have NOT logged in yet. Without it, a sheet editor (no BigQuery access needed) could
    # point a not-yet-claimed senior person's row at a mailbox they control, wait for the next
    # --apply, log in, and be bound to someone else's person_id — with the audit trail
    # stamping it `preregistration`, i.e. legitimate. Requiring the local part to stay
    # identical means this statement can only ever move an address BETWEEN DOMAINS, which is
    # exactly the migration it exists for, and never to a different person's mailbox.
    sql = f"""MERGE `{table}` AS t
USING (SELECT * FROM UNNEST(@rows)) AS s
ON t.person_id = s.person_id
WHEN MATCHED AND STARTS_WITH(t.oauth_sub, '{PENDING_SUB_PREFIX}') AND t.oauth_sub != s.oauth_sub
     AND SPLIT(t.email, '@')[OFFSET(0)] = SPLIT(s.email, '@')[OFFSET(0)] THEN
  UPDATE SET oauth_sub = s.oauth_sub, email = s.email, updated_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN
  INSERT (oauth_sub, person_id, email, created_at, updated_at)
  VALUES (s.oauth_sub, s.person_id, s.email, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())"""
    return sql


def apply_rows(client, table: str, rows: list[dict]) -> None:
    """Run :func:`merge_sql` with ``rows`` bound as a query parameter (live I/O)."""
    from google.cloud import bigquery

    param = bigquery.ArrayQueryParameter(
        "rows", "STRUCT",
        [
            bigquery.StructQueryParameter(
                None,
                bigquery.ScalarQueryParameter("oauth_sub", "STRING", r["oauth_sub"]),
                bigquery.ScalarQueryParameter("person_id", "STRING", r["person_id"]),
                bigquery.ScalarQueryParameter("email", "STRING", r["email"]),
            )
            for r in rows
        ],
    )
    client.query(
        merge_sql(table), job_config=bigquery.QueryJobConfig(query_parameters=[param])
    ).result()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet-id", help="Sheets API で読む場合（要 spreadsheets スコープ）")
    parser.add_argument("--range", default="A1:M300", dest="cell_range")
    parser.add_argument(
        "--grid-file",
        help="シートを手元に書き出したファイルから読む（.tsv/.csv/.json）。追加の権限が不要",
    )
    parser.add_argument(
        "--impersonate",
        help="このサービスアカウントとしてシートを読む（シートを当該SAに閲覧者で共有しておく）",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="実際に BigQuery へ書き込む（既定は SQL を表示するだけ・管理者承認後に使う）",
    )
    args = parser.parse_args()
    if args.apply:
        # 実データに書く前に、組織設定（org.toml）が埋まっているかを確かめる。
        from waca_loop.org import exit_if_incomplete

        exit_if_incomplete()

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sync_org_sheet", REPO_ROOT / "scripts" / "sync_persons_from_sheet.py"
    )
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)

    if not args.grid_file and not args.sheet_id:
        parser.error("--sheet-id か --grid-file のどちらかが必要です")
    grid = (
        sync.read_grid_file(args.grid_file)
        if args.grid_file
        else sync._read_grid_live(  # noqa: SLF001
            args.sheet_id, args.cell_range, args.impersonate
        )
    )
    people, unmapped = sync.parse_unit_master_sheet(grid)
    if unmapped:
        print("ERROR: シートを直してから再実行してください:")
        for label in unmapped:
            print(f"   - {label}")
        return 2

    rows = build_rows(people)
    without_email = [p["display_name"] for p in people if not (p.get("_email") or "").strip()]
    print(f"事前登録できる人: {len(rows)}名")
    for r in rows:
        print(f"   {r['person_id']:20s} {r['display_name']:12s} {r['email']}")
    if without_email:
        print(f"\nメールアドレスがシートに無い人（事前登録できない）: {len(without_email)}名")
        print("   " + "、".join(without_email))

    from waca_loop.config import get_settings

    settings = get_settings()
    table = settings.identity_map_table
    sql = merge_sql(table)

    if not args.apply:
        print("\n--- 実行する SQL（--apply で書き込み。BQ書込は管理者承認後）---\n")
        print(sql)
        print("\n@rows =")
        for r in rows:
            print(f"  ({r['oauth_sub']!r}, {r['person_id']!r}, {r['email']!r})")
        return 0

    from waca_loop.bq import make_client

    apply_rows(make_client(settings), table, rows)
    print(f"\npre-registered {len(rows)} rows into {table}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
