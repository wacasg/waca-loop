"""C1 — 未接続者と拒否ログの可視化（SPEC_coverage §3 C1・受け入れ AC-1）.

R3a 集計は `tool_events` を起点にするので**接続した人しか見えない**。登録したのに一度も
接続していない人は、この集計には現れない。ここでは「登録側から見る」ことと、「弾かれた
接続試行が見える」ことを固定する。
"""

from __future__ import annotations

import sys
from pathlib import Path

from waca_loop.ledger.tool_events import connection_funnel, rejected_subject_summary

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_ac1_people_who_never_connected_come_first():
    """未接続が先頭に出ること。**これが出ないと母数の問題に気づけない。**"""
    funnel = connection_funnel(
        registrations=[
            {"person_id": "p_active", "registered_on": "2026-06-30"},
            {"person_id": "p_new_a", "registered_on": "2026-08-13"},
            {"person_id": "p_new_b", "registered_on": "2026-08-20"},
        ],
        events=[{"actor_id": "p_active", "occurred_at": "2026-08-21", "tool": "usage_recorded"}],
    )
    assert [f["person_id"] for f in funnel] == ["p_new_a", "p_new_b", "p_active"], (
        "未接続が先頭・登録の古い順になっていない"
    )
    assert funnel[0]["connected"] is False
    assert funnel[-1]["connected"] is True


def test_ac1_a_registered_person_with_no_events_is_not_dropped():
    """左結合であること — events 起点だと未接続者が**そもそも行に現れない**（元の欠陥）."""
    funnel = connection_funnel(
        registrations=[{"person_id": "p_ghost", "registered_on": "2026-08-13"}], events=[]
    )
    assert len(funnel) == 1
    assert funnel[0]["events"] == 0
    assert funnel[0]["ledger_rows"] == 0
    assert funnel[0]["last_event"] is None


def test_the_funnel_counts_ledger_rows_per_person():
    funnel = connection_funnel(
        registrations=[{"person_id": "p_a"}, {"person_id": "p_b"}],
        events=[{"actor_id": "p_a", "occurred_at": "2026-08-20", "tool": "retrieval"}],
        ledger_rows=[{"actor_id": "p_a"}, {"actor_id": "p_a"}, {"actor_id": "p_b"}],
    )
    by = {f["person_id"]: f for f in funnel}
    assert by["p_a"]["ledger_rows"] == 2
    assert by["p_b"]["ledger_rows"] == 1
    assert by["p_b"]["connected"] is False   # 台帳に行があっても events ゼロなら未接続


def test_ac1_a_person_rejected_repeatedly_is_surfaced():
    """同じ sub が繰り返し弾かれていたら、回数と期間つきで出ること（AC-1 の後半）."""
    got = rejected_subject_summary([
        {"sub": "sub_1eff2bc", "occurred_at": "2026-08-17T01:00:00Z", "text": "x not in map"},
        {"sub": "sub_1eff2bc", "occurred_at": "2026-08-18T01:00:00Z", "text": "x not in map"},
        {"sub": "sub_1eff2bc", "occurred_at": "2026-08-18T09:00:00Z", "text": "x not in map"},
        {"sub": "sub_1eff2bc", "occurred_at": "2026-08-19T01:00:00Z", "text": "x not in map"},
    ])
    assert len(got) == 1
    assert got[0]["attempts"] == 4
    assert got[0]["first_day"] == "2026-08-17"
    assert got[0]["last_day"] == "2026-08-19"


def test_the_rejection_report_never_prints_a_raw_email():
    """FLOOR-01 — 拒否ログには生メールが出る。レポートは必ずマスクして返すこと.

    マスクは `ingest.anonymize` に委ねている（正規表現を書き直さない）。ここでは
    **出口で生値が消えていること**だけを見る — 委ねた先が変わっても、この表明は残る。
    """
    got = rejected_subject_summary([{
        "sub": "sub_x", "occurred_at": "2026-08-17",
        "text": "rejected sub for taro.yamada@mail.example.com (not in oauth_identity_map)",
    }])
    hint = got[0]["masked_hint"]
    assert "taro.yamada@mail.example.com" not in hint, "生のメールアドレスが出力に残っている"
    assert "mail.example.com" not in hint
    assert "oauth_identity_map" in hint, "何が起きたかは分かる形で残っていること"


def test_the_rejection_query_reads_cloud_run_not_bigquery():
    """拒否ログは Cloud Run にしかない（BQ を見ても永遠に出てこない）."""
    from coverage_report import rejection_log_command

    cmd = rejection_log_command("proj", 7)
    assert cmd[:3] == ["gcloud", "logging", "read"]
    assert any("not in oauth_identity_map" in c for c in cmd)
    assert any("waca-loop-serve" in c for c in cmd)
    assert "--project=proj" in cmd


def test_the_report_still_works_when_the_rejection_log_cannot_be_read():
    """gcloud の認証は切れがち。**ログが読めなくてもレポート本体は出す**こと."""
    from coverage_report import _read_rejections

    assert _read_rejections("no-such-project-xyz", 1) == []


# --- R7: 段の集計（SPEC_coverage §3 C5・受け入れ AC-5）--------------------------------


def _row(pid, ev, led, connected=None):
    """`connection_funnel` の出力の形だけを持つ最小の行.

    `connected` を**明示できる**ようにしてある。`ev > 0` から自動導出すると、「イベントは
    無いのに台帳行がある」という現実に起こる組み合わせを**構造的に作れず**、段の単調性の
    破れをテストで捕まえられない。テスト側のヘルパーが現実より狭いと、番人はそこを見ない。
    """
    return {
        "person_id": pid,
        "registered_on": "2026-08-01",
        "claimed": True,
        "events": ev,
        "first_event": None,
        "last_event": None,
        "ledger_rows": led,
        "connected": (ev > 0) if connected is None else connected,
    }


def _funnel(*rows):
    return [_row(*r) for r in rows]


def test_ac5_the_funnel_shows_where_people_fall_out_not_just_the_rate():
    """登録→接続→記録が**1つの並び**で見えること。段ごとの通過率つき."""
    from waca_loop.ledger.tool_events import funnel_summary

    # 10名登録、4名が接続、そのうち1名だけが記録している
    s = funnel_summary(_funnel(
        *[(f"p_ghost{i}", 0, 0) for i in range(6)],
        ("p_c1", 5, 0), ("p_c2", 3, 0), ("p_c3", 2, 0), ("p_writer", 9, 4),
    ))
    assert [x["stage"] for x in s["stages"]] == ["registered", "connected", "recording"]
    assert [x["people"] for x in s["stages"]] == [10, 4, 1]
    assert s["never_connected"] == 6
    assert s["connected_but_silent"] == 3, "接続したが1行も書いていない人が見えること"
    assert s["recording_rate"] == 0.25
    assert s["end_to_end_rate"] == 0.1, "登録から見ると1割。ここが手前の落ちを映す"


def test_ac5_the_rate_never_claims_to_be_the_per_turn_recording_rate():
    """**記録率を名乗ってよい相手を間違えない**こと（C5 が入るまで per-turn は出せない）.

    人ベースの通過率を「記録率」と読むと、1行でも書いた人は100%扱いになり、**同じ人が
    9ターン記録しなかった分が丸ごと消える**。SPEC_coverage §3 C5 が「真の分母は観測できない」
    と書いているのはこの穴のこと。旗を立てておかないと、ダッシュボードの数字が独り歩きする。
    """
    from waca_loop.ledger.tool_events import funnel_summary

    s = funnel_summary(_funnel(("p_writer", 100, 1)))
    assert s["per_turn_rate_available"] is False, (
        "ターンカウンタ（C5）が無いのに per-turn の記録率を出せると名乗っている"
    )
    assert s["recording_rate"] == 1.0  # 人ベースでは満点に見える…
    assert s["stages"][-1]["people"] == 1  # …が、意味は「1行でも書いた人が1名」でしかない


def test_ac5_no_one_registered_does_not_crash_or_invent_a_rate():
    """分母ゼロで 0 割りも「0%」の捏造もしないこと（新規組織の初日）."""
    from waca_loop.ledger.tool_events import funnel_summary

    s = funnel_summary([])
    assert s["stages"][0]["people"] == 0
    assert s["recording_rate"] is None, "分母が無いのに率を作ってはいけない"
    assert s["end_to_end_rate"] is None


def test_ac5_a_withdrawn_ledger_leaves_the_recording_stage():
    """取り下げ（R6）で台帳行が0になった人は、記録の段から**外れる**こと.

    `connection_funnel` の `ledger_rows` は取り下げ済みを除いた行数を渡される前提。
    ここは畳む側が「接続しているから記録者」と誤魔化さないことの確認。
    """
    from waca_loop.ledger.tool_events import funnel_summary

    s = funnel_summary(_funnel(("p_withdrew", 12, 0)))
    assert s["stages"][2]["people"] == 0
    assert s["connected_but_silent"] == 1


def test_ac5_the_stages_never_grow_as_you_go_down_the_funnel():
    """記録段が接続段を**上回らない**こと — ファネルは単調でなければ意味を成さない.

    `tool_events` の書き込みは best-effort（失敗を握り潰す）ので、`usage_recorded` イベントを
    取りこぼしたまま台帳行だけ残る人は実際に起こりうる。記録段を funnel 全体から独立に
    抽出すると、その1名で **通過率 200% ・「接続したが書いていない人」-1名** が出てしまう。
    """
    from waca_loop.ledger.tool_events import funnel_summary

    # イベントを取りこぼしたが台帳行はある人 + 正常な人
    s = funnel_summary([
        _row("p_lost_event", ev=0, led=3, connected=False),
        _row("p_ok", ev=5, led=1),
    ])
    people = [x["people"] for x in s["stages"]]
    assert people == sorted(people, reverse=True), f"段が下に行くほど増えている: {people}"
    assert people == [2, 1, 1]
    assert s["connected_but_silent"] >= 0, "「接続したが書いていない人」が負になっている"
    for st in s["stages"]:
        assert st["passed_from_prev"] is None or st["passed_from_prev"] <= 1.0, (
            f"{st['stage']} の通過率が 100% を超えている"
        )
