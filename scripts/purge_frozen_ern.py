#!/usr/bin/env python3
"""ERN のライフサイクル — frozen の宣言を畳み、期限切れを消す（SPEC_phase3 §5）. **既定 dry-run**.

    python scripts/purge_frozen_ern.py                # 計画を表示するだけ（既定）
    python scripts/purge_frozen_ern.py --apply-freeze # frozen への畳み込みだけ実行
    python scripts/purge_frozen_ern.py --apply-delete # 期限切れの削除まで実行（要確認）

削除は**明示フラグでしか走らない**。畳み込み（status→frozen）と削除を別フラグにしてあるのは、
前者が可逆で後者が不可逆だからで、同じ `--apply` にまとめると「畳みたいだけ」の実行で
消える。

計画のロジックは `waca_loop.ern.lifecycle`（純ロジック・テスト済み）にあり、
このスクリプトは I/O だけを担当する。

**弱点（隠さない）**: 保持期間の起点は `persons.updated_at` の代理で、専用の `frozen_at` 列を
持っていない。別要因で person 行が更新されると時計が延びるので、`overdue` は
「退職から1年経った行」ではなく「その代理で1年を超えた行」である
（SPEC_phase3 §5・§7 の Open item）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply-freeze", action="store_true",
                    help="frozen への畳み込みを実行する（可逆）")
    ap.add_argument("--apply-delete", action="store_true",
                    help="保持期間を過ぎた frozen 宣言を削除する（**不可逆**）")
    args = ap.parse_args()

    from waca_loop.bq import make_client
    from waca_loop.config import get_settings
    from waca_loop.ern.lifecycle import RETENTION_DAYS, plan_purge

    settings = get_settings()
    if not settings.gcp_project:
        print("ERROR: GCP_PROJECT required.", file=sys.stderr)
        return 2
    client = make_client(settings)

    assertions = [
        dict(r) for r in client.query(
            f"SELECT assertion_id, person_id, status FROM `{settings.ern_assertions_table}`"
        ).result()
    ]
    persons = {
        r["person_id"]: {"status": r["status"], "updated_at": r["updated_at"]}
        for r in client.query(
            f"SELECT person_id, status, updated_at FROM `{settings.persons_table}`"
        ).result()
    }

    plan = plan_purge(assertions, persons, retention_days=RETENTION_DAYS)
    print(f"assertions={len(assertions)} persons={len(persons)} "
          f"（保持 {RETENTION_DAYS} 日）")
    print(f"  frozen へ畳む: {len(plan.to_freeze)}")
    print(f"  期限切れ（削除候補）: {len(plan.overdue)}")
    print(f"  孤児（名簿に無い person_id）: {len(plan.orphaned)}")
    if plan.orphaned:
        print("    ※ 孤児は『退職して1年』ではなく『名簿と宣言がずれている』です。")
        print("      消す前に、なぜずれたかを見てください。")
    if plan.is_empty:
        print("\nすることはありません。")
        return 0

    if not (args.apply_freeze or args.apply_delete):
        print("\n（dry-run。実行するには --apply-freeze / --apply-delete）")
        return 0

    from google.cloud import bigquery

    def _ids(name: str, ids: tuple[str, ...]) -> bigquery.QueryJobConfig:
        return bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter(name, "STRING", list(ids))]
        )

    if args.apply_freeze and plan.to_freeze:
        client.query(
            f"UPDATE `{settings.ern_assertions_table}` "  # noqa: S608
            "SET status = 'frozen', updated_at = CURRENT_TIMESTAMP() "
            "WHERE assertion_id IN UNNEST(@ids)",
            job_config=_ids("ids", plan.to_freeze),
        ).result()
        print(f"\n{len(plan.to_freeze)} 件を frozen にしました。")

    if args.apply_delete and plan.overdue:
        client.query(
            f"DELETE FROM `{settings.ern_assertions_table}` "  # noqa: S608
            "WHERE assertion_id IN UNNEST(@ids)",
            job_config=_ids("ids", plan.overdue),
        ).result()
        print(f"{len(plan.overdue)} 件を削除しました（不可逆）。")

    if plan.orphaned:
        print(f"\n孤児 {len(plan.orphaned)} 件は**触っていません**（人の確認が要ります）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
