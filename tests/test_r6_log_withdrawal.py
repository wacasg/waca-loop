"""R6 — 本人が自分の台帳記録を取り下げる.

3つの決まりが、そのまま3つの性質になっている。

① 本人申告      → 取り下げられるのは**自分の行だけ**。他人の log_id は動かせない
② 論理削除      → 行は残る。通常の読み取り・集計からだけ外れる
③ 警告つき閲覧  → スチュワードは読めるが、**毎回**警告が payload に付いてくる

`review_pending_usage` の discard（物理削除・fail-closed）とは失敗の向きが逆であること
も、ここで固定する。取り下げは「先に隠す」ほうが安全なので、証跡の失敗で止めない。
"""

from __future__ import annotations

import pytest

from waca_loop.embeddings import HashingEmbedder
from waca_loop.grounding.persons import InMemoryPersonStore
from waca_loop.grounding.terms import InMemoryTermStore
from waca_loop.ledger.tool_events import InMemoryToolEventStore
from waca_loop.ledger.usage_log import (
    DELETION_REASONS,
    InMemoryUsageLogStore,
)
from waca_loop.models import UsageRecord, UseCategory, Viewer
from waca_loop.server import ServerContext


def _utc():
    from datetime import UTC, datetime

    return datetime.now(UTC)

OWNER = Viewer(subject="s1", person_id="p_owner", is_org_member=True,
               units=frozenset({"secretariat", "org"}))
OTHER = Viewer(subject="s2", person_id="p_other", is_org_member=True,
               units=frozenset({"secretariat", "org"}))
STEWARD = Viewer(subject="s3", person_id="p_steward", is_org_member=True,
                 units=frozenset({"steward", "org"}))


def _ctx(viewer, store, trail=None):
    emb = HashingEmbedder()
    return ServerContext(
        term_store=InMemoryTermStore([], emb),
        usage_store=store,
        embedder=emb,
        viewer=viewer,
        person_store=InMemoryPersonStore(persons=[], identity_map={}),
        tool_event_store=trail,
    )


def _row(store, actor_id: str) -> str:
    return store.record(
        UsageRecord(
            actor_id=actor_id,
            actor_raw=None,
            client="claude_code",
            use_category=UseCategory.L1,
            data_kinds=["internal"],
            summary="要約",
            ingested_via="mcp",
        )
    )


@pytest.fixture
def store():
    return InMemoryUsageLogStore()


# --- ① 本人申告 ---------------------------------------------------------------

def test_the_owner_can_withdraw_their_own_row(store):
    log_id = _row(store, "p_owner")
    out = _ctx(OWNER, store).request_log_deletion(log_id, "private")
    assert out["withdrawn"] is True
    assert [w["log_id"] for w in store.withdrawals] == [log_id]
    assert store.withdrawals[0]["reason"] == "private"


def test_someone_else_cannot_withdraw_my_row(store):
    """①の核。他人の log_id を渡しても行は動かない."""
    log_id = _row(store, "p_owner")
    with pytest.raises(ValueError):
        _ctx(OTHER, store).request_log_deletion(log_id, "private")
    assert store.withdrawals == []


def test_not_yours_and_not_found_are_indistinguishable(store):
    """他人の行と存在しない行で、返る形が変わってはいけない(id の総当たり防止)."""
    import uuid

    mine = _row(store, "p_owner")
    absent = str(uuid.uuid4())
    ctx = _ctx(OTHER, store)
    with pytest.raises(ValueError) as not_yours:
        ctx.request_log_deletion(mine, "private")
    with pytest.raises(ValueError) as no_such:
        ctx.request_log_deletion(absent, "private")
    assert str(not_yours.value) == f"usage row not found: {mine}"
    assert str(no_such.value) == f"usage row not found: {absent}"


def test_the_steward_is_not_exempt_from_ownership(store):
    """スチュワードでも「他人の行を代わりに取り下げる」経路は無い(①は本人申告)."""
    log_id = _row(store, "p_owner")
    with pytest.raises(ValueError):
        _ctx(STEWARD, store).request_log_deletion(log_id, "private")
    assert store.withdrawals == []


# --- ② 論理削除 ---------------------------------------------------------------

def test_the_row_survives_the_withdrawal(store):
    """物理削除しない(②)。行数は減らない."""
    log_id = _row(store, "p_owner")
    _ctx(OWNER, store).request_log_deletion(log_id, "private")
    assert len(store.rows) == 1


def test_a_withdrawn_row_leaves_the_monitor(store):
    """取り下げた行は shadow-AI モニタの母数から外れる."""
    log_id = _row(store, "p_owner")
    assert store.distinct_actors(30) == {"p_owner"}
    _ctx(OWNER, store).request_log_deletion(log_id, "private")
    assert store.distinct_actors(30) == set()


def test_a_withdrawn_row_cannot_be_approved_back(store):
    """スチュワードの pending 承認で復活させられない."""
    log_id = _row(store, "p_owner")
    _ctx(OWNER, store).request_log_deletion(log_id, "private")
    assert store.set_status(log_id, "recorded") is False


def test_withdrawing_twice_is_refused(store):
    log_id = _row(store, "p_owner")
    ctx = _ctx(OWNER, store)
    ctx.request_log_deletion(log_id, "private")
    with pytest.raises(ValueError):
        ctx.request_log_deletion(log_id, "private")


# --- ③ 警告つき閲覧 -----------------------------------------------------------

WARNING = "これは論理削除済みです。インシデントなど必要がなければ閲覧しないでください。"


def test_the_steward_sees_the_warning_with_the_rows(store):
    log_id = _row(store, "p_owner")
    _ctx(OWNER, store).request_log_deletion(log_id, "mistake")
    out = _ctx(STEWARD, store).list_withdrawn_usage()
    assert out["warning"] == WARNING
    assert [r["log_id"] for r in out["rows"]] == [log_id]


def test_the_warning_is_there_even_when_nothing_was_withdrawn(store):
    """警告は「結果に添えるおまけ」ではなく、この一覧の一部."""
    assert _ctx(STEWARD, store).list_withdrawn_usage()["warning"] == WARNING


def test_both_stores_project_the_same_columns(store):
    """双子がずれないこと。片側だけ列を落としても気づけない形にはしない.

    「SQL に列名が含まれるか」の**片方向**チェックでは、`summary` を定数から外すと
    InMemory は返さなくなるのにテストは通り、本番だけ返し続ける。両者とも
    `_WITHDRAWN_FIELDS` から組み立てるので、その事実を固定する。
    """
    import inspect

    from waca_loop.ledger.usage_log import (
        _WITHDRAWN_DETAIL_FIELDS,
        _WITHDRAWN_FIELDS,
        BigQueryUsageLogStore,
    )

    log_id = _row(store, "p_owner")
    _ctx(OWNER, store).request_log_deletion(log_id, "private")
    got = _ctx(STEWARD, store).list_withdrawn_usage()["rows"][0]
    assert tuple(got) == _WITHDRAWN_FIELDS
    # BigQuery 側は SELECT 句を定数から組み立てている（ハードコードしていない）
    src = inspect.getsource(BigQueryUsageLogStore._usage_rows_for_withdrawals_sql)
    assert "for f in fields" in src
    assert "_WITHDRAWN_FIELDS" in inspect.getsource(BigQueryUsageLogStore.list_withdrawn)
    assert "_WITHDRAWN_DETAIL_FIELDS" in inspect.getsource(BigQueryUsageLogStore.get_withdrawn)
    assert "summary" not in _WITHDRAWN_FIELDS
    assert "reason" not in _WITHDRAWN_FIELDS
    assert "summary" in _WITHDRAWN_DETAIL_FIELDS
    assert "reason" in _WITHDRAWN_DETAIL_FIELDS
    assert "actor_raw" not in _WITHDRAWN_DETAIL_FIELDS


def test_a_non_steward_cannot_read_withdrawn_rows(store):
    log_id = _row(store, "p_owner")
    _ctx(OWNER, store).request_log_deletion(log_id, "private")
    for viewer in (OWNER, OTHER):
        with pytest.raises(PermissionError):
            _ctx(viewer, store).list_withdrawn_usage()


def test_the_steward_list_is_capped_at_the_tightest_default(store):
    """_K_CAPS に載せていない = 既定の最小値(10)。黙って広げていないことを固定する."""
    for _ in range(25):
        log_id = _row(store, "p_owner")
        _ctx(OWNER, store).request_log_deletion(log_id, "private")
    assert len(_ctx(STEWARD, store).list_withdrawn_usage(1000)["rows"]) == 10


# --- 理由は閉じた符号（自由文を書かせない / FLOOR-01） -------------------------

@pytest.mark.parametrize("reason", DELETION_REASONS)
def test_every_documented_reason_is_accepted(store, reason):
    log_id = _row(store, "p_owner")
    assert _ctx(OWNER, store).request_log_deletion(log_id, reason)["reason"] == reason


def test_free_text_reason_is_refused(store):
    """取り下げたい内容を、同じ表にもう一度書き写させない."""
    log_id = _row(store, "p_owner")
    with pytest.raises(ValueError):
        _ctx(OWNER, store).request_log_deletion(log_id, "妻との離婚調停の相談だったため")
    assert store.withdrawals == []


def test_log_id_must_be_a_uuid(store):
    with pytest.raises(ValueError):
        _ctx(OWNER, store).request_log_deletion("直近の私事の記録", "private")


# --- 証跡（内容は載せない・止めない） -----------------------------------------

def test_the_withdrawal_is_trailed_without_the_summary(store):
    trail = InMemoryToolEventStore()
    log_id = _row(store, "p_owner")
    _ctx(OWNER, store, trail).request_log_deletion(log_id, "other_org")
    events = [r for r in trail.rows if r["tool"] == "usage_withdrawn"]
    assert [r["detail"] for r in events] == [f"other_org:{log_id}"]
    assert not any("要約" in str(r) for r in trail.rows)


def test_a_broken_trail_does_not_keep_private_content_visible(store):
    """discard と失敗の向きが逆であることの固定 — 証跡が落ちても取り下げは通す."""

    class Broken(InMemoryToolEventStore):
        def record(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("trail down")

    log_id = _row(store, "p_owner")
    _ctx(OWNER, store, Broken()).request_log_deletion(log_id, "private")
    assert [w["log_id"] for w in store.withdrawals] == [log_id]


# --- 一覧と閲覧の境界 --------------------------------------

def test_the_list_does_not_hand_back_the_content(store):
    """一覧は「特定と仕分け」まで。要約は配らない.

    この集合は**本人が見て「これは残せない」と判断した要約**そのもの。一覧ツールが最新分を
    まとめて外部モデルの文脈に流し込む形にはしない。
    """
    log_id = _row(store, "p_owner")
    _ctx(OWNER, store).request_log_deletion(log_id, "private")
    rows = _ctx(STEWARD, store).list_withdrawn_usage()["rows"]
    assert "summary" not in rows[0]
    assert "要約" not in str(rows)
    # `other_org` が actor_id の隣に並ぶ一覧を作らない
    assert "reason" not in rows[0]


def test_reading_one_row_returns_the_summary_and_is_always_trailed(store):
    """内容を読む段は1件ずつ・理由必須・log_id つきで監査に残る."""
    trail = InMemoryToolEventStore()
    log_id = _row(store, "p_owner")
    _ctx(OWNER, store).request_log_deletion(log_id, "private")
    out = _ctx(STEWARD, store, trail).get_withdrawn_usage(log_id, "インシデント#12 の調査")
    assert out["row"]["summary"] == "要約"
    assert out["row"]["reason"] == "private"
    reads = [r for r in trail.rows if r["tool"] == "withdrawn_usage_read"]
    assert [r["detail"] for r in reads] == [log_id]      # どの記録を読んだかが残る
    assert not any("インシデント" in str(r) for r in trail.rows)   # 理由の本文は残さない


def test_reading_one_row_requires_a_reason_and_the_steward_role(store):
    log_id = _row(store, "p_owner")
    _ctx(OWNER, store).request_log_deletion(log_id, "private")
    with pytest.raises(ValueError):
        _ctx(STEWARD, store).get_withdrawn_usage(log_id, "   ")
    with pytest.raises(PermissionError):
        _ctx(OTHER, store).get_withdrawn_usage(log_id, "見たい")


def test_reading_one_row_is_fail_closed_on_a_broken_trail(store):
    """取り下げ(best-effort)と逆向き — 読む側は記録できないなら読ませない."""

    class Broken(InMemoryToolEventStore):
        def record(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("trail down")

    log_id = _row(store, "p_owner")
    _ctx(OWNER, store).request_log_deletion(log_id, "private")
    with pytest.raises(RuntimeError):
        _ctx(STEWARD, store, Broken()).get_withdrawn_usage(log_id, "調査")


def test_rows_written_before_the_sub_was_bound_are_still_withdrawable(store):
    """person_id が付く前に書かれた行を、本人が取り下げられること.

    これが無いと、**actor_id が解決されていない行だけ**が永久に取り下げ不能になる。
    R6 の約束が、いちばん必要な相手に対してだけ成立しない形。
    """
    from waca_loop.auth.identity import hash_subject

    unresolved = store.record(
        UsageRecord(
            actor_id=None,
            actor_raw=hash_subject("s1"),      # OWNER の subject
            client="claude_code",
            use_category=UseCategory.L1,
            data_kinds=["internal"],
            summary="要約",
            ingested_via="mcp",
        )
    )
    assert _ctx(OWNER, store).request_log_deletion(unresolved, "private")["withdrawn"] is True


def test_another_persons_unresolved_row_is_still_refused(store):
    """上の経路が、他人の未解決の行まで開けてしまっていないこと（負の確認）."""
    from waca_loop.auth.identity import hash_subject

    unresolved = store.record(
        UsageRecord(
            actor_id=None, actor_raw=hash_subject("s2"),   # OTHER の subject
            client="claude_code", use_category=UseCategory.L1,
            data_kinds=["internal"], summary="要約", ingested_via="mcp",
        )
    )
    with pytest.raises(ValueError):
        _ctx(OWNER, store).request_log_deletion(unresolved, "private")
    assert store.withdrawals == []


def test_a_refused_attempt_is_trailed_as_refused(store):
    """起きなかった取り下げを「起きた」と記録しない."""
    trail = InMemoryToolEventStore()
    log_id = _row(store, "p_owner")
    with pytest.raises(ValueError):
        _ctx(OTHER, store, trail).request_log_deletion(log_id, "private")
    tools = [r["tool"] for r in trail.rows]
    assert "usage_withdraw_refused" in tools
    assert "usage_withdrawn" not in tools


def test_the_log_id_is_canonicalised_before_it_reaches_the_trail(store):
    """uuid.UUID は urn: や波括弧を受けるので、生の文字列を残さない."""
    trail = InMemoryToolEventStore()
    log_id = _row(store, "p_owner")
    _ctx(OWNER, store, trail).request_log_deletion(f"urn:uuid:{log_id}", "private")
    details = [r["detail"] for r in trail.rows if r["tool"] == "usage_withdrawn"]
    assert details == [f"private:{log_id}"]
    assert not any("urn:" in str(r) for r in trail.rows)


def test_the_store_refuses_a_free_text_reason_too(store):
    """符号の閉じ方も ACL と同じく二重に持つ."""
    log_id = _row(store, "p_owner")
    with pytest.raises(ValueError):
        store.withdraw(log_id, actor_id="p_owner", actor_raw=None, reason="なんとなく")


def test_the_ledger_row_is_never_edited(store):
    """設計そのものの固定 — 取り下げは追記であって、台帳行の書き換えではない.

    本番の BigQuery は挿入直後の行を UPDATE できない（＝取り下げが最も必要な瞬間に失敗する）。
    台帳行に触らないことが、その回避策。
    """
    log_id = _row(store, "p_owner")
    before = dict(store.rows[0])
    _ctx(OWNER, store).request_log_deletion(log_id, "private")
    assert store.rows[0] == before


# --- 取り下げと物理削除・集計 ---------------------------------------------

def test_the_steward_cannot_discard_a_withdrawn_row(store):
    """取り下げ後の物理削除で ②（行は残る）が壊れないこと.

    ぶつかり方はごく普通のもの — 行が pending になる → 本人が取り下げる →
    その前に一覧を作っていたスチュワードが discard を押す。誰も選んでいない競合で、
    インシデント調査が辿れるはずの唯一の写しが消える。
    """
    log_id = _row(store, "p_owner")
    _ctx(OWNER, store).request_log_deletion(log_id, "private")
    with pytest.raises(ValueError):
        _ctx(STEWARD, store).review_pending_usage(log_id, "discard")
    assert len(store.rows) == 1
    assert _ctx(STEWARD, store).get_withdrawn_usage(log_id, "調査")["row"]["summary"] == "要約"


def test_the_operator_report_excludes_withdrawn_rows():
    """運用卓の集計も取り下げ行を数えない.

    SQL を組み立てる関数を直接見る。
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "coverage_report.py"
    spec = importlib.util.spec_from_file_location("coverage_report", path)
    assert spec and spec.loader
    coverage_report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(coverage_report)

    sql = coverage_report.build_ledger_sql(
        "proj.ds.usage_log", 7, "proj.ds.usage_withdrawals"
    )
    assert "usage_withdrawals" in sql
    assert "NOT IN" in sql


def test_duplicate_withdrawals_do_not_crowd_the_stewards_list(store):
    """同時取り下げの重複で、他人の取り下げ行を一覧から押し出せない.

    本番では2つの `INSERT … SELECT` が互いのコミット前のスナップショットを読むため、
    同じ `log_id` の取り下げが二重に入りうる。隠す動作には影響しない（集合演算）が、
    一覧は log_id ごとに1行でなければ、自分の行への連打で**他人の取り下げを10件の枠から
    追い出せる**（監視の回避）。in-memory では再現できないので、意味のほうを固定する。
    """
    from waca_loop.ledger.usage_log import _withdrawal_row

    mine = _row(store, "p_owner")
    _ctx(OWNER, store).request_log_deletion(mine, "private")
    # 本番でだけ起きる重複を、手で作って同じ状態にする
    for _ in range(12):
        store.withdrawals.append(
            _withdrawal_row(mine, "p_owner", None, "private", _utc())
        )
    theirs = store.record(
        UsageRecord(actor_id="p_other", actor_raw=None, client="claude_code",
                    use_category=UseCategory.L1, data_kinds=["internal"],
                    summary="別の要約", ingested_via="mcp")
    )
    _ctx(OTHER, store).request_log_deletion(theirs, "private")

    rows = _ctx(STEWARD, store).list_withdrawn_usage()["rows"]
    ids = [r["log_id"] for r in rows]
    assert ids.count(mine) == 1
    assert theirs in ids, "重複で他人の取り下げが一覧から押し出されている"


def test_the_bigquery_twin_dedupes_in_sql_too():
    """双子が同じ意味であること — 片方だけ重複を潰しても意味が揃わない."""
    import inspect

    from waca_loop.ledger.usage_log import BigQueryUsageLogStore

    sql = inspect.getsource(BigQueryUsageLogStore._withdrawal_refs_sql)
    assert "GROUP BY log_id" in sql


def test_withdrawn_reads_filter_partitions_without_losing_old_rows():
    """残作業E: 両表を窓で走査し、古い取り下げ行も既知IDから読める形にする."""
    import inspect
    import re
    from types import SimpleNamespace

    from waca_loop.ledger.usage_log import BigQueryUsageLogStore

    store = BigQueryUsageLogStore.__new__(BigQueryUsageLogStore)
    store._settings = SimpleNamespace(  # noqa: SLF001
        usage_log_table="p.d.usage_log",
        usage_withdrawals_table="p.d.usage_withdrawals",
    )
    list_ref_sql = store._withdrawal_refs_sql(one=False)  # noqa: SLF001
    get_ref_sql = store._withdrawal_refs_sql(one=True)  # noqa: SLF001
    usage_sql = store._usage_rows_for_withdrawals_sql(  # noqa: SLF001
        ("log_id", "occurred_at", "withdrawn_at")
    )

    for sql in (list_ref_sql, get_ref_sql):
        assert re.search(r"WHERE\s+withdrawn_at\s+>=\s+@part_start\b", sql)
        assert re.search(r"AND\s+withdrawn_at\s+<\s+@part_end\b", sql)
    assert "AND log_id = @id" in get_ref_sql

    assert "FROM UNNEST(@log_ids)" in usage_sql
    assert re.search(r"WHERE\s+l\.occurred_at\s+>=\s+@part_start\b", usage_sql)
    assert re.search(r"AND\s+l\.occurred_at\s+<\s+@part_end\b", usage_sql)

    assert "_partition_windows()" in inspect.getsource(BigQueryUsageLogStore.list_withdrawn)
    assert "_partition_windows()" in inspect.getsource(BigQueryUsageLogStore._withdrawal_refs)
    assert "_partition_windows()" in inspect.getsource(
        BigQueryUsageLogStore._usage_rows_for_withdrawals
    )


def _query_params(job_config):
    return {
        p.name: getattr(p, "value", getattr(p, "values", None))
        for p in job_config.query_parameters
    }


def test_get_withdrawn_walks_to_an_older_usage_log_partition():
    """古い台帳行を今日取り下げたケースを、SQL形状だけでなく制御フローで固定する."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from waca_loop.ledger.usage_log import BigQueryUsageLogStore

    new_start = datetime(2026, 8, 1, tzinfo=UTC)
    new_end = datetime(2026, 9, 1, tzinfo=UTC)
    old_start = datetime(2026, 1, 1, tzinfo=UTC)
    old_end = datetime(2026, 2, 1, tzinfo=UTC)

    class Job:
        def __init__(self, rows):
            self._rows = rows

        def result(self):
            return self._rows

    class Client:
        def __init__(self):
            self.usage_starts = []

        def query(self, sql, job_config=None):  # noqa: ANN001
            params = _query_params(job_config)
            if "usage_withdrawals" in sql:
                assert params["part_start"] == new_start
                return Job([{
                    "log_id": "log_old",
                    "withdrawn_at": "2026-08-15T00:00:00+00:00",
                    "reason": "private",
                }])
            self.usage_starts.append(params["part_start"])
            if params["part_start"] == old_start:
                return Job([{
                    "log_id": "log_old",
                    "occurred_at": "2026-01-15T00:00:00+00:00",
                    "actor_id": "p_owner",
                    "client": "codex-cli",
                    "use_category": "l1",
                    "summary": "要約",
                }])
            return Job([])

    client = Client()
    store = BigQueryUsageLogStore(
        client,
        SimpleNamespace(
            usage_log_table="p.d.usage_log",
            usage_withdrawals_table="p.d.usage_withdrawals",
            usage_log_retention_days=1,
        ),
    )
    store._partition_windows = lambda: iter(((new_start, new_end), (old_start, old_end)))  # noqa: SLF001

    got = store.get_withdrawn("log_old")
    assert got["summary"] == "要約"
    assert client.usage_starts == [new_start, old_start]


def test_list_withdrawn_paginates_within_a_withdrawal_partition_before_aging_out():
    """同じ withdrawal 窓の最初の候補が欠けても、次ページの新しい候補を飛ばさない."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from waca_loop.ledger.usage_log import BigQueryUsageLogStore

    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)

    class Job:
        def __init__(self, rows):
            self._rows = rows

        def result(self):
            return self._rows

    class Client:
        def __init__(self):
            self.offsets = []

        def query(self, sql, job_config=None):  # noqa: ANN001
            params = _query_params(job_config)
            if "usage_withdrawals" in sql:
                self.offsets.append(params["scan_offset"])
                if params["scan_offset"] == 0:
                    return Job([
                        {
                            "log_id": f"missing_{i}",
                            "withdrawn_at": f"2026-08-{i + 1:02d}T00:00:00+00:00",
                            "reason": "private",
                        }
                        for i in range(10)
                    ])
                return Job([
                    {"log_id": "good_1", "withdrawn_at": "2026-08-20T00:00:00+00:00",
                     "reason": "private"},
                    {"log_id": "good_2", "withdrawn_at": "2026-08-19T00:00:00+00:00",
                     "reason": "mistake"},
                ])
            wanted = set(params["log_ids"])
            return Job([
                {
                    "log_id": log_id,
                    "occurred_at": "2026-08-10T00:00:00+00:00",
                    "actor_id": "p_owner",
                    "client": "codex-cli",
                    "use_category": "l1",
                }
                for log_id in sorted(wanted)
                if log_id.startswith("good_")
            ])

    client = Client()
    store = BigQueryUsageLogStore(
        client,
        SimpleNamespace(
            usage_log_table="p.d.usage_log",
            usage_withdrawals_table="p.d.usage_withdrawals",
            usage_log_retention_days=1,
        ),
    )
    store._partition_windows = lambda: iter(((start, end),))  # noqa: SLF001

    rows = store.list_withdrawn(2)
    assert [r["log_id"] for r in rows] == ["good_1", "good_2"]
    assert client.offsets == [0, 10]


def test_partition_windows_clamp_tiny_retention_values():
    """usage_log_retention_days=1 の誤設定で、1日刻みの大量ジョブにしない."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from waca_loop.ledger.usage_log import BigQueryUsageLogStore

    store = BigQueryUsageLogStore.__new__(BigQueryUsageLogStore)
    store._settings = SimpleNamespace(usage_log_retention_days=1)  # noqa: SLF001
    store._scan_floor = datetime.now(UTC) - timedelta(days=800)  # noqa: SLF001

    windows = list(store._partition_windows())  # noqa: SLF001
    assert len(windows) == 3
