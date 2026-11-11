#!/usr/bin/env python3
"""Google フォームの回答タブ → 改善要望 (SPEC_improvement_requests §3.2)。

フォームは**ログイン不要**なので本人性が無い。したがって取り込んだ行は
`channel="form"` / `status="unverified"` で入り、事務局が目で通して `open` に上げるまで
開発者の作業キュー（§4 の提示）には並ばない。**ログイン不要の入口を作業キューに直結させない**
——これがこの経路の設計上いちばん効く一点である。

フォームから WACA Loop へ直接書く経路は作らない（外向きの口を増やさない）。
定期実行（cron・Cloud Scheduler など）から呼ぶ。

Usage:
    python scripts/ingest_improvement_form.py --grid-file responses.tsv --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# 同時アクセスのアラート閾値の既定。
# 超えたら**取り込みを止めて知らせる**。行を捨てもしないし、全件通しもしない。
# 実行時は `Settings.form_burst_threshold` を使う（§3.2「閾値は config 値にする」）。
DEFAULT_BURST_THRESHOLD = 30

# フォームの設問見出し → 内部キー。見出しは日本語・英語併記なので部分一致で拾う。
_COL_HINTS = {
    "kind": ("報告の種類", "nature of your submission"),
    "title": ("タイトル", "title for your report"),
    "body": ("詳細", "issue or suggestion"),
    "steps": ("再現手順", "steps to reproduce"),
    "env": ("動作環境", "environment details"),
    "name": ("お名前", "your name"),
}

_KIND_MAP = {"不具合": "bug", "bug": "bug", "改善": "improvement",
             "improvement": "improvement", "その他": "other", "other": "other"}


def _match(header: str, hints: tuple[str, ...]) -> bool:
    h = header.lower()
    return any(hint.lower() in h for hint in hints)


def _kind_of(cell: str) -> str:
    """種別セル → kind。**判別できなければ `other`。捨てない・寄せない**（§1-9）。"""
    low = (cell or "").lower()
    for token, kind in _KIND_MAP.items():
        if token.lower() in low:
            return kind
    return "other"


def parse_form_grid(grid: list[list[str]]) -> list[dict]:
    """フォーム回答の grid → 要望 dict のリスト（純粋関数・オフライン検証可）。

    再現手順・動作環境は**列を増やさず `body` に見出し付きで連結する**（§7.1）。
    改善要望のときは両方空欄になるので、列にすると空欄ばかりの列が並ぶだけになる。

    お名前が空欄なら匿名（`reporter_label="匿名"`）。**「匿名にしますか」とは問わない** —
    問うと実名で出してよい人まで匿名を選び、報告先が消える（§7.1）。
    """
    if not grid:
        return []
    header = [(c or "").strip() for c in grid[0]]
    idx = {
        key: next((i for i, h in enumerate(header) if _match(h, hints)), None)
        for key, hints in _COL_HINTS.items()
    }

    def cell(row: list[str], key: str) -> str:
        i = idx.get(key)
        return (row[i] or "").strip() if i is not None and i < len(row) else ""

    out = []
    for row in grid[1:]:
        if not any((c or "").strip() for c in row):
            continue
        title = cell(row, "title")
        body = cell(row, "body")
        if not title and not body:
            continue
        for label, key in (("再現手順", "steps"), ("動作環境", "env")):
            extra = cell(row, key)
            if extra:
                body = f"{body}\n\n【{label}】\n{extra}"
        name = cell(row, "name")
        out.append({
            "kind": _kind_of(cell(row, "kind")),
            # 件名が空でも **body の先頭を昇格させない**。件名はシートに出るので、
            # ログイン不要の入口に書かれた本文の冒頭（顧客名やメールが混じりうる）が
            # そのまま全社シートへ載る道になる。
            "title": title or "（件名なし）",
            "body": body,
            "channel": "form",
            "status": "unverified",     # ← 本人性が無い。作業キューに直結させない
            "reporter_id": None,        # フォームでは person を引き当てない（検証できない）
            "reporter_label": name or "匿名",
        })
    return out


def burst_refusal(
    rows: list[dict], threshold: int = DEFAULT_BURST_THRESHOLD
) -> str | None:
    """一度に大量が来ていたら、取り込まずに知らせる理由文字列を返す（§3.2）。"""
    if len(rows) > threshold:
        return (
            f"フォーム由来が一度に {len(rows)} 件（閾値 {threshold}）。"
            "取り込みを止めました。中身を確認してから再実行してください"
        )
    return None


def _trail_burst(settings, count: int) -> None:
    """流入超過の痕跡を tool_events に1行残す（内容は件数のみ・本文は残さない）。"""
    try:
        from waca_loop.bq import make_client
        from waca_loop.ledger.tool_events import BigQueryToolEventStore

        BigQueryToolEventStore(make_client(settings), settings).record(
            "form_ingest_burst_refused",
            actor_id="ingest_improvement_form",
            detail=str(count),
        )
    except Exception as exc:  # noqa: BLE001 — 痕跡が残せなくても取り込みは止めたまま
        print(f"   (警告: 痕跡を残せませんでした: {exc})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid-file", required=True, help="回答タブの書き出し (.tsv/.csv/.json)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        # 実データに書く前に、組織設定（org.toml）が埋まっているかを確かめる。
        from waca_loop.org import exit_if_incomplete

        exit_if_incomplete()

    from scripts.sync_persons_from_sheet import read_grid_file  # noqa: PLC0415

    from waca_loop.config import get_settings

    settings = get_settings()
    rows = parse_form_grid(read_grid_file(args.grid_file))
    refusal = burst_refusal(rows, settings.form_burst_threshold)
    if refusal:
        # 止めるだけでは「静かに落ちた」のと区別がつかない。痕跡を tool_events に残す。
        # 残せなくても取り込みは止めたまま。
        _trail_burst(settings, len(rows))
        print(f"ERROR: {refusal}")
        return 2
    print(f"-- {len(rows)} 件（すべて status=unverified で入ります）")
    for r in rows[:5]:
        print(f"   [{r['kind']}] {r['title']}  <{r['reporter_label']}>")
    if not args.apply:
        print("   (dry-run: 書き込んでいません)")
        return 0
    from waca_loop.bq import make_client  # noqa: I001
    from waca_loop.config import get_settings
    from waca_loop.improvement.requests import BigQueryRequestStore
    from waca_loop.models import ImprovementRequest, RequestKind, RequestStatus

    settings = get_settings()
    store = BigQueryRequestStore(make_client(settings), settings)
    from waca_loop.ingest.anonymize import scrub_text
    from waca_loop.models import ScopeAudience, ScopeSensitivity, ScopeTags, ScopeTier

    for r in rows:
        store.add(ImprovementRequest(
            kind=RequestKind(r["kind"]),
            # ログイン不要の入口＝内容を統制できない入口。**格納前に必ず scrub する**
            # （§2 最終項）。注意書きは守ってもらう前提の紙で、これが実際の歯止め。
            title=scrub_text(r["title"]).text,
            body=scrub_text(r["body"]).text,
            channel="form", status=RequestStatus.UNVERIFIED,
            reporter_id=None, reporter_label=r["reporter_label"],
            # フォーム由来は部門の文脈を持たない（誰の所属か検証できない）。org 階層で受ける。
            scope=ScopeTags(
                ScopeTier.ORG, ScopeAudience.INTERNAL, ScopeSensitivity.NORMAL
            ),
        ))
    print(f"   {len(rows)} 件を投入しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
