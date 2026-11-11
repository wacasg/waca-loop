"""T8 — 監視: active なのに台帳ゼロの人物を missing として検出 (§8)."""

from __future__ import annotations

from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.models import UsageRecord, UseCategory
from waca_loop.monitor.positive_list import positive_list_check, run_positive_list_check


def test_pure_set_difference():
    missing = positive_list_check({"a", "b", "c"}, {"a"})
    assert missing == {"b", "c"}


def test_active_but_silent_person_flagged(person_store):
    usage = InMemoryUsageLogStore()
    # Only p_sato produced a ledger row; p_tanaka (active) is silent.
    usage.record(
        UsageRecord(
            use_category=UseCategory.L2,
            data_kinds=["public"],
            summary="x",
            client="claude_code",
            actor_id="p_sato",
        )
    )
    report = run_positive_list_check(person_store, usage, window_days=7)

    # p_frozen is not active -> not expected; p_tanaka active+silent -> missing.
    assert "p_tanaka" in report.missing
    assert "p_sato" not in report.missing
    assert "p_frozen" not in report.expected
    assert 0.0 <= report.coverage <= 1.0
