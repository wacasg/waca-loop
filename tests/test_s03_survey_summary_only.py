"""S3 — 要約のみ: 逐語原文が survey_responses に保存されない."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from waca_loop.models import SurveyResponse

_FORBIDDEN = {"prompt", "raw", "raw_text", "verbatim", "transcript", "messages", "input_text"}
DDL = Path(__file__).resolve().parent.parent / "sql" / "ddl"


def _strip_sql_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines()).lower()


def test_survey_response_model_has_no_verbatim_field():
    names = {f.name for f in dataclasses.fields(SurveyResponse)}
    assert _FORBIDDEN.isdisjoint(names), f"verbatim field leaked: {names}"


def test_survey_ddl_has_no_verbatim_column():
    sql = _strip_sql_comments((DDL / "07_survey.sql").read_text(encoding="utf-8"))
    for forbidden in ("prompt", "raw_text", "verbatim", "transcript"):
        assert forbidden not in sql, f"survey DDL must not have a {forbidden!r} column"
