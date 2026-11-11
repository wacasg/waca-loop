#!/usr/bin/env python3
"""R3a coverage report (SPEC_ops): 「ポリシー確認したが記録しなかった」疑いの日次集計.

Reads `<DS_LEDGER>.tool_events` and prints, per day per actor:
policy_check / usage_recorded / mismatch(=max(0, checks−writes)) and scope declarations.

Usage:
  python scripts/coverage_report.py --days 7          # query BigQuery (needs creds)
  python scripts/coverage_report.py --days 7 --dry-run  # print the SQL only

The aggregation itself is the pure function ``coverage_mismatch`` in
``waca_loop.ledger.tool_events`` (offline-tested); this script only fetches rows.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waca_loop.ledger.tool_events import (  # noqa: E402
    connection_funnel,
    coverage_mismatch,
    funnel_summary,
    rejected_subject_summary,
)


def build_sql(table: str, days: int) -> str:
    # `detail` is required, not decorative: the usage_reviewed/discard listing below reads it,
    # and that listing is the only operator-facing record of a physically deleted ledger row.
    return f"""
        SELECT occurred_at, tool, actor_id, actor_raw, detail
        FROM `{table}`
        WHERE occurred_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)
        ORDER BY occurred_at
    """


def build_registration_sql(identity_map: str, persons: str) -> str:
    """登録者一覧（未接続を見つけるための**起点**）。

    `tool_events` を起点にすると接続した人しか見えない。登録側から左結合するのが C1 の要点
    （SPEC_coverage §3 C1）。`claimed_at IS NULL` は「案内済みだが未接続」の運用指標（C2）。
    """
    return f"""
        SELECT m.person_id, m.created_at AS registered_on, m.claimed_at, p.status
        FROM `{identity_map}` AS m
        LEFT JOIN `{persons}` AS p USING (person_id)
        ORDER BY m.created_at
    """


#: この人数を下回ると、ファネルの見出しだけで個人が割れる。
#: 抑止ではなく警告に使う — 管理者向けの資格つきスクリプトなので、真の値は見えてよい。
SMALL_ORG_THRESHOLD = 5


def build_ledger_sql(usage_log: str, days: int, withdrawals: str) -> str:
    """R6: 取り下げた行は「この人はAIを使った」の証拠として数えない.

    store 側の2経路に加え、この**3つ目の読み手**も除外する（しないと「取り下げたのに
    集計には残る」）。

    副問い合わせの `IS NOT NULL` は飾りではない。`NOT IN` は右辺に NULL が1つでもあると
    **全行に対して UNKNOWN を返す** ので、記録段が組織まるごと 0 になる。しかも「誰も記録して
    いない」という**もっともらしい見た目**で落ちるので気づけない。いまは DDL の
    `log_id STRING NOT NULL` が防いでいるが、それは別ファイルの宣言であって、この照会は
    スキーマが動いても正しくあるべき。
    """
    return f"""
        SELECT actor_id, actor_raw
        FROM `{usage_log}`
        WHERE occurred_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)
          AND log_id NOT IN (
                SELECT log_id FROM `{withdrawals}` WHERE log_id IS NOT NULL
              )
    """


def rejection_log_command(project: str, days: int) -> list[str]:
    """拒否ログの取得コマンド。**BQ ではなく Cloud Run のログにしかない**（C1・B の検知）。"""
    return [
        "gcloud", "logging", "read",
        'resource.type="cloud_run_revision" '
        'resource.labels.service_name="waca-loop-serve" '
        '"not in oauth_identity_map"',
        f"--project={project}", f"--freshness={int(days)}d",
        "--format=value(timestamp,textPayload,jsonPayload.message)", "--limit=200",
    ]


#: 記録の失敗のマーカー。書き込み失敗時にログへ残す2行はこれで引ける。
#: 台帳の失敗は **BQ には残らない**（残せなかったのが失敗の内容）ので、Cloud Run の
#: ログが唯一の在処。`usage_write_failed` 証跡と両方を見るのは、証跡の書き込み
#: 自体が落ちている場合（CV-1a の警告だけが出る。tests/test_coverage_recording.py）を
#: 取り落とさないため。
WRITE_FAILURE_MARKERS = ("waca-loop-ledger:", "waca-loop-trail:")


def write_failure_log_command(project: str, days: int) -> list[str]:
    """CV-1a/CV-1b のマーカーを Cloud Run のログから引くコマンド。"""
    query = " OR ".join(f'"{m}"' for m in WRITE_FAILURE_MARKERS)
    return [
        "gcloud", "logging", "read",
        'resource.type="cloud_run_revision" '
        'resource.labels.service_name="waca-loop-serve" '
        f"({query})",
        f"--project={project}", f"--freshness={int(days)}d",
        "--format=value(timestamp,textPayload,jsonPayload.message)", "--limit=200",
    ]


def write_failure_summary(events: list[dict], log_lines: list[str]) -> dict:
    """記録の失敗を1か所に集める（純関数・オフラインで検査できる形）。

    `trail` は証跡の失敗、`ledger` は台帳の失敗。`usage_write_failed` は台帳が落ちた回数の
    **証跡側の写し**で、ログ行と一致しないことがある（証跡も落ちていれば写しは残らない）。
    数を並べて出し、どちらが多いかを人が読む — 断定はしない。
    """
    per_marker = {
        m: sum(1 for line in log_lines if m in line) for m in WRITE_FAILURE_MARKERS
    }
    return {
        "usage_write_failed_events": sum(
            1 for r in events if r.get("tool") == "usage_write_failed"
        ),
        "log_lines": per_marker,
    }


def _read_write_failures(project: str, days: int) -> list[str]:
    """記録の失敗のログ行。読めなくてもレポート本体は出す（拒否ログと同じ流儀）。"""
    import subprocess

    try:
        out = subprocess.run(
            write_failure_log_command(project, days),
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 — ログが読めなくても他の集計は出す
        print(f"(記録失敗ログを取得できませんでした: {type(exc).__name__})", file=sys.stderr)
        return []
    if out.returncode != 0:
        # stderr 本文は出さない: gcloud のエラー文は実行中のアカウント名・プロジェクトを
        # そのまま含む。終了コードだけで切り分けはできる。
        print(f"(記録失敗ログを取得できませんでした: gcloud exit={out.returncode})",
              file=sys.stderr)
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def _read_rejections(project: str, days: int) -> list[dict]:
    """拒否ログを読む。失敗しても**レポート本体は出す**（gcloud 認証は切れがち）。"""
    import subprocess

    try:
        out = subprocess.run(
            rejection_log_command(project, days), capture_output=True, text=True, timeout=120
        )
    except Exception as exc:  # noqa: BLE001 — ログが読めなくても他の集計は出す
        print(f"(拒否ログを取得できませんでした: {type(exc).__name__})", file=sys.stderr)
        return []
    if out.returncode != 0:
        print(f"(拒否ログを取得できませんでした: {out.stderr.strip()[:200]})", file=sys.stderr)
        return []
    entries = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        ts = parts[0] if parts else ""
        text = " ".join(parts[1:]) if len(parts) > 1 else line
        sub = ""
        for token in text.replace('"', " ").replace("'", " ").split():
            if token.startswith("sub") or (token.isdigit() and len(token) > 15):
                sub = token
                break
        entries.append({"sub": sub or "?", "occurred_at": ts, "text": text})
    return entries


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    ap.add_argument("--dry-run", action="store_true", help="print SQL and exit")
    ap.add_argument("--no-rejections", action="store_true",
                    help="skip the Cloud Run rejection log (gcloud 不要にしたいとき)")
    ap.add_argument("--no-failure-log", action="store_true",
                    help="skip the Cloud Run 記録失敗ログ。証跡側の件数は出す")
    args = ap.parse_args()

    from waca_loop.config import get_settings

    settings = get_settings()
    sql = build_sql(settings.tool_events_table, args.days)
    reg_sql = build_registration_sql(settings.identity_map_table, settings.persons_table)
    ledger_sql = build_ledger_sql(
        settings.usage_log_table, args.days, settings.usage_withdrawals_table
    )
    if args.dry_run:
        print(sql)
        print(reg_sql)
        print(ledger_sql)
        print("# 拒否ログ:", " ".join(rejection_log_command(settings.gcp_project, args.days)))
        print("# 記録失敗ログ:",
              " ".join(write_failure_log_command(settings.gcp_project, args.days)))
        return 0

    from waca_loop.bq import make_client

    client = make_client(settings)
    rows = [dict(r) for r in client.query(sql).result()]
    registrations = [dict(r) for r in client.query(reg_sql).result()]
    ledger = [dict(r) for r in client.query(ledger_sql).result()]
    all_events = [dict(r) for r in client.query(build_sql(settings.tool_events_table, 3650))
                  .result()]
    report = coverage_mismatch(rows)

    # --- C1-1: 登録者 × 接続実績（未接続が先頭）------------------------------------------
    funnel = connection_funnel(registrations, all_events, ledger)
    never = [f for f in funnel if not f["connected"]]

    # --- R7: 段の集計を先に出す（AC-5）。個票より前に「どこで落ちているか」を見せる ---------
    summary = funnel_summary(funnel)
    print("■ ファネル（登録 → 接続 → 記録）")
    prev = None
    for st in summary["stages"]:
        rate = st["passed_from_prev"]
        arrow = f"  ↓ {rate:.0%}" if rate is not None else ""
        drop = f"  （-{prev - st['people']}名）" if prev is not None and prev > st["people"] else ""
        print(f"    {st['label']:<4} {st['people']:>4}名{arrow}{drop}")
        prev = st["people"]
    e2e = summary["end_to_end_rate"]
    if e2e is not None:
        print(f"    登録から見た通過率: {e2e:.0%}")
    elif summary["stages"][0]["people"] == 0:
        print("    登録者がいません")
    else:
        # 登録者はいるが接続者ゼロ。率が None なのは計算漏れではないと明示する。
        print("    接続者がゼロのため、以降の率は出せません（母数なし）")
    if summary["connected_but_silent"]:
        print(f"    ※ 接続したが1行も記録していない人: {summary['connected_but_silent']}名"
              " — C4 の nudge が届くべき相手")
    if not summary["per_turn_rate_available"]:
        print("    ※ ここでの「記録」は**1行でも書いた人**の数。ターンあたりの記録率は"
              "分母（総ターン数）が観測できないため出していない（SPEC_coverage §3 C5）")
    # 用途の限定を**数字と同じ画面に**置く。この見出しはレポートの冒頭で、切り出して共有
    # されやすい一方、2行下には person_id つきの個票が並ぶ。「台帳 0」が名前と並ぶ形は、
    # そのままでは勤務評価の材料に読めてしまう。
    # 同ファイルの拒否ログ側に既にある「断定しない」の但し書きと同じ流儀。
    print("    ※ この数字は**施策の効果測定**であり、個人の勤務評価には用いない")
    if 0 < summary["stages"][0]["people"] < SMALL_ORG_THRESHOLD:
        # 母数が小さいと、この見出しだけで個人が割れる（「接続したが記録していない人: 1名」＋
        # 誰が繋いでいるかの知識）。抑止はせず、切り出しを止める但し書きを数字に貼り付ける。
        # ダッシュボード等へ出すなら、そちらでは
        # 最小セル抑止（率も含めて伏せる）が要る。
        print(f"    ⚠ 母数が {summary['stages'][0]['people']}名と小さく、この見出しだけで"
              "個人が特定されえます。**この節を切り出して共有しないこと**")
    print()

    print(f"■ 登録 {len(funnel)}名 / 接続あり {len(funnel) - len(never)}名 "
          f"/ **未接続 {len(never)}名**")
    if never:
        print("  ↓ 未接続（登録が古い順）— ここが母数。記録率を上げても、この人たちは動かない")
    print(f"  {'person_id':<24} {'登録':<12} {'案内':<5} {'events':>7} {'台帳':>6} "
          f"{'最終':<12}")
    for f in funnel:
        mark = "  " if f["connected"] else "!!"
        print(f"{mark}{f['person_id']:<24} {f['registered_on'] or '-':<12} "
              f"{'済' if f['claimed'] else '未':<5} {f['events']:>7} {f['ledger_rows']:>6} "
              f"{f['last_event'] or '-':<12}")

    # --- C1-2: 許可リストで弾かれた接続試行（未登録候補）---------------------------------
    if not args.no_rejections:
        rejected = rejected_subject_summary(_read_rejections(settings.gcp_project, args.days))
        if rejected:
            print(f"\n■ 許可リストで弾かれた接続試行: {len(rejected)}件")
            print("  本人は繋ごうとして毎回弾かれている。登録が接続試行の後追いになっている兆候。")
            print("  （値はマスク済み。生値が要るときは Cloud Run のログを直接見る — FLOOR-01）")
            for r in rejected:
                print(f"  !! {r['sub']:<16} {r['attempts']}回 "
                      f"{r['first_day']}〜{r['last_day']}  {r['masked_hint']}")
        else:
            print("\n■ 許可リストで弾かれた接続試行: なし")
    # --- 記録の失敗 ----------------------------------------------------------------------
    skip_failure_log = args.no_failure_log or args.no_rejections
    failure_lines = [] if skip_failure_log else _read_write_failures(
        settings.gcp_project, args.days
    )
    failures = write_failure_summary(rows, failure_lines)
    print("■ 記録の失敗")
    print(f"    usage_write_failed 証跡: {failures['usage_write_failed_events']}件")
    for marker, n in failures["log_lines"].items():
        print(f"    ログ行 {marker:<24} {n}件")
    if skip_failure_log:
        print("    （--no-failure-log / --no-rejections のためログ行は読んでいません）")
    print()
    scope_events = sum(1 for r in rows if r["tool"] == "scope_declared")
    # usage_reviewed/discard は台帳行の物理削除。件数はここでしか追えない。
    discards = [r for r in rows if r["tool"] == "usage_reviewed"
                and str(r.get("detail") or "").startswith("discard:")]
    # R6: 他人の log_id への取り下げ試行は refused として残る。出さなければ検知は机上の話。
    # 読んだ側（withdrawn_usage_read）も同じ理由で出す。
    refused = [r for r in rows if r["tool"] == "usage_withdraw_refused"]
    reads = [r for r in rows if r["tool"] == "withdrawn_usage_read"]

    # "attempts": the event is written before the delete, so a bad log_id also lands here.
    print(f"tool_events last {args.days}d: {len(rows)} events, "
          f"{scope_events} scope declarations, {len(discards)} ledger discard attempts, "
          f"{len(refused)} refused withdrawals, {len(reads)} withdrawn-row reads")
    if refused:
        # 断定しない: この事象は「他人の記録」「存在しない ID」「二重取り下げ」の3つを
        # 区別できない（区別できないこと自体が設計 — 総当たりで他人の id を探れないように
        # 同じ返答にしてある）。再試行するクライアントは二重取り下げを日常的に起こすので、
        # ここで人を名指しで断定すると、ダブルクリックが境界侵害の報告になる。
        print("  ⚠ 取り下げが拒否されました（他人の記録／存在しない ID／二重取り下げのいずれか）:")
        for r in refused[:10]:
            print(f"    {r.get('occurred_at')}  {r.get('actor_id') or r.get('actor_raw')}")
    for r in discards:
        actor = r.get("actor_id") or r.get("actor_raw") or "?"
        print(f"  DISCARD ATTEMPT {r['occurred_at']} by {actor} -> {r.get('detail')}")
    silent = [k for k, b in report.items() if b["silent"]]
    if silent:
        # R3b: retrieved and recorded nothing. Louder than mismatch on purpose — this is a
        # known actor using the corpus entirely outside the ledger.
        print(f"!! 記録なしで検索した actor/日: {len(silent)}件 — {', '.join(sorted(silent))}")
    print(f"{'day/actor':<50} {'checks':>7} {'writes':>7} {'reads':>7} "
          f"{'mismatch':>9} {'silent':>7}")
    for key in sorted(report):
        b = report[key]
        print(f"{key:<50} {b['policy_check']:>7} {b['usage_recorded']:>7} "
              f"{b['retrieval']:>7} {b['mismatch']:>9} {b['silent']:>7}")
    total_mismatch = sum(b["mismatch"] for b in report.values())
    print(f"\nTOTAL mismatch (policy_check without usage_recorded): {total_mismatch}")
    total_silent = sum(b["silent"] for b in report.values())
    print(f"TOTAL silent   (retrieved, recorded nothing): {total_silent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
