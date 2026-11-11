"""正典シート → ポリシー行の同期.

手で書いた写しは正典からずれ、同じ番号が別の業務を指しうる。ここでは、行IDを正典から
機械的に作ること、タブごとに違う列並びを名前で引くこと、そして「シートが表現できない行
（下限・組織全体）を消さない」ことを固定する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sync_policy_from_sheet import (  # noqa: E402
    diff_summary,
    merge_with_carry_forward,
    parse_policy_tab,
)

HEADER = ["No.", "分類", "業務", "現状レベル(0-2)", "現状の使い方・安全な進め方"]


def _grid(rows, *, unit="教材開発部", dept="KYO", header=HEADER):
    return [["部署名", "", unit, "", "部署コード", dept], [], header, *rows]


def test_row_ids_come_from_the_sheet_not_from_a_human():
    """行IDは 部署コード-No.。ゼロ埋めしない（シートの書き方は `KYO-8`）."""
    dept, unit, rows = parse_policy_tab(_grid([
        ["8", "【資料作成】", "研修資料の構成案の作成補助", "1", "章単位に小分け"],
        ["16", "【資料作成】", "研修資料の表現の推敲補助", "1", "段落単位に小分け"],
    ]))
    assert dept == "KYO" and unit == "kyozai"
    assert [r["row_id"] for r in rows] == ["KYO-8", "KYO-16"]
    assert rows[0]["level"] == 1 and rows[0]["is_floor"] is False
    assert rows[0]["line"].startswith("【資料作成】")


def test_columns_are_found_by_name_not_position():
    """タブごとに列の並びが違う。位置で決め打つと全部ずれる."""
    header = ["No.", "分類", "業務", "現状レベル(0-2)", "現状の使い方・安全な進め方",
              "目標レベル(0-2)", "目標", "課題", "担当", "備考", "備考（補足"]
    row = ["1", "【会員対応】", "一般問い合わせ", "1", "公開情報を基に回答案", "2"]
    row += ["", "", "", "", ""]
    _, _, rows = parse_policy_tab(
        _grid([row], unit="サンプル協会事務局", dept="SEC", header=header)
    )
    assert rows[0]["row_id"] == "SEC-1"
    assert rows[0]["effect"] == "公開情報を基に回答案"


def test_the_unit_is_never_guessed():
    """部署名を org_* に対応づけられないなら、推測せず落ちる."""
    with pytest.raises(ValueError, match="推測しない"):
        parse_policy_tab(_grid([["1", "", "何か", "1", ""]], unit="存在しない部"))


def test_a_bad_level_stops_the_sync():
    with pytest.raises(ValueError, match="現状レベル"):
        parse_policy_tab(_grid([["1", "", "何か", "3", ""]]))


def test_rows_after_the_table_are_ignored():
    """表の下の注記行を行として拾わない."""
    _, _, rows = parse_policy_tab(_grid([
        ["1", "", "業務A", "1", ""],
        [],
        ["", "注記", "この表の下には補足を書く"],
    ]))
    assert [r["row_id"] for r in rows] == ["KYO-1"]


def test_floor_and_org_rows_survive_the_sync():
    """シートに対応タブが無い行（下限・組織全体）を消さない。

    人員同期が steward を持ち越すのと同じ理屈。消すと下限が配達されなくなる。
    """
    previous = [
        {"row_id": "FLOOR-01", "scope_unit": "org", "is_floor": True, "line": "床1"},
        {"row_id": "ORG-3", "scope_unit": "org", "level": 2, "line": "組織全体"},
        {"row_id": "KYO-01", "scope_unit": "kyozai", "level": 2, "line": "ゼロ埋めのID"},
    ]
    sheet = [{"row_id": "KYO-1", "scope_unit": "kyozai", "level": 1, "line": "正典のID"}]
    merged = merge_with_carry_forward(sheet, previous)
    ids = [r["row_id"] for r in merged]
    assert "FLOOR-01" in ids and "ORG-3" in ids
    assert "KYO-01" not in ids, "シートに無い部署行が持ち越されている（配達が二重になる）"
    assert "KYO-1" in ids


def test_diff_summary_survives_rows_without_a_level():
    """床行は level を持たない。差分表示がそれで落ちないこと."""
    out = diff_summary(
        [{"row_id": "FLOOR-01", "line": "床1"}],
        [{"row_id": "FLOOR-01", "line": "床1"}, {"row_id": "KYO-1", "level": 1, "line": "新"}],
    )
    assert "追加 1" in out


# --- 承認日 → 配達ステータス--------------------

def test_an_unfilled_approval_date_delivers_as_trial():
    """承認印の無いルールを「承認済み」と名乗って配らない。ただし配りはする。

    draft にすると部署ポリシーが誰にも届かなくなり、「届けることで安全な経路に集める」
    という設計思想に反する。trial なら届いて、かつ確定していないと分かる。
    """
    from sync_policy_from_sheet import status_for

    for unfilled in ("", "（記入）", "(記入)", "—", "   "):
        assert status_for(unfilled) == "trial", f"{unfilled!r} を承認済み扱いしている"


def test_a_filled_approval_date_delivers_as_approved():
    from sync_policy_from_sheet import status_for

    assert status_for("2026-08-20") == "approved"
    assert status_for("2026/08/20 (山田)") == "approved"


def test_the_status_comes_from_the_sheet_row_by_row():
    """WACA Loop は承認しない。人が書いた欄を読むだけ（SPEC_phase1 §9.6 の境界）."""
    grid = [
        ["部署名", "", "教材開発部", "", "部署コード", "KYO"],
        ["版", "", "第2版（案）", "", "承認日／発効日", "（記入）"],
        [],
        HEADER,
        ["1", "【教材作成】", "下書き作成", "1", "そのまま入力可"],
    ]
    _, _, rows = parse_policy_tab(grid)
    assert rows[0]["status"] == "trial"

    grid[1][5] = "2026-08-20"
    _, _, rows = parse_policy_tab(grid)
    assert rows[0]["status"] == "approved"
