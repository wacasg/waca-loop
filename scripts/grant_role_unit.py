#!/usr/bin/env python3
"""owner ユニット（改善要望の見送り）を人に付与する（組織シートが表現できない権限・§5 / §8）。

役割ユニットは部署ではないので組織シートに書けない。ユニットIDは org.toml の [units] で決まる。
`sync_persons_from_sheet.py` は既存の付与を carry-forward するだけなので、**最初の1回を
与える**のはこのスクリプトである。付与できるのは owner だけで、steward と export_admin は
扱わない（理由は下の許可リストのコメント）。

付与は権限そのものなので、既定は dry-run。`--apply` は管理者の承認後に実行する。

Usage:
    python scripts/grant_role_unit.py --unit owner --person p_yamada p_tanaka
    python scripts/grant_role_unit.py --unit owner --person p_yamada --reason "..." --apply
    python scripts/grant_role_unit.py --unit owner --person p_x --revoke --reason "..." --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# 付与してよい役割ユニットの許可リスト。**任意の文字列を付けさせない** — 打ち間違いで
# 誰も持たないユニットを作ると、ガードは通っているのに実際には誰も操作できない、という
# いちばん気づきにくい壊れ方をする。
#
# **steward と export-admin はここから意図的に外してある**。
# 前者は共有層への昇格権限、後者は FLOOR-02 の上限バイパスで、どちらも「owner ユニットを
# 1回付ける」ためのスクリプトの片手間に付けてよい重さではない。BigQuery の書き込み IAM を
# 持つ人が `--apply` 一発で承認者や break-glass 保持者を増やせる状態を作らない。
# それらが要るときは、そのための経路を別に、意図して作ること。
def _role_units() -> dict[str, str]:
    from waca_loop.retrieval.acl import OWNER_UNIT

    return {
        OWNER_UNIT: "改善要望の見送り(wontfix)を決める（SPEC_improvement_requests §8）",
    }


def plan(current: list[str], unit: str, revoke: bool) -> list[str] | None:
    """付与/剥奪後の unit_path。変化が無ければ None（純粋関数）。"""
    units = list(current)
    if revoke:
        if unit not in units:
            return None
        return [u for u in units if u != unit]
    if unit in units:
        return None
    return [*units, unit]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--person", nargs="+", required=True, help="person_id を1つ以上")
    parser.add_argument("--revoke", action="store_true", help="付与ではなく剥奪する")
    parser.add_argument(
        "--reason", help="なぜ付与/剥奪するか（--apply には必須。監査行に残る）"
    )
    parser.add_argument("--apply", action="store_true", help="実際に書き込む（既定は dry-run）")
    args = parser.parse_args()
    if args.apply:
        # 実データに書く前に、組織設定（org.toml）が埋まっているかを確かめる。
        from waca_loop.org import exit_if_incomplete

        exit_if_incomplete()

    roles = _role_units()
    if args.unit not in roles:
        print(f"ERROR: 未知の役割ユニット {args.unit!r}。許可されているのは:")
        for u, why in roles.items():
            print(f"   {u:20s} {why}")
        return 2

    from google.cloud import bigquery

    from waca_loop.bq import make_client
    from waca_loop.config import get_settings

    settings = get_settings()
    client = make_client(settings)
    rows = list(client.query(
        f"SELECT person_id, display_name, unit_path FROM `{settings.persons_table}` "
        "WHERE person_id IN UNNEST(@ids)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("ids", "STRING", args.person)
        ]),
    ).result())
    found = {r["person_id"] for r in rows}
    missing = [p for p in args.person if p not in found]
    if missing:
        # 存在しない person_id に付与したつもりになるのが最悪なので、先に落とす。
        print(f"ERROR: persons に見つかりません: {', '.join(missing)}")
        return 2

    verb = "剥奪" if args.revoke else "付与"
    changes = []
    for r in rows:
        after = plan(list(r["unit_path"] or []), args.unit, args.revoke)
        if after is None:
            print(f"   - {r['person_id']} ({r['display_name']}): 変化なし")
            continue
        print(f"   {verb}: {r['person_id']} ({r['display_name']}) → {after}")
        changes.append((r["person_id"], list(r["unit_path"] or []), after))

    if not changes:
        return 0
    if not args.apply:
        print("   (dry-run: 書き込んでいません。--apply で実行)")
        return 0
    if not args.reason:
        # 権限の変更が理由なしで通る状態を作らない。監査行の中身がこれになる。
        print("ERROR: --apply には --reason が必要です（監査行に残ります）")
        return 2

    for person_id, before, after in changes:
        # 読んでから書くまでの間に sync_persons_from_sheet が走ると、全上書きでは
        # **シートが消したばかりのユニットを復活させる**。
        # 読んだ時点の unit_path を条件に入れて、変わっていたら何も書かない。
        job = client.query(
            f"UPDATE `{settings.persons_table}` "
            "SET unit_path = @units, updated_at = CURRENT_TIMESTAMP() "
            "WHERE person_id = @pid AND unit_path = @before",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("units", "STRING", after),
                bigquery.ArrayQueryParameter("before", "STRING", before),
                bigquery.ScalarQueryParameter("pid", "STRING", person_id),
            ]),
        )
        job.result()
        if job.num_dml_affected_rows == 0:
            print(f"   SKIP: {person_id} は読み込み後に変更されました。もう一度実行してください")
            continue
        _audit(settings, client, person_id, args.unit, verb, args.reason)
        print(f"   {verb}: {person_id}")
    return 0


def _audit(settings, client, person_id: str, unit: str, verb: str, reason: str) -> None:
    """権限変更の痕跡を tool_events に残す。

    break-glass も承認権限も「監査証跡があること」を前提に設計されている。付与そのものが
    どこにも残らないなら、その前提が最初から成立していない。
    """
    from waca_loop.ledger.tool_events import BigQueryToolEventStore

    BigQueryToolEventStore(client, settings).record(
        "role_unit_granted",
        actor_id="grant_role_unit",
        detail=f"{verb} {unit} -> {person_id}: {reason}",
    )


if __name__ == "__main__":
    raise SystemExit(main())
