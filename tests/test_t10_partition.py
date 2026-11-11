"""T10 — パーティション: usage_log が日付パーティションで作成される (§8).

DDL-shape test (no live BQ): the ledger table must be partitioned by DATE(occurred_at)
so window queries (monitor, retention) are efficient.
"""

from __future__ import annotations

from pathlib import Path

DDL = Path(__file__).resolve().parent.parent / "sql" / "ddl"


def test_usage_log_is_date_partitioned():
    sql = (DDL / "03_usage_log.sql").read_text(encoding="utf-8")
    normalized = " ".join(sql.split()).lower()
    assert "partition by date(occurred_at)" in normalized


def _strip_sql_comments(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        code = line.split("--", 1)[0]  # drop trailing -- comments (and comment-only lines)
        lines.append(code)
    return "\n".join(lines).lower()


def test_usage_log_has_no_verbatim_column():
    # Inspect column definitions only, not the explanatory comments.
    sql = _strip_sql_comments((DDL / "03_usage_log.sql").read_text(encoding="utf-8"))
    for forbidden in ("prompt", "raw_text", "verbatim", "transcript"):
        assert forbidden not in sql, f"ledger DDL must not have a {forbidden!r} column (T6)"


def test_terms_table_has_embedding_array():
    sql = (DDL / "01_terms.sql").read_text(encoding="utf-8").lower()
    assert "embedding" in sql and "array<float64>" in sql
