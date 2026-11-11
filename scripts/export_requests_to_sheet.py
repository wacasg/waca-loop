#!/usr/bin/env python3
"""改善要望を組織シートの「WACA Loop改善要望」タブへ書き出す (SPEC_improvement_requests §5・§10)。

**WACA Loop → シートの一方向。** シート側で状態を書き換えても WACA Loop は読まない
（正は WACA Loop・§1-1）。
`report_improvement_request` / `set_request_status` から best-effort で呼ばれ、本スクリプトが
夜間に取りこぼしを埋める。**シートへの書き込みが失敗しても WACA Loop 側の記録は成功扱い** —
鏡が曇っても本体は止めない。逆にすると外部サービスの都合で組織の記録が落ちる。

本体のロジックは `waca_loop.improvement.sheet_mirror` にある（本番のサーバが即時反映で
同じものを使うため）。このスクリプトはその夜間実行の入口。

**シートは3軸ACLが効かない場所である。** そこへ出してよいのは「全社に見せてよい行」だけ:

- ``body`` 全文は出さない（§5）
- **``owner_unit`` の付いた部内スコープの行は出さない** — body を落としても、件名と実装内容は
  本文と同じ種類の情報を運ぶ（「A部の◯◯取り込みが失敗する」）。WACA Loop では見られない要望を
  シート経由で読める状態にしない
- **``unverified``（ログイン不要フォーム由来・仕分け前）は出さない** — URL を知った社外の
  第三者が任意のテキストを全社閲覧のシートに載せられる直通路になる

Usage:
    python scripts/export_requests_to_sheet.py --dry-run
    python scripts/export_requests_to_sheet.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


from waca_loop.improvement.sheet_mirror import (  # noqa: E402
    SHEET_TAB,
    exporter_rows,
    to_grid,
    write_grid,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet-id", help="書き出し先スプレッドシート")
    parser.add_argument(
        "--impersonate",
        help="このサービスアカウントとしてシートを書く（シートを編集者で共有しておくこと）。"
             "sync_persons_from_sheet.py と同じ認証経路",
    )
    parser.add_argument("--apply", action="store_true", help="実際に書き込む（既定は dry-run）")
    args = parser.parse_args()
    if args.apply:
        # 実データに書く前に、組織設定（org.toml）が埋まっているかを確かめる。
        from waca_loop.org import exit_if_incomplete

        exit_if_incomplete()

    from waca_loop.config import get_settings

    settings = get_settings()
    rows = exporter_rows(settings)
    grid = to_grid(rows)

    print(f"-- {len(rows)} 件 → '{SHEET_TAB}' (A1:H{len(grid)})")
    if not args.apply:
        for line in grid[:6]:
            print("   ", line)
        print("   (dry-run: 書き込んでいません)")
        return 0
    sheet_id = args.sheet_id or settings.improvement_sheet_id
    if not sheet_id:
        parser.error("--sheet-id か IMPROVEMENT_SHEET_ID が必要です")
    write_grid(
        sheet_id, grid, impersonate=args.impersonate or settings.sheets_impersonate_sa or None
    )
    print(f"   '{SHEET_TAB}' に {len(rows)} 件を書き出しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
