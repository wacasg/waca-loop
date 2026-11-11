"""記録カバレッジ（docs/SPEC_coverage.md）の受け入れ基準の置き場。

CV-* の基準は docs/SPEC_coverage.md ではなく、このファイルで定義する:

- CV-1 失敗を残す — CV-1a 証跡の失敗をログに残しツールは成功させる / CV-1b 台帳への書き込み失敗は
  固定文とエラーコードで返す / CV-1c ログ・エラー本文に内容と身元を出さない / CV-1d 同じ
  request_id の再送で行を増やさない / CV-1e 失敗した書き込みを記録済みとして数えない
- CV-2 クライアント種別と check_id の対応づけ（CV-2a〜CV-2f。各テスト名が個々の基準）

未実装の基準は skip にしておく — 実装前に緑にしない（陰性側を先に緑にしても意味が無い）。
実装した基準から skip を外す。

作法: tests/conftest.py の InMemory fixture と ServerContext を直接呼ぶ（MCP 経由にしない）。
CV-1b だけは build_mcp() の境界まで通す。
"""

from __future__ import annotations

import logging

import pytest

from waca_loop.guard import LEDGER_WRITE_FAILED_CODE, LEDGER_WRITE_FAILED_MESSAGE
from waca_loop.ledger.tool_events import InMemoryToolEventStore
from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.models import UsageRecord, UseCategory, Viewer
from waca_loop.server import ServerContext

DESIGN = "記録カバレッジの受け入れ基準"
_NOT_YET = "受け入れ基準 {sec} — 未実装（雛形）"


def _todo(sec: str):
    return pytest.mark.skip(reason=_NOT_YET.format(sec=sec))


# --- CV-1 失敗を残す -----------------------------------------------------------------------
#
# 「失敗が残る」は、成功と失敗が**区別できる**ことまで含む。黙殺（`except: pass`）だと、
# 証跡ストアの停止と「クライアントが呼ばなかった」が同じ見た目になる — それが C4 の督促が
# 答えられなければならない唯一の問いなので、ここは観測性の飾りではない。

#: ログ・エラー本文に出てはいけない語。例外本文にこれを埋め込んだストアを挿して測る。
_SECRET_SUMMARY = "SECRET-SUMMARY-会員名簿の集計"
_SECRET_ACTOR = "p_leaky_actor"


def _member(person_id: str = "p_m") -> Viewer:
    return Viewer(subject=f"sub-{person_id}", person_id=person_id, is_org_member=True,
                  units=frozenset({"dept_eng"}))


def _ctx(term_store, embedder, *, viewer=None, events=None, usage=None, policy_store=None):
    return ServerContext(
        term_store=term_store,
        usage_store=usage if usage is not None else InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=viewer or _member(),
        tool_event_store=events,
        policy_store=policy_store,
    )


class _BrokenTrail(InMemoryToolEventStore):
    """証跡だけが落ちるストア。例外本文に機微値を載せて CV-1c も同時に測る。"""

    def record(self, *a, **kw):
        raise RuntimeError(f"trail down for {_SECRET_ACTOR}: {_SECRET_SUMMARY}")


class _BrokenLedger(InMemoryUsageLogStore):
    """台帳だけが落ちるストア。例外本文に機微値（要約そのもの）を載せる。"""

    def record(self, record: UsageRecord, **kw):
        raise RuntimeError(f"usage_log insert failed for {record.actor_id}: {record.summary}")


def test_cv1a_trail_failure_is_logged_and_does_not_break_the_tool(
    term_store, embedder, policy_store, caplog
):
    """証跡ストアが例外を投げても両ツールは成功し、警告が出て、台帳行は1件のまま（CV-1a）。"""
    usage = InMemoryUsageLogStore()
    ctx = _ctx(term_store, embedder, events=_BrokenTrail(), usage=usage,
               policy_store=policy_store)
    with caplog.at_level(logging.WARNING, logger="waca_loop.server"):
        policy = ctx.get_applicable_policy()
        out = ctx.log_usage("l2", ["none"], "サンプル協会 の議事メモ要約", "claude_code")

    assert policy["legend"] and "log_id" in out       # どちらのツールも成功している
    assert len(usage.rows) == 1                        # 二重記録も欠落もしていない

    warnings = [r for r in caplog.records if "waca-loop-trail" in r.getMessage()]
    assert warnings, "証跡の失敗が黙殺されている（CV-1a の退行）"
    assert all(r.levelno == logging.WARNING for r in warnings)
    # 例外の**クラス名だけ**。本文を載せると要約や actor がログに出る（CV-1c）。
    assert all("RuntimeError" in r.getMessage() for r in warnings)


def test_cv1b_ledger_write_failure_returns_fixed_text_with_error_code(
    term_store, embedder, policy_store, caplog
):
    """台帳ストア例外 → 固定文 + WACA_LOOP_LEDGER_WRITE_FAILED（CV-1b）。"""
    events = InMemoryToolEventStore()
    ctx = _ctx(term_store, embedder, events=events, usage=_BrokenLedger(),
               policy_store=policy_store)
    with caplog.at_level(logging.ERROR, logger="waca_loop.server"):
        with pytest.raises(RuntimeError) as err:
            ctx.log_usage("l2", ["none"], "サンプル協会 の議事メモ要約", "claude_code")

    assert str(err.value) == LEDGER_WRITE_FAILED_MESSAGE
    assert LEDGER_WRITE_FAILED_CODE in str(err.value)
    assert "request_id" in str(err.value)      # 再送のしかたが本文に書かれている（CV-1d と対）
    # CV-1e: 失敗した書き込みは `usage_recorded` ではない — 督促を閉じてはならない
    kinds = [r["tool"] for r in events.rows]
    assert "usage_write_failed" in kinds
    assert "usage_recorded" not in kinds
    # 診断はサーバ側のログに、クラス名として残る
    assert any("waca-loop-ledger" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cv1b_the_mcp_boundary_shows_the_same_fixed_text(
    term_store, embedder, policy_store
):
    """MCP 境界でも本文は同じ。例外本文・traceback が外に出ないこと（CV-1b）。"""
    pytest.importorskip("mcp")
    from mcp.shared.exceptions import McpError

    from waca_loop.server import build_mcp

    ctx = _ctx(term_store, embedder, usage=_BrokenLedger(), policy_store=policy_store)
    server = build_mcp(ctx)
    with pytest.raises((McpError, Exception)) as err:
        await server.call_tool(
            "log_usage",
            {"use_category": "l2", "data_kinds": ["none"],
             "summary": _SECRET_SUMMARY, "client": "claude_code"},
        )
    text = str(err.value)
    assert LEDGER_WRITE_FAILED_CODE in text, f"MCP 境界に固定文が出ていない: {text!r}"
    assert _SECRET_SUMMARY not in text
    assert "Traceback" not in text


def test_cv1c_no_content_or_identity_leaks_into_logs_or_errors(
    term_store, embedder, policy_store, caplog, capsys
):
    """ログ行・エラー本文・stderr に summary / task_hint / actor が出ないこと（CV-1c）。

    機微値を**例外本文に埋め込んだ**ストアを両方挿す。失敗を記録する経路が
    「例外をそのままログに流す」実装なら、ここで要約と actor が漏れる。
    """
    viewer = _member(_SECRET_ACTOR)
    task_hint = f"{_SECRET_SUMMARY} の下書き"
    ctx = _ctx(term_store, embedder, viewer=viewer, events=_BrokenTrail(),
               usage=_BrokenLedger(), policy_store=policy_store)
    with caplog.at_level(logging.DEBUG):
        ctx.get_applicable_policy(task_hint)
        with pytest.raises(RuntimeError) as err:
            ctx.log_usage("l2", ["none"], _SECRET_SUMMARY, "claude_code")

    streams = capsys.readouterr()
    haystack = "\n".join(
        [r.getMessage() for r in caplog.records]
        + [str(err.value), streams.out, streams.err]
    )
    for secret in (_SECRET_SUMMARY, _SECRET_ACTOR, task_hint):
        assert secret not in haystack, f"{secret!r} がログ・エラー本文に漏れている"


def _usage_record(*, request_id: str | None, actor_id: str = "p_m",
                  summary: str = "サンプル協会 の議事メモ要約") -> UsageRecord:
    return UsageRecord(
        use_category=UseCategory("l2"), data_kinds=["none"], summary=summary,
        client="claude_code", actor_id=actor_id, actor_raw=None,
        request_id=request_id, ingested_via="mcp", status="recorded",
    )


def _ledger_twins():
    """双子。BigQuery 側は fake_bq で SQL を実評価する（既存行の SELECT が効くこと）。

    偽クライアントは列を見本行から決めるので、表は**別 actor の1行**で作る。その1行は
    「他人の行が再送判定に混ざらない」の見張りも兼ねる（同じ request_id を持たせてある）。
    """
    from datetime import UTC, datetime

    from fake_bq import FakeBigQueryClient
    from waca_loop.ledger.usage_log import BigQueryUsageLogStore, _record_to_row

    class _Settings:
        def __getattr__(self, name: str) -> str:
            if name.endswith("_table") or name.endswith("_ref"):
                return "p.d." + name.removesuffix("_table").removesuffix("_ref")
            raise AttributeError(name)

        gcp_project = "p"

    seed = _record_to_row(
        _usage_record(request_id="req-1", actor_id="p_seed"), "log-seed",
        datetime.now(UTC),
    )
    client = FakeBigQueryClient({"p.d.usage_log": [seed]})
    mem = InMemoryUsageLogStore()
    mem.rows.append(dict(seed))
    return mem, BigQueryUsageLogStore(client, _Settings()), client


def _bq_log_ids(client) -> list[str]:
    return [r["log_id"] for r in client.query("SELECT log_id FROM `p.d.usage_log`", None).result()]


def test_cv1d_resend_with_same_request_id_does_not_duplicate():
    """同 actor・同 request_id の再送は既存 log_id を返し、行は増えない（CV-1d・双子両方）。"""
    mem, bq, client = _ledger_twins()

    for store, count in ((mem, lambda: len(mem.rows)),
                         (bq, lambda: len(_bq_log_ids(client)))):
        base = count()                                  # 種の1行（別 actor・同 request_id）
        first = store.record(_usage_record(request_id="req-1"))
        assert first != "log-seed", (
            f"{type(store).__name__}: 他 actor の行を再送とみなした（request_id は "
            "actor をまたいで一意ではない）"
        )
        assert count() == base + 1

        again = store.record(_usage_record(request_id="req-1"))
        assert again == first, f"{type(store).__name__}: 再送で新しい log_id が出た"
        assert count() == base + 1, f"{type(store).__name__}: 再送で行が増えた"

        # request_id 無しは毎回新しい行（重複排除の対象にしない）
        store.record(_usage_record(request_id=None))
        store.record(_usage_record(request_id=None))
        assert count() == base + 3, f"{type(store).__name__}: id 無しの記録がまとめられた"


def test_cv1d_twins_return_the_same_answer_for_the_same_resend():
    """双子が同じ判断をすること（片方だけ重複排除が効く状態を作らない）。"""
    mem, bq, client = _ledger_twins()
    rec = _usage_record(request_id="req-parity")
    mem_first, bq_first = mem.record(rec), bq.record(rec)
    assert (mem.record(rec) == mem_first) == (bq.record(rec) == bq_first)
    assert len(mem.rows) == len(_bq_log_ids(client)) == 2   # 種 + 1件



# --- CV-1d の陰性側: 重複排除が**記録を消してはならない**-------
#
# ここが CV-1d のいちばん危ない面。`request_id` は DDL で「学習リクエスト等との突合用（任意）」と
# 書かれた**多対1の突合キー**で、1ターンに一意だと誰にも言っていない。素朴に
# `(actor, request_id)` だけで重複排除すると、同じ値を使い回すクライアントの2ターン目以降が
# 台帳に入らないまま「記録済み」に見える — CV-1 が防ごうとしている未記録そのものを作ってしまう。


def test_a_reused_request_id_across_turns_still_records_every_turn(
    term_store, embedder, policy_store
):
    """同じ request_id・**別の内容** なら、どのターンも記録されること。"""
    usage = InMemoryUsageLogStore()
    events = InMemoryToolEventStore()
    ctx = _ctx(term_store, embedder, events=events, usage=usage, policy_store=policy_store)
    for i in range(4):
        out = ctx.log_usage("l2", ["none"], f"サンプル協会 の議事メモ要約 {i}", "claude_code",
                            request_id="learning-request-42")
        assert "deduplicated" not in out, f"{i}ターン目が再送とみなされた"
    assert len(usage.rows) == 4, "使い回した request_id で記録が落ちた"
    assert [r["tool"] for r in events.rows].count("usage_recorded") == 4


def test_a_resend_is_not_counted_as_a_recorded_turn(term_store, embedder, policy_store):
    """再送は `usage_recorded` を出さない = 督促の分子に入らないこと（CV-1d）。

    ここを `usage_recorded` にすると、使い回した1つの `request_id` で「5ターン記録済み・
    中身は1件」が作れてしまう（証跡からは本物の記録と区別できない）。
    """
    usage = InMemoryUsageLogStore()
    events = InMemoryToolEventStore()
    ctx = _ctx(term_store, embedder, events=events, usage=usage, policy_store=policy_store)
    first = ctx.log_usage("l2", ["none"], "サンプル協会 の議事メモ要約", "claude_code",
                          request_id="req-1")
    again = ctx.log_usage("l2", ["none"], "サンプル協会 の議事メモ要約", "claude_code",
                          request_id="req-1")

    assert again["log_id"] == first["log_id"]
    assert again["deduplicated"] is True and "deduplicated" not in first
    assert len(usage.rows) == 1
    kinds = [r["tool"] for r in events.rows]
    assert kinds.count("usage_recorded") == 1, "再送が記録として数えられている"
    assert kinds.count("usage_resend_deduped") == 1


def test_a_resend_outside_the_window_is_a_new_record():
    """窓（分オーダー）の外なら新しい行。再送の地平を過ぎた同値は別の記録として扱う。"""
    from datetime import UTC, datetime, timedelta

    from waca_loop.ledger.usage_log import RESEND_WINDOW_MINUTES

    assert RESEND_WINDOW_MINUTES <= 60, "再送の窓が時間オーダーに広がっている"
    store = InMemoryUsageLogStore()
    now = datetime.now(UTC)
    rec = _usage_record(request_id="req-1")
    first = store.record(rec, occurred_at=now - timedelta(minutes=RESEND_WINDOW_MINUTES + 1))
    assert store.record(rec, occurred_at=now) != first
    assert len(store.rows) == 2


def test_a_different_declaration_is_not_a_resend():
    """要約が同じでも**宣言**が違えば別の記録。

    2ターン目が `pii` を宣言した場合が肝心。ここを「要約が同じだから再送」と畳むと、
    落ちるのは**いちばん残したい宣言**になる。
    """
    import dataclasses

    mem, bq, client = _ledger_twins()
    changes = (("data_kinds", ["pii"]), ("client", "codex"), ("status", "pending"))
    for store, count in ((mem, lambda: len(mem.rows)),
                         (bq, lambda: len(_bq_log_ids(client)))):
        base = count()
        same = "サンプル協会 の会員データ集計"
        original = _usage_record(request_id="req-decl", summary=same)
        first = store.record(original)
        for field, other in changes:
            rec = dataclasses.replace(original, **{field: other})
            assert store.record(rec) != first, (
                f"{type(store).__name__}: {field} が違うのに再送とみなされた"
            )
        assert count() == base + 1 + len(changes)


def test_the_two_fingerprint_gates_agree():
    """双子のダイジェストが一致すること（Python 側と SQL 側が別の答えを出さない）。

    SQL 側は `fake_bq` が **本物の SHA256/TO_BASE64** で評価する。ここが恒真だと、
    「内容が同じか」を実は見ていない状態で緑になる。
    """
    mem, bq, client = _ledger_twins()
    rec = _usage_record(request_id="req-fp", summary="サンプル協会 の議事メモ要約")
    assert mem.record(rec) == mem.record(rec)          # Python 側が同一と見る
    bq_first = bq.record(rec)
    assert bq.record(rec) == bq_first                  # SQL 側も同一と見る
    assert len(mem.rows) == len(_bq_log_ids(client)) == 2   # 種 + 1件


def test_the_resend_lookup_never_sends_the_summary_text_to_bigquery():
    """照会のパラメータに要約が乗らないこと。"""
    mem, bq, client = _ledger_twins()
    secret = f"サンプル協会 {_SECRET_SUMMARY}"
    bq.record(_usage_record(request_id="req-meta", summary=secret))
    bq.record(_usage_record(request_id="req-meta", summary=secret))
    assert client.query_params, "照会が走っていない（テストが飾りになっている）"
    sent = [str(v) for params in client.query_params for v in params.values()]
    assert not any(secret in sql for sql in client.sqls)
    assert not any(secret in value for value in sent), (
        "要約が照会パラメータに乗っている（クエリジョブのメタデータに残る）"
    )
    from waca_loop.ledger.usage_log import _record_fingerprint

    fingerprint = _record_fingerprint(_usage_record(request_id="req-meta", summary=secret))
    assert secret not in fingerprint


def test_request_id_is_never_free_prose_in_the_ledger(term_store, embedder, policy_store):
    """自由文の request_id は保存されないこと。"""
    usage = InMemoryUsageLogStore()
    ctx = _ctx(term_store, embedder, usage=usage, policy_store=policy_store)
    ctx.log_usage("l2", ["none"], "サンプル協会 の議事メモ要約", "claude_code",
                  request_id=f"要約: {_SECRET_SUMMARY}")
    assert usage.rows[0]["request_id"] is None, "自由文が request_id 列に入った"
    ctx.log_usage(
        "l2", ["none"], "サンプル協会 の議事メモ要約 2", "claude_code", request_id="req_1:2"
    )
    assert usage.rows[1]["request_id"] == "req_1:2"   # id 形はそのまま残る


def test_empty_identities_do_not_share_a_dedup_bucket():
    """actor_id が空文字の呼び手同士が一括りにならないこと。

    SQL の `IS NOT NULL` は空文字を通すので、片方の `request_id` がもう片方の書き込みを
    消せてしまう。Python 側は falsy で弾いており、**SQL だけが緩い**状態を作らない。
    """
    mem, bq, client = _ledger_twins()
    for store, count in ((mem, lambda: len(mem.rows)),
                         (bq, lambda: len(_bq_log_ids(client)))):
        base = count()
        a = _usage_record(request_id="req-empty", actor_id="", summary="A の要約")
        b = _usage_record(request_id="req-empty", actor_id="", summary="A の要約")
        first = store.record(a)
        second = store.record(b)
        assert second != first, f"{type(store).__name__}: 空 identity が一括りになった"
        assert count() == base + 2

# --- CV-2 クライアント種別の観測 -------------------------------------------------------------

@_todo("CV-2a")
def test_cv2a_unknown_client_is_normalized_to_other_and_raw_value_is_dropped():
    """未知の client は other として保存され、原文がどの列・ログにも残らない。"""


# --- CV-2 check_id の対応づけ -----------------------------------------------------------------

@_todo("CV-2b")
def test_cv2b_check_id_record_does_not_close_another_clients_check():
    """自動実行の未記録が、対話側の check_id 付き記録で消えない（陰性側）。"""


@_todo("CV-2c")
def test_cv2c_record_without_check_id_pairs_with_the_latest_check():
    """check_id 無しの記録は直前の確認を閉じる。tests/test_ops.py の C4 リプレイと同じ結果。"""


@_todo("CV-2d")
def test_cv2d_foreign_or_malformed_check_ids_close_nothing_and_are_not_stored():
    """別 actor の id・窓外の id・形式外の文字列は何も閉じず、保存もされない。"""


@_todo("CV-2e")
def test_cv2e_breakdown_never_carries_another_actors_client():
    """SQL 側ゲートと返却後ゲートを別々に壊しても、内訳に他 actor のクライアント名が混ざらない。"""


@_todo("CV-2f")
def test_cv2f_late_record_closes_its_own_check():
    """次の確認より後に届いた同 id の記録が、その id の確認を閉じる。"""
