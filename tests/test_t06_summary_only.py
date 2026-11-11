"""T6 — 要約のみ: 逐語の生プロンプトが台帳に保存されない (§8).

Enforced structurally: neither the UsageRecord model nor the row dict has any verbatim
field, and the log_usage tool has no parameter to pass raw text.
"""

from __future__ import annotations

import dataclasses
import inspect

from waca_loop.ledger.usage_log import InMemoryUsageLogStore, UsageSummary, _record_to_row
from waca_loop.models import UsageRecord, UseCategory
from waca_loop.server import ServerContext

_FORBIDDEN = {"prompt", "raw", "raw_text", "verbatim", "transcript", "messages", "input_text"}


def test_usage_record_has_no_verbatim_field():
    field_names = {f.name for f in dataclasses.fields(UsageRecord)}
    assert _FORBIDDEN.isdisjoint(field_names), f"verbatim field leaked into model: {field_names}"


def test_row_has_no_verbatim_key():
    rec = UsageRecord(
        use_category=UseCategory.L2,
        data_kinds=["public"],
        summary="要約のみ",
        client="claude_desktop",
    )
    row = _record_to_row(rec, "log-x", __import__("datetime").datetime(2026, 1, 1))
    assert _FORBIDDEN.isdisjoint(row.keys())
    assert row["summary"] == "要約のみ"


def test_log_usage_tool_signature_has_no_raw_param():
    params = set(inspect.signature(ServerContext.log_usage).parameters)
    assert _FORBIDDEN.isdisjoint(params)


def test_usage_summary_read_model_has_no_raw_field():
    field_names = {f.name for f in dataclasses.fields(UsageSummary)}
    assert _FORBIDDEN.isdisjoint(field_names)


def test_only_summary_carries_content():
    store = InMemoryUsageLogStore()
    rec = UsageRecord(
        use_category=UseCategory.L0,
        data_kinds=["pii"],
        summary="個人情報を含む依頼を匿名化方針で処理。",
        client="chatgpt",
    )
    store.record(rec)
    assert store.rows[0]["summary"]
