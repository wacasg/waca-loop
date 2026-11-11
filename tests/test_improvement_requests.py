"""改善要望の受け入れ基準 R1〜R8 (docs/SPEC_improvement_requests.md §6・§8)。

InMemory ストアでオフライン実行（T#/S# と同じ規律）。
"""

from __future__ import annotations

import pytest

from waca_loop.improvement.requests import (
    InMemoryRequestStore,
    request_acl_where_clause,
)
from waca_loop.models import Viewer
from waca_loop.retrieval.acl import OWNER_UNIT, STEWARD_UNIT
from waca_loop.server import ServerContext

KYOZAI = Viewer(subject="k", person_id="p_k", is_org_member=True,
                units=frozenset({"kyozai", "org"}))
JIMU = Viewer(subject="j", person_id="p_j", is_org_member=True,
              units=frozenset({"secretariat", "org"}))
STEWARD = Viewer(subject="s", person_id="p_s", is_org_member=True,
                 units=frozenset({STEWARD_UNIT}))
OWNER = Viewer(subject="o", person_id="p_yamada", is_org_member=True,
               units=frozenset({OWNER_UNIT, "org"}))
PARTNER = Viewer(subject="x", person_id="p_x", is_org_member=False,
                 units=frozenset({"partner"}))
UNRESOLVED = Viewer(subject="u", person_id=None, is_org_member=True,
                    units=frozenset({"org"}))


def _ctx(viewer, store, term_store, embedder):
    return ServerContext(
        term_store=term_store, usage_store=_Sink(), embedder=embedder,
        viewer=viewer, request_store=store,
    )


class _Sink:
    def add(self, *a, **k):
        return "u1"


@pytest.fixture
def store():
    return InMemoryRequestStore()


def test_r1_an_unresolved_viewer_cannot_file_a_request(store, term_store, embedder):
    """R1 帰属の無い viewer は共有層に書けない (§3.1・§5)."""
    ctx = _ctx(UNRESOLVED, store, term_store, embedder)
    with pytest.raises(PermissionError):
        ctx.report_improvement_request("bug", "検索が落ちる", "lookup_terms が 500 を返す")
    assert store.rows == []


def test_r2_owner_unit_is_resolved_not_guessed(store, term_store, embedder):
    """R2 所有単位は resolve_owner_unit 経由。複数所属で名指しが無ければ拒否."""
    ctx = _ctx(KYOZAI, store, term_store, embedder)
    ctx.report_improvement_request("improvement", "用語検索を絞りたい", "件数が多い")
    assert store.rows[0]["owner_unit"] == "kyozai"   # org は所有にしない

    both = Viewer(subject="b", person_id="p_b", is_org_member=True,
                  units=frozenset({"kyozai", "secretariat", "org"}))
    with pytest.raises(PermissionError, match="name the owning unit"):
        _ctx(both, store, term_store, embedder).report_improvement_request(
            "bug", "曖昧", "どの部門の話か決められない"
        )
    # 名指しすれば通る（推測はしないが、決められないわけではない）
    _ctx(both, store, term_store, embedder).report_improvement_request(
        "bug", "曖昧", "本文", owner_unit="secretariat"
    )
    assert store.rows[-1]["owner_unit"] == "secretariat" 


def test_r3_anonymous_leaves_no_reporter_id(store, term_store, embedder):
    """R3 匿名なら person_id が行に残らない（あとから名寄せできない）."""
    ctx = _ctx(KYOZAI, store, term_store, embedder)
    ctx.report_improvement_request("bug", "落ちる", "再現する", anonymous=True)
    row = store.rows[0]
    assert row["reporter_id"] is None
    assert row["reporter_label"] == "匿名"
    assert "p_k" not in str(row)


def test_r4_form_rows_stay_out_of_the_developer_queue(store, term_store, embedder):
    """R4 ログイン不要フォーム由来は unverified で入り、既定の一覧に出ない (§3.2).

    ここが緩むと、URL を知った外部からの書き込みがそのまま開発者の作業キューに流れ込む。
    """
    from waca_loop.models import ImprovementRequest, RequestKind, RequestStatus

    store.add(ImprovementRequest(
        kind=RequestKind.OTHER, title="外から来た話", body="本文",
        channel="form", status=RequestStatus.UNVERIFIED, owner_unit=None,
    ))
    # 既定（open のみ）には出ない
    assert _ctx(OWNER, store, term_store, embedder).list_improvement_requests()["requests"] == []
    # 仕分け役が明示的に指定したときだけ見える
    triage = _ctx(OWNER, store, term_store, embedder).list_improvement_requests("unverified")
    assert len(triage["requests"]) == 1
    # 一般メンバーは明示しても見えない
    assert _ctx(
        KYOZAI, store, term_store, embedder
    ).list_improvement_requests("unverified")["requests"] == []


def test_r5_a_dept_request_is_not_visible_to_another_dept(store, term_store, embedder):
    """R5 部内スコープの要望が他部門から見えない（二重ゲート）."""
    _ctx(KYOZAI, store, term_store, embedder).report_improvement_request(
        "bug", "教材側の話", "本文"
    )
    assert _ctx(JIMU, store, term_store, embedder).list_improvement_requests()["requests"] == []
    assert len(
        _ctx(KYOZAI, store, term_store, embedder).list_improvement_requests()["requests"]
    ) == 1
    # 横断視界: スチュワードと WACA Loop オーナーは背骨を見る
    assert len(
        _ctx(STEWARD, store, term_store, embedder).list_improvement_requests()["requests"]
    ) == 1
    # 非 org メンバーは常に空 (fail closed)
    assert _ctx(
        PARTNER, store, term_store, embedder
    ).list_improvement_requests()["requests"] == []
    # SQL 側にも同じ規則があること (§3.3 二重ゲート)
    sql, params = request_acl_where_clause(JIMU)
    assert "unverified" in sql and "@acl_units" in sql
    assert params["req_is_org_member"] is True and params["req_is_triage"] is False


def test_r6_no_numeric_ranking_columns_anywhere():
    """R6 DDL・行マッパー・モデルに priority/severity の数値列が無い (§2).

    数値列を作ると、そのうち誰かがそれで人や部署を比べる。「最優先」は提示順のこと。
    """
    from pathlib import Path

    from waca_loop.improvement.requests import _request_to_row
    from waca_loop.models import ImprovementRequest, RequestKind

    ddl = Path(__file__).resolve().parent.parent / "sql/ddl/12_improvement_requests.sql"
    sql = ddl.read_text(encoding="utf-8")
    body = sql.split("CREATE TABLE", 1)[1]
    for banned in ("priority", "severity", "rank", "score"):
        assert f"  {banned}" not in body, f"DDL に {banned} 列がある"
    row = _request_to_row(
        ImprovementRequest(kind=RequestKind.BUG, title="t", body="b"), "r1", _now()
    )
    assert not {"priority", "severity", "rank", "score"} & set(row)
    assert not {"priority", "severity"} & set(ImprovementRequest.__dataclass_fields__)


def test_r6b_no_verbatim_field(store):
    """INV-1 と同型 — prompt/raw_text/transcript 相当の欄を持たない."""
    from waca_loop.improvement.requests import _request_to_row
    from waca_loop.models import ImprovementRequest, RequestKind

    row = _request_to_row(
        ImprovementRequest(kind=RequestKind.BUG, title="t", body="b"), "r1", _now()
    )
    assert not {"prompt", "raw_text", "transcript"} & set(row)


def test_r9_other_kind_is_kept_not_folded(store, term_store, embedder):
    """R9「その他」を捨てず、どちらかへ寄せもしない (§1-9).

    寄せると「不具合の件数」を数えたときに混ざっているのに気づけなくなる。
    """
    ctx = _ctx(KYOZAI, store, term_store, embedder)
    ctx.report_improvement_request("other", "どちらでもない話", "本文")
    assert store.rows[0]["kind"] == "other"
    assert len(ctx.list_improvement_requests(kind="other")["requests"]) == 1
    assert ctx.list_improvement_requests(kind="bug")["requests"] == []


def test_r8_wontfix_is_owner_only(store, term_store, embedder):
    """R8 見送りはスチュワードでも付けられない — WACA Loop オーナー限定 (§8).

    ほかの状態は途中経過の記録で次の更新で直る。見送りは「直さないと決めた」結論で、
    起案者への説明責任が伴う。記録の権限と打ち切る権限は別物。
    """
    rid = _ctx(KYOZAI, store, term_store, embedder).report_improvement_request(
        "bug", "落ちる", "本文"
    )["request_id"]

    steward = _ctx(STEWARD, store, term_store, embedder)
    # ほかの状態遷移はスチュワードで通る
    steward.set_request_status(rid, "designing")
    assert store.rows[0]["status"] == "designing"
    # 見送りだけ落ちる
    with pytest.raises(PermissionError, match="wontfix"):
        steward.set_request_status(rid, "wontfix")
    assert store.rows[0]["status"] == "designing"

    # WACA Loop オーナーなら通る
    _ctx(OWNER, store, term_store, embedder).set_request_status(
        rid, "wontfix", resolution="仕様どおりのため見送り"
    )
    assert store.rows[0]["status"] == "wontfix"
    assert store.rows[0]["resolution"] == "仕様どおりのため見送り"


def test_status_change_needs_a_visible_row(store, term_store, embedder):
    """見えない要望を手探りで書き換えられないこと（blind write の禁止）."""
    rid = _ctx(KYOZAI, store, term_store, embedder).report_improvement_request(
        "bug", "教材側の話", "本文"
    )["request_id"]
    with pytest.raises(ValueError, match="not found or not visible"):
        _ctx(JIMU, store, term_store, embedder).set_request_status(rid, "designing")


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


# --- フォーム取り込みと書き出し（純粋関数・§3.2/§5） ----------------------------------

def test_form_rows_are_unverified_and_never_carry_a_person_id():
    """フォームは本人性が無い。person を引き当てず、必ず unverified で入る (§3.2)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.ingest_improvement_form import burst_refusal, parse_form_grid

    grid = [
        ["タイムスタンプ", "報告の種類", "レポートのタイトル", "問題の詳細",
         "再現手順", "動作環境", "お名前"],
        ["2026-08-21", "Bug Report / 不具合報告", "検索が落ちる", "500 が返る",
         "1. 検索する", "macOS / Claude Code", "打橋"],
        ["2026-08-21", "Other / その他", "使い方が分からない", "どこを見ればよいか",
         "", "", ""],
    ]
    rows = parse_form_grid(grid)
    assert len(rows) == 2
    assert all(r["status"] == "unverified" and r["reporter_id"] is None for r in rows)
    assert all(r["channel"] == "form" for r in rows)
    # 再現手順・動作環境は列を増やさず body に連結する（§7.1）
    assert "【再現手順】" in rows[0]["body"] and "【動作環境】" in rows[0]["body"]
    # 「その他」は寄せずに残す（§1-9）
    assert rows[0]["kind"] == "bug"
    assert rows[1]["kind"] == "other"
    # 名前が空欄なら匿名（「匿名にしますか」とは問わない）
    assert rows[0]["reporter_label"] == "打橋"
    assert rows[1]["reporter_label"] == "匿名"
    # 大量流入は捨てずに止める（§3.2）
    assert burst_refusal(rows) is None
    assert burst_refusal(rows * 20) is not None


def test_export_grid_drops_the_body():
    """シートは3軸ACLが効かない場所。本文を持ち出さない (§5)."""
    from waca_loop.improvement.sheet_mirror import to_grid

    grid = to_grid([{
        "request_id": "r1", "kind": "bug", "title": "件名", "body": "部内の生々しい事情",
        "reporter_label": None, "status": "wontfix", "resolution": "仕様どおり",
        "created_at": "2026-08-21T00:00:00+00:00", "updated_at": "2026-08-21T00:00:00+00:00",
    }])
    flat = "".join("".join(r) for r in grid)
    assert "部内の生々しい事情" not in flat
    # 注記も生成物の一部（タブごと置換するので、手書きの注記は消える）
    assert "WACA Loop には戻りません" in flat
    assert "不具合" in flat and "見送り" in flat
    assert "匿名" in flat            # reporter_label が無い行は person_id から復元しない


# --- 匿名・可視域の回帰テスト -------------------------------------

def test_anonymous_leaves_no_unit_and_no_ledger_trace(store, term_store, embedder):
    """匿名は person_id を落とすだけでは成立しない.

    (1) 所属ユニットを刻むと、数名しかいない単位から出た匿名要望は owner_unit だけで
        起案者が割れる。(2) tool_events に actor_id 付きの痕跡が残ると、同時刻の
        イベントを突き合わせて秒精度で一意に決まる。
    """
    from waca_loop.ledger.tool_events import InMemoryToolEventStore

    trail = InMemoryToolEventStore()
    ctx = ServerContext(
        term_store=term_store, usage_store=_Sink(), embedder=embedder,
        viewer=KYOZAI, request_store=store, tool_event_store=trail,
    )
    ctx.report_improvement_request("bug", "言いにくい話", "本文", anonymous=True)
    row = store.rows[0]
    assert row["reporter_id"] is None
    assert row["owner_unit"] is None        # ← 所属で割れない
    assert row["scope_tier"] == "org"       # 匿名の代償は全社可視。狭く見せて割れるより良い
    assert trail.rows == []                 # ← 時刻突き合わせで割れない

    # 記名なら、両方とも残る（匿名の扱いだけが特別であることを固定する）
    ctx.report_improvement_request("bug", "普通の話", "本文")
    assert store.rows[1]["owner_unit"] == "kyozai"
    assert any(r["tool"] == "improvement_request_reported" for r in trail.rows)


def test_export_does_not_flatten_scoped_or_unverified_rows():
    """出口で可視域を捨てない.

    ACL を通した結果をそのまま全社シートへ書くと、WACA Loop では見られない要望の件名と
    実装内容がシート経由で読めてしまう。
    """
    from waca_loop.improvement.sheet_mirror import exportable, reporter_cell

    rows = [
        {"request_id": "a", "owner_unit": "kyozai", "status": "open",
         "title": "A部の取り込みが失敗する"},
        {"request_id": "b", "owner_unit": None, "status": "unverified",
         "title": "社外から書かれた何か"},
        {"request_id": "c", "owner_unit": None, "status": "open", "title": "全社の話"},
    ]
    assert [r["request_id"] for r in exportable(rows)] == ["c"]

    # 検証済みかどうかを表示に残す（「匿名」も「記名」も意味を失わせない）
    assert reporter_cell({"reporter_id": "p_a", "reporter_label": None}) == "記名（WACA Loop経由）"
    assert reporter_cell({"reporter_id": None, "reporter_label": "田中"}) == "（自己申告）田中"
    assert reporter_cell({"reporter_id": None, "reporter_label": "匿名"}) == "匿名"


def test_the_two_gates_agree_on_every_combination(term_store, embedder):
    """二重ゲートが本当に二重であること — 片方だけ直したときに気づける仕掛け.

    in-process の request_visible_to と SQL 断片の評価を、行 × viewer の直積で突き合わせる。
    SQL は実行できないので、断片の構造から同じ判定を組み立てて比べる。
    """
    from waca_loop.improvement.requests import request_visible_to
    from waca_loop.retrieval.acl import visible_to

    rows = [
        {"status": st, "owner_unit": ou, "scope_tier": tier,
         "scope_audience": "internal", "scope_sensitivity": sens}
        for st in ("open", "unverified")
        for ou in (None, "kyozai", "secretariat")
        for tier in ("org", "dept")
        for sens in ("normal", "sensitive")
    ]
    viewers = [KYOZAI, JIMU, STEWARD, OWNER, PARTNER]
    for row in rows:
        for v in viewers:
            got = request_visible_to(row, v)
            # SQL 断片が表す規則を、同じ部品（visible_to）から組み立て直す
            expect = bool(v.is_org_member) and (
                bool(v.units & {STEWARD_UNIT, OWNER_UNIT})
                or (
                    row["status"] != "unverified"
                    and visible_to(_scope(row), row["owner_unit"], v)
                )
            )
            assert got is expect, (row, v.subject, got, expect)


def _scope(row):
    from waca_loop.models import (
        ScopeAudience,
        ScopeSensitivity,
        ScopeTags,
        ScopeTier,
    )

    return ScopeTags(
        ScopeTier(row["scope_tier"]),
        ScopeAudience(row["scope_audience"]),
        ScopeSensitivity(row["scope_sensitivity"]),
    )


def test_ll_owner_unit_survives_a_sheet_resync():
    """`owner` が組織シート再同期で消えないこと (§8 carry-forward).

    シートが表現できないユニットなので、消えると誰も見送りを決められなくなる。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.sync_persons_from_sheet import UNIT_MAP, manual_units

    derivable = set(UNIT_MAP.values()) | {"org"}
    kept = manual_units([OWNER_UNIT, "kyozai", "org"], derivable)
    assert OWNER_UNIT in kept
    assert "kyozai" not in kept   # シート由来は毎回作り直されるので持ち越さない


def test_tool_requests_are_not_filed_as_sfaq_candidates(term_store, embedder):
    """WACA Loop 自身への要望は SFAQ 候補にしない (§9.2).

    混ぜると SFAQ が「使いにくい」の山になり、「なぜそう決めたか」を引くための層が
    用途を失う。行き先が無いのではなく、別の入口(report_improvement_request)がある。
    """
    from waca_loop.improvement.candidates import InMemoryCandidateStore
    from waca_loop.server import _is_about_the_tool

    assert _is_about_the_tool({"question_text": "search_sfaq が遅いのを直したい"}) is True
    assert _is_about_the_tool({"question_text": "WACA Loop の用語検索が見にくい"}) is True
    # 組織の判断は通す（道具の名前が出ない普通の問い）
    assert _is_about_the_tool({"question_text": "会員名簿の重複はどう判断するか"}) is False

    ctx = ServerContext(
        term_store=term_store, usage_store=_Sink(), embedder=embedder,
        viewer=KYOZAI, candidate_store=InMemoryCandidateStore(),
    )
    # バッチ全体を落とさず、該当項目だけを落として理由を返す（途中で raise すると先に
    # 書けた候補だけが残り、呼び出し側が再送して重複する）
    out = ctx.propose_candidates([
        {"kind": "sfaq", "payload": {"question_text": "会員名簿の重複はどう判断するか"}},
        {"kind": "sfaq", "payload": {"question_text": "lookup_terms の結果が多すぎる件"}},
    ])
    assert len(out["candidate_ids"]) == 1               # 正当な候補は残る
    assert out["skipped"][0]["index"] == 1              # 落としたものと理由が分かる
    assert "report_improvement_request" in out["skipped"][0]["reason"]


def test_the_sheet_mirror_never_breaks_the_write(store, term_store, embedder):
    """鏡が曇っても本体は止めない (§10).

    シート書き込みの例外を伝播させると、Sheets API の都合で組織の記録そのものが落ちる。
    正(WACA Loop)と鏡(シート)の関係が逆転するので、ここは握りつぶすのが正しい。
    """
    def _explode():
        raise RuntimeError("Sheets API down")

    ctx = ServerContext(
        term_store=term_store, usage_store=_Sink(), embedder=embedder,
        viewer=KYOZAI, request_store=store, request_sheet_mirror=_explode,
    )
    out = ctx.report_improvement_request("bug", "落ちる", "本文")
    assert out["status"] == "open"
    assert len(store.rows) == 1          # ← 本体の記録は残っている


def test_role_grant_plan_is_idempotent():
    """役割ユニットの付与/剥奪は冪等（§8 の最初の1回を与える経路）."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.grant_role_unit import plan

    owner = OWNER_UNIT
    assert plan(["org"], owner, revoke=False) == ["org", owner]
    assert plan(["org", owner], owner, revoke=False) is None
    assert plan(["org", owner], owner, revoke=True) == ["org"]
    assert plan(["org"], owner, revoke=True) is None
