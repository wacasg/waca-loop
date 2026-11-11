

def test_retrieval_without_any_record_is_flagged_silent():
    """R3b: 検索したのに記録が1件も無い actor/日 を silent として出すこと.

    policy_check − usage_recorded の指標だけでは、「ポリシーは見たのに記録しなかった」
    しか捕まえられない。policy_check も log_usage も**一度も呼ばない**クライアントは
    両方0・不整合0となり、正常に見えてしまう（数時間検索し続けても台帳の行は1件、など）。
    """
    from waca_loop.ledger.tool_events import coverage_mismatch

    rows = [
        {"occurred_at": "2026-08-13T10:00:00", "tool": "retrieval", "actor_id": "p_x"},
        {"occurred_at": "2026-08-13T10:05:00", "tool": "retrieval", "actor_id": "p_x"},
    ]
    bucket = coverage_mismatch(rows)["2026-08-13/p_x"]
    assert bucket["retrieval"] == 2
    assert bucket["usage_recorded"] == 0
    assert bucket["mismatch"] == 0      # mismatch の指標だけでは見えない
    assert bucket["silent"] == 1        # R3b で見える


def test_recording_even_once_clears_silent():
    """検索が何度あっても、記録が1件でもあれば silent ではない(件数差は取らない).

    1ターンが複数回の検索に展開されて log_usage は1回、というのが正常な形なので、
    retrieval − usage_recorded の差はノイズになる。合図はゼロであること。
    """
    from waca_loop.ledger.tool_events import coverage_mismatch

    rows = [
        {"occurred_at": "2026-08-13T10:00:00", "tool": "retrieval", "actor_id": "p_y"},
        {"occurred_at": "2026-08-13T10:01:00", "tool": "retrieval", "actor_id": "p_y"},
        {"occurred_at": "2026-08-13T10:02:00", "tool": "retrieval", "actor_id": "p_y"},
        {"occurred_at": "2026-08-13T10:03:00", "tool": "usage_recorded", "actor_id": "p_y"},
    ]
    assert coverage_mismatch(rows)["2026-08-13/p_y"]["silent"] == 0
