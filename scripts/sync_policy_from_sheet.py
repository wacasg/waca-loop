#!/usr/bin/env python3
"""ポリシーシートの部署タブ → ポリシー行 JSON（SPEC_phase1 §9 配達係の入口）.

**なぜ要るか**: 人員・組織と同じく、ポリシー行もシート→WACA Loop で同期する。手で書いた
写しの JSON は、シートを直しても WACA Loop に反映されず、ずれても誰も気づかない。行数が
合わなくなったり、**同じ番号が別の業務を指したり**すると、「部署コード-No. で呼ぶ」という
運用の前提が崩れる。

行ID は正典から**機械的に**作る（`部署コード-No.`）。人が付け替える余地を残さないので、
シートを直して流し直せば必ず一致する。

正典はシート（組織ごとに `--sheet-id` で指定する）。

Usage:
    # オフライン（保存したグリッドで検証）
    python scripts/sync_policy_from_sheet.py --grid-file grid.json --dry-run

    # ライブ（Sheets API・要 spreadsheets.readonly スコープ）
    python scripts/sync_policy_from_sheet.py --sheet-id <ID> \
        --tabs '事務局,教材開発部' --dry-run

    # 書き出し（このスクリプトは BQ に触らない。投入は seed_policy.py）
    python scripts/sync_policy_from_sheet.py --sheet-id <ID> --tabs '...' \
        --out policy_seed.json
    python scripts/seed_policy.py policy_seed.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# シート→WACA Loop の**部署コード以外**の対応（単位名 → ユニットID）は org.toml の [unit_map]
# が正典（人員同期と共有）。ここで書き直すと二重管理になり、片方だけ直る事故が起きる。
from sync_persons_from_sheet import UNIT_MAP  # noqa: E402

from waca_loop.org import get_org_config  # noqa: E402

# 列見出し → 内部キー。**位置ではなく名前で引く**（タブごとに「分類」列の有無が違っても
# 列がずれないように）。
COLUMNS = {
    "no": ("No.", "No"),
    "category": ("分類",),
    "line": ("業務",),
    "level": ("現状レベル(0-2)", "現状レベル（0-2）", "現状レベル"),
    "effect": ("現状の使い方・安全な進め方",),
    "target_level": ("目標レベル(0-2)", "目標レベル（0-2）", "目標レベル"),
    "share": ("LL共有範囲",),
}

# 「未記入」を表す書式。シートのテンプレートが最初から入れている文字列も未承認とみなす。
UNFILLED = ("", "（記入）", "(記入)", "-", "—")

# 部署タブから作った行だけを入れ替える。これ以外（下限・組織全体）は
# シートに対応タブが無く、組織の AI 利用方針の文書から起草するものなので持ち越す。
CARRY_FORWARD_PREFIXES = ("FLOOR-", "ORG-")


def _norm(s: object) -> str:
    return str(s or "").strip()


def _header_index(grid: list[list[str]]) -> int:
    """「No.」で始まる見出し行の位置。見つからなければ -1。"""
    for i, row in enumerate(grid):
        if row and _norm(row[0]) in COLUMNS["no"]:
            return i
    return -1


def _column_map(header: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, names in COLUMNS.items():
        for j, cell in enumerate(header):
            if _norm(cell) in names:
                out[key] = j
                break
    return out


def _find_labelled(grid: list[list[str]], label: str) -> str:
    """「部署コード」「部署名」のように、ラベルの**右隣で最初に埋まっているセル**を返す。"""
    for row in grid:
        for j, cell in enumerate(row):
            if _norm(cell) == label:
                for k in range(j + 1, len(row)):
                    if _norm(row[k]):
                        return _norm(row[k])
    return ""


def _unit_for(unit_name: str) -> str | None:
    """部署名 → ユニットID。表記ゆれ（全角括弧・組織名の接頭辞）だけ吸収し、**推測はしない**。"""
    base = unit_name.replace("（", "(").replace("）", ")").strip()
    stripped = base.removeprefix(get_org_config().name).strip()
    for cand in (base, base.lower(), stripped, stripped.lower()):
        if cand in UNIT_MAP:
            return UNIT_MAP[cand]
    return None


def status_for(approved_on: str) -> str:
    """承認日／発効日の記入有無から配達ステータスを決める。

    `approved` と `trial` はどちらも配達されるが、`trial` は「試行中」と分かる形で届く
    (`policy/acl.py` の servable statuses)。**承認日が空欄なら trial** — 承認印の無い
    ルールを「承認済み」と名乗って配ると、組織が定めた承認の手続きが
    意味を失う。かといって配らない（draft）と、届けることで安全な経路に集める
    という設計思想そのものに反する。届けたうえで確定していないと伝える、が答えである。

    **この判定は人が書いた欄をそのまま読むだけで、WACA Loop は承認しない。** 承認日を書き込むのは
    人の仕事（SPEC_phase1 §9 の境界: WACA Loop がシートに書くのは列 J だけ）。

    誰の承認を承認日とみなすかは組織が決める。このスクリプトは承認日の欄が埋まって
    いるかだけを見る。下限（FLOOR-*）は部署タブでは扱わず、組織全体の絶対ルールとして
    組織の AI 利用方針の文書に従う。
    """
    return "trial" if _norm(approved_on) in UNFILLED else "approved"


def parse_policy_tab(grid: list[list[str]]) -> tuple[str, str, list[dict]]:
    """1つの部署タブ → (部署コード, scope_unit, ポリシー行のリスト)。

    行ID は ``部署コード-No.``。シートの番号の書き方（例 `KYO-8`）に合わせ、**ゼロ埋めしない**。
    """
    dept = _find_labelled(grid, "部署コード")
    unit_name = _find_labelled(grid, "部署名")
    if not dept:
        raise ValueError("部署コードが見つからない（タブの体裁が想定と違う）")
    unit = _unit_for(unit_name)
    if not unit:
        raise ValueError(
            f"部署名 {unit_name!r} をユニットIDに対応づけられない。"
            "推測しない — org.toml の [unit_map] に追加すること"
        )

    approved_on = _find_labelled(grid, "承認日／発効日") or _find_labelled(grid, "承認日/発効日")
    status = status_for(approved_on)

    h = _header_index(grid)
    if h < 0:
        raise ValueError("見出し行（No.）が見つからない")
    cols = _column_map(grid[h])
    for required in ("no", "line", "level"):
        if required not in cols:
            raise ValueError(f"必須列 {COLUMNS[required][0]!r} がタブに無い")

    rows: list[dict] = []
    for raw in grid[h + 1:]:
        if not raw:
            continue
        no = _norm(raw[cols["no"]] if cols["no"] < len(raw) else "")
        if not no.isdigit():
            continue   # 空行・表の下の注記行は行として扱わない
        line = _norm(raw[cols["line"]] if cols["line"] < len(raw) else "")
        if not line:
            continue
        level_raw = _norm(raw[cols["level"]] if cols["level"] < len(raw) else "")
        if level_raw not in ("0", "1", "2"):
            raise ValueError(f"{dept}-{no}: 現状レベルが 0/1/2 でない（{level_raw!r}）")
        effect = _norm(
            raw[cols["effect"]] if "effect" in cols and cols["effect"] < len(raw) else ""
        )
        category = _norm(
            raw[cols["category"]] if "category" in cols and cols["category"] < len(raw) else ""
        )
        rows.append({
            "row_id": f"{dept}-{no}",
            "scope_unit": unit,
            "level": int(level_raw),
            "line": f"{category}{line}" if category else line,
            "effect": effect,
            "is_floor": False,
            "status": status,
            "version": 1,
            "trigger_kinds": [],
        })
    return dept, unit, rows


def merge_with_carry_forward(sheet_rows: list[dict], previous: list[dict]) -> list[dict]:
    """シート由来の行＋持ち越し行（下限・組織全体）。

    持ち越すのは ``FLOOR-*`` / ``ORG-*`` — シートに対応タブが無く、組織の AI 利用方針の
    文書から起草するもの。人員同期が steward ユニット等を
    持ち越すのと同じ理屈で、**シートが表現できないものを消さない**。
    """
    carried = [r for r in previous if r["row_id"].startswith(CARRY_FORWARD_PREFIXES)]
    return carried + sheet_rows


def diff_summary(old: list[dict], new: list[dict]) -> str:
    old_by = {r["row_id"]: r for r in old}
    new_by = {r["row_id"]: r for r in new}
    added = sorted(set(new_by) - set(old_by))
    removed = sorted(set(old_by) - set(new_by))
    def _shape(r: dict) -> tuple:
        # status を入れる。承認↔試行の切替は**配達の意味が変わる**変更で、
        # 差分に出ないまま適用されると誰も気づけない。
        return (r.get("line"), r.get("level"), r.get("status", "approved"))

    changed = sorted(
        rid for rid in set(old_by) & set(new_by) if _shape(old_by[rid]) != _shape(new_by[rid])
    )
    lines = [
        f"既存 {len(old)} 行 → 新 {len(new)} 行",
        f"  追加 {len(added)}: {', '.join(added) or '—'}",
        f"  削除 {len(removed)}: {', '.join(removed) or '—'}",
        f"  変更 {len(changed)}: {', '.join(changed) or '—'}",
    ]
    for rid in changed:
        lines.append(f"    {rid}")
        o, n = old_by[rid], new_by[rid]
        lines.append(f"      旧: L{o.get('level')} [{o.get('status', 'approved')}] {o.get('line')}")
        lines.append(f"      新: L{n.get('level')} [{n.get('status', 'approved')}] {n.get('line')}")
    return "\n".join(lines)


def read_grid_file(path: str) -> dict[str, list[list[str]]]:
    """{タブ名: グリッド} の JSON（オフライン検証・テスト用）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_live(
    sheet_id: str, tabs: list[str], impersonate: str | None
) -> dict[str, list[list[str]]]:
    from sync_persons_from_sheet import _read_grid_live  # noqa: PLC0415

    return {t: _read_grid_live(sheet_id, f"'{t}'!A1:N100", impersonate) for t in tabs}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sheet-id", help="正典スプレッドシートのID")
    ap.add_argument("--tabs", help="部署タブ名のカンマ区切り")
    ap.add_argument("--grid-file", help="保存したグリッド JSON（オフライン検証）")
    ap.add_argument("--impersonate", help="サービスアカウントの代理先")
    ap.add_argument("--previous", default="policy_seed.json",
                    help="持ち越し元（FLOOR-*/ORG-* を引き継ぐ）")
    ap.add_argument("--out", help="書き出し先 JSON（省略時は書かない）")
    ap.add_argument("--dry-run", action="store_true", help="差分だけ出して書かない")
    args = ap.parse_args()
    if not args.dry_run:
        # 実データに書く前に、組織設定（org.toml）が埋まっているかを確かめる。
        from waca_loop.org import exit_if_incomplete

        exit_if_incomplete()

    if args.grid_file:
        grids = read_grid_file(args.grid_file)
    elif args.sheet_id and args.tabs:
        tabs = [t.strip() for t in args.tabs.split(",")]
        grids = _read_live(args.sheet_id, tabs, args.impersonate)
    else:
        ap.error("--grid-file か（--sheet-id と --tabs）のどちらかが要る")

    sheet_rows: list[dict] = []
    for tab, grid in grids.items():
        dept, unit, rows = parse_policy_tab(grid)
        st = rows[0]["status"] if rows else "—"
        note = "（承認日が未記入 → 試行中として配達）" if st == "trial" else ""
        print(f"[{tab}] 部署コード={dept} unit={unit} 行数={len(rows)} status={st}{note}",
              file=sys.stderr)
        sheet_rows.extend(rows)

    ids = [r["row_id"] for r in sheet_rows]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise SystemExit(f"行IDが重複している: {sorted(dupes)}")

    prev_path = Path(args.previous)
    previous = json.loads(prev_path.read_text(encoding="utf-8")) if prev_path.exists() else []
    merged = merge_with_carry_forward(sheet_rows, previous)

    print(diff_summary(previous, merged))
    if args.dry_run or not args.out:
        return 0
    Path(args.out).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n書き出した: {args.out}（投入は scripts/seed_policy.py）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
