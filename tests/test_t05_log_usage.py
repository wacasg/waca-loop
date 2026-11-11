"""T5 — log_usage 記録: INSERT成功、occurred_at/actor/use_category/summary が入る (§8)."""

from __future__ import annotations


def test_log_usage_records_row(context):
    out = context.log_usage(
        use_category="l1",
        data_kinds=["internal"],
        summary="顧客向け提案の下書きを作成した。",
        client="claude_code",
        request_id="req-1",
    )
    assert out["log_id"]

    rows = context.usage_store.rows
    assert len(rows) == 1
    row = rows[0]
    assert row["use_category"] == "l1"
    assert row["summary"] == "顧客向け提案の下書きを作成した。"
    assert row["occurred_at"]
    assert row["actor_id"] == "p_sato"  # resolved viewer
    assert row["client"] == "claude_code"
    assert row["data_kinds"] == ["internal"]
