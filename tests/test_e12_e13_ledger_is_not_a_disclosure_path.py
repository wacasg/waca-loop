"""E12(b) 計器盤搭載 ／ E13 台帳が開示経路にならない（SPEC_phase3 §4f）.

`find_expert` は台帳に**載る**（誰も見ていない機能にしない）。しかしその記録に
**候補者名・person_id を入れない**。入れると「誰が誰を探したか」が台帳から読め、
E6 のサイレント非対称開示が台帳経由で破れる。

「誰が探したか」は `ern_audit` にだけ残り、それを読み出す MCP ツールは作らない。
つまり組み合わせて初めて意味を持つ2つの記録が、**別々の場所に、別々の到達性で**置かれる。
"""

from __future__ import annotations

from waca_loop.models import FbType


def test_e12b_the_call_lands_on_the_instrument_trail(ern_context, ern_seed_fn):
    """A(正): 呼び出しが tool_events に載る（計器盤に出る）."""
    ern_seed_fn(ern_context, authors=("p_ok",))
    ern_context.find_expert("採点基準")

    tools = [r["tool"] for r in ern_context.tool_event_store.rows]
    assert "find_expert" in tools


def test_e13_the_trail_carries_no_candidate_identifier(ern_context, ern_seed_fn):
    """C(負): 台帳の行に候補者の person_id も表示名も無い."""
    ern_seed_fn(ern_context, authors=("p_ok", "p_ok2"), fb_type=FbType.DECISION)
    ern_context.find_expert("採点基準")

    blob = str(ern_context.tool_event_store.rows)
    for leaked in ("p_ok", "p_ok2", "同意 太郎", "同意 次郎"):
        assert leaked not in blob, f"台帳に候補者の識別子が漏れている: {leaked}"


def test_e13_the_trail_carries_no_topic_text(ern_context, ern_seed_fn):
    """C(負): 領域ラベルそのものも台帳に入れない.

    領域は「何を探したか」で、候補者名と突き合わせなくても、繰り返せば
    「誰がどの分野を調べているか」の一覧になる。detail は長さだけにしてある。
    """
    ern_seed_fn(ern_context, authors=("p_ok",))
    ern_context.find_expert("懲戒処分の基準")

    blob = str(ern_context.tool_event_store.rows)
    assert "懲戒処分" not in blob


def test_the_pairing_lives_only_in_the_audit_table(ern_context, ern_seed_fn):
    """A(正): 突き合わせに要る2つが、別々の場所に置かれている.

    台帳 = 「find_expert が呼ばれた」まで。監査 = 「誰が誰を候補にしたか」。
    後者を読む道具は無い（E6 で表明済み）。
    """
    ern_seed_fn(ern_context, authors=("p_ok",))
    ern_context.find_expert("採点基準")

    trail = str(ern_context.tool_event_store.rows)
    audit = ern_context.ern_audit_store.rows[0]

    assert "find_expert" in trail and "p_ok" not in trail
    assert audit.actor_id == "p_asker" and audit.candidate_ids == ["p_ok"]


def test_an_empty_result_still_lands_on_the_trail(ern_context):
    """A(正): 候補ゼロでも記録は残る（"何も出なかった" も観測対象）."""
    ern_context.find_expert("誰も知らない領域")
    assert any(r["tool"] == "find_expert" for r in ern_context.tool_event_store.rows)


def test_a_failing_audit_write_does_not_break_the_search(ern_context, ern_seed_fn):
    """C(負): 証跡が書けなくても人検索は落ちない — ただし黙って握り潰さない.

    ここは `_clamp_k` の break-glass と**逆**の判断である。break-glass は「記録が
    残せないなら超過も許さない」。find_expert は読み取りで、記録の失敗で答えが
    返らなくなるほうが害が大きい。代わりに warning を出す。
    """
    import logging

    class _Broken:
        def record(self, **kwargs):
            raise RuntimeError("audit sink down")

    ern_seed_fn(ern_context, authors=("p_ok",))
    ern_context.ern_audit_store = _Broken()

    with_logs = logging.getLogger("waca_loop.server")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    with_logs.addHandler(handler)
    try:
        out = ern_context.find_expert("採点基準")
    finally:
        with_logs.removeHandler(handler)

    assert out["candidates"], "証跡の失敗で検索まで止まっている"
    assert any("ern_audit" in r.getMessage() for r in records), "黙って握り潰している"


def test_a_refused_call_is_still_recorded(ern_context):
    """A(正): 非メンバーの試みも計器盤に残る（開示ゼロでも観測はする）.

    開示はゼロなので漏れではないが、**いちばん見たいのは「人検索を試みた非メンバー」**で、
    そこだけ痕跡が残らないと観測の穴になる。
    """
    import dataclasses

    from waca_loop.models import Viewer

    outsider = dataclasses.replace(
        ern_context,
        viewer=Viewer(subject="x", person_id=None, is_org_member=False, units=frozenset()),
    )
    assert outsider.find_expert("採点基準")["candidates"] == []

    rows = [r for r in ern_context.tool_event_store.rows if r["tool"] == "find_expert"]
    assert len(rows) == 1
    assert "refused" in (rows[0]["detail"] or "")
    assert "採点基準" not in str(rows[0])      # 拒否の記録にも topic は入れない


def test_a_refusal_leaves_no_audit_row(ern_context):
    """C(負): 候補がゼロどころか判定にも至らないので、ern_audit には書かない."""
    import dataclasses

    from waca_loop.models import Viewer

    outsider = dataclasses.replace(
        ern_context,
        viewer=Viewer(subject="x", person_id=None, is_org_member=False, units=frozenset()),
    )
    outsider.find_expert("採点基準")
    assert ern_context.ern_audit_store.rows == []


def test_the_audit_retention_is_shorter_than_the_usage_ledger():
    """A(正): いちばん機微な表が、いちばん長く残る状態になっていない.

    `usage_log`（どのツールを使ったか）が 730 日で切れるのに、`ern_audit`
    （誰を探したか）が無期限、という逆向きの非対称にしない。
    """
    from pathlib import Path

    from waca_loop.config import Settings

    s = Settings()
    assert s.ern_audit_retention_days < s.usage_log_retention_days

    ddl = (Path(__file__).resolve().parent.parent / "sql" / "ddl" / "15_ern.sql").read_text(
        encoding="utf-8"
    )
    assert f"partition_expiration_days={s.ern_audit_retention_days}" in ddl, (
        "DDL の期限と config の値がずれている — 片方だけ直る形になっている"
    )
