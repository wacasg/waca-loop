"""改善要望を組織シートへ映す（SPEC_improvement_requests §5・§10）。

**WACA Loop が正、シートは鏡。** 一方向にしか流れない。ここがパッケージ側にあるのは、
デプロイ先のサーバ（Cloud Run）が即時反映のために import するからである — スクリプト側に
置くと、`ServerContext.request_sheet_mirror` に挿すものがサーバに存在せず、
「即時反映」が黙って何もしない機能になる。

Google のライブラリは**関数の中で import する**（純粋ロジック境界・CLAUDE.md）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from waca_loop.config import Settings

SHEET_TAB = "WACA Loop改善要望"
HEADER = ["受付ID", "種別", "件名", "起案者", "状態", "実装内容", "上がった日", "更新日"]

_KIND_JA = {"bug": "不具合", "improvement": "改善要望", "other": "その他"}
_STATUS_JA = {
    "unverified": "未確認", "open": "受付", "designing": "設計中",
    "implemented": "実装済", "released": "リリース済", "wontfix": "見送り",
}

# 置換で消す範囲の上限。これを超えると古い行が残り、「置換であって追記ではない」という
# 約束が黙って壊れる。超えたら気づけるよう、書き出し側で明示的に落とす。
MAX_SHEET_ROWS = 10000


def exportable(rows: list[dict]) -> list[dict]:
    """シートに出してよい行だけに絞る（純粋関数）。

    ACL を通した結果をそのまま全社シートへ平坦化しない。ゲートは通っていても、
    **出口で可視域を捨てない**のがここの仕事である:

    - ``owner_unit`` の付いた部内スコープの行は出さない。``body`` を落としても、件名と
      実装内容は本文と同じ種類の情報を運ぶ（「A部の◯◯取り込みが失敗する」）
    - ``unverified``（ログイン不要フォーム由来・仕分け前）は出さない。社外の第三者が
      任意のテキストを全社閲覧のシートに載せられる直通路になる
    """
    return [
        r
        for r in rows
        if not (r.get("owner_unit") or None) and r.get("status") != "unverified"
    ]


def reporter_cell(row: dict) -> str:
    """起案者の表示。**検証済みかどうかを表示に残す**。

    WACA Loop 経由は OAuth で本人性が担保されている。フォームの名前欄は自由記入で、他人の名前を
    書くこともできる。両方を同じ見た目にすると「匿名」も「記名」も意味を失う。
    """
    label = row.get("reporter_label")
    if row.get("reporter_id"):
        return label or "記名（WACA Loop経由）"
    if label and label != "匿名":
        return f"（自己申告）{label}"
    return "匿名"


# タブの末尾に毎回書き足す注記。**タブごと置換する**ので、人が手で書いた注記は次の書き出しで
# 消える。だから注記も生成物の一部にする — そうしないと「状態列を直したのに
# 戻った」という混乱を防ぐ言葉が、その混乱の原因そのものによって消える。
FOOTER_NOTE = (
    "※ この表は WACA Loop が書き出しています。"
    "ここを直接編集しても WACA Loop には戻りません（正は WACA Loop 側・毎日23時に上書き）。"
)


def to_grid(rows: list[dict]) -> list[list[str]]:
    """要望行 → シートの2次元配列（純粋関数）。

    **`body` は列に持たない**（§5）。シートは3軸ACLが効かない場所である。
    """
    grid = [list(HEADER)]
    for r in rows:
        grid.append([
            r.get("request_id", ""),
            _KIND_JA.get(r.get("kind", ""), r.get("kind", "")),
            r.get("title", ""),
            reporter_cell(r),
            _STATUS_JA.get(r.get("status", ""), r.get("status", "")),
            r.get("resolution") or "",
            str(r.get("created_at", ""))[:10],
            str(r.get("updated_at", ""))[:10],
        ])
    grid.append([""] * len(HEADER))
    grid.append([FOOTER_NOTE, *[""] * (len(HEADER) - 1)])
    return grid


def exporter_rows(settings: Settings) -> list[dict]:
    """シートに出す行を BigQuery から取る。

    横断視界が要る（部門をまたいだ一覧を作るのが仕事）ので、**ACL を外すのではなく
    WACA Loop オーナーとして通す** — ゲートは通常と同じ関数を通り、そのうえで
    :func:`exportable` が出口を絞る。
    """
    from waca_loop.bq import make_client
    from waca_loop.improvement.requests import BigQueryRequestStore
    from waca_loop.models import Viewer
    from waca_loop.retrieval.acl import OWNER_UNIT

    exporter = Viewer(
        subject="export", person_id="p_export", is_org_member=True,
        units=frozenset({OWNER_UNIT}),
    )
    store = BigQueryRequestStore(make_client(settings), settings)
    return exportable(store.list(viewer=exporter))


def write_grid(
    sheet_id: str, grid: list[list[str]], *, impersonate: str | None = None
) -> None:
    """タブを丸ごと置き換える（WACA Loop → シートの一方向・§5）。

    追記ではなく置換にするのは、WACA Loop が正だから。シート側で誰かが状態列を書き換えても、
    次の書き出しで WACA Loop の値に戻る — それが「鏡」の意味である。

    認証は `sync_persons_from_sheet.py` と同じ2経路。ユーザADCは spreadsheets スコープを
    取れないので（Google が Cloud SDK クライアントをブロックする）、実運用は
    ``impersonate`` でサービスアカウントを使う。
    """
    if len(grid) > MAX_SHEET_ROWS:
        # 黙って古い行を残すより、書き出しを落として気づかせる。
        raise ValueError(
            f"書き出しが {len(grid)} 行で上限 {MAX_SHEET_ROWS} を超えました。"
            "置換で消しきれず古い行が残るので、範囲かページングを見直してください"
        )
    from google.auth import default
    from google.auth.transport.requests import AuthorizedSession

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if impersonate:
        from google.auth import impersonated_credentials

        source, _ = default()
        creds = impersonated_credentials.Credentials(
            source_credentials=source, target_principal=impersonate, target_scopes=scopes
        )
    else:
        creds, _ = default(scopes=scopes)
    sess = AuthorizedSession(creds)
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
    # 先にタブ全体を消してから書く。行数が減ったときに古い行が残らないようにする。
    sess.post(f"{base}/values/{SHEET_TAB}:clear", json={}).raise_for_status()
    sess.put(
        f"{base}/values/{SHEET_TAB}!A1:H{len(grid)}",
        params={"valueInputOption": "RAW"},
        json={"values": grid},
    ).raise_for_status()


def build_mirror(settings: Settings) -> Callable[[], None] | None:
    """`ServerContext.request_sheet_mirror` に挿す呼び出し可能（§10 即時反映）。

    シート ID が設定されていなければ ``None`` — 未設定を「何もしない」に落とすのは
    呼び出し側であって、ここで黙って握らない。

    **例外はここで握らない。** 握るのは `ServerContext._mirror_requests_to_sheet` の
    仕事で、そこに「鏡が曇っても本体は止めない」という判断が書いてある。二重に握ると
    失敗がどこでも見えなくなる。
    """
    sheet_id = getattr(settings, "improvement_sheet_id", "") or ""
    if not sheet_id:
        return None
    impersonate = getattr(settings, "sheets_impersonate_sa", "") or None

    def _mirror() -> None:
        write_grid(sheet_id, to_grid(exporter_rows(settings)), impersonate=impersonate)

    return _mirror


def main() -> int:
    """夜間の書き出し（コンソールエントリ `waca-loop-mirror`）。

    即時反映（§10）が働いていれば通常は差分ゼロだが、即時反映が失敗した分をここで埋める。
    `scripts/` はコンテナに入らないので、定期実行の入口はパッケージ側に置く。
    """
    import logging

    from waca_loop.config import get_settings

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    sheet_id = settings.improvement_sheet_id
    if not sheet_id:
        logging.warning("IMPROVEMENT_SHEET_ID が未設定 — 何もしません")
        return 0
    rows = exporter_rows(settings)
    write_grid(
        sheet_id,
        to_grid(rows),
        impersonate=settings.sheets_impersonate_sa or None,
    )
    logging.info("'%s' に %d 件を書き出しました", SHEET_TAB, len(rows))
    return 0


__all__ = [
    "FOOTER_NOTE",
    "HEADER",
    "MAX_SHEET_ROWS",
    "SHEET_TAB",
    "build_mirror",
    "exportable",
    "exporter_rows",
    "main",
    "reporter_cell",
    "to_grid",
    "write_grid",
]
