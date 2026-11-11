"""E3 — DDL に順位・総合スコア列が無い（INV-5）.

T10 と同型の SQL 検査。**データ側から**前提が崩れるのを止める — 出力の関門（E1）だけでは、
テーブルに `score` 列がある状態を許してしまい、次に誰かが「せっかくあるのだから返そう」と
思う余地が残る。
"""

from __future__ import annotations

import re
from pathlib import Path

DDL = Path(__file__).resolve().parent.parent / "sql" / "ddl" / "15_ern.sql"

#: 置いてはいけない列名。**列名として**現れることだけを見る（コメント中の説明は許す）。
FORBIDDEN = ("rank", "score", "rating", "order", "weight", "points", "ranking")


def _column_lines(sql: str) -> list[str]:
    """コメントを除いた、列定義らしい行だけ."""
    out = []
    for raw in sql.splitlines():
        line = raw.split("--", 1)[0].strip()
        if line and not line.upper().startswith(("CREATE", "PARTITION", "CLUSTER", "OPTIONS")):
            out.append(line)
    return out


def test_the_ern_ddl_exists():
    assert DDL.exists(), "sql/ddl/15_ern.sql が無い（SPEC_phase3 §1）"


def test_no_ranking_or_score_columns():
    """C(負): 順位・総合点の列を置かない."""
    for line in _column_lines(DDL.read_text(encoding="utf-8")):
        name = line.split()[0].lower().strip("(),`")
        for bad in FORBIDDEN:
            assert not re.fullmatch(rf"{bad}|.*_{bad}|{bad}_.*", name), (
                f"ERN の DDL に順位/総合点らしい列がある: {line!r} (E3/INV-5)"
            )


def test_both_ern_tables_are_declared():
    sql = DDL.read_text(encoding="utf-8")
    assert "ern_assertions" in sql
    assert "ern_audit" in sql


def test_no_extra_tables_sneak_in():
    """C(負): 増やしてよいのは「宣言」と「監査」の2つだけ（SPEC_phase3 §1）.

    シグナル表を別に作ると、同じ事実の写しが2つになり片方だけ古くなる — そして
    「誰がどれだけ貢献したか」を記録した表が独立に存在すること自体が INV-5 から遠い。
    """
    sql = DDL.read_text(encoding="utf-8")
    creates = re.findall(r"CREATE TABLE IF NOT EXISTS `[^`]*\.(\w+)`", sql)
    assert sorted(creates) == ["ern_assertions", "ern_audit"], creates


def test_the_dataclasses_carry_no_score_fields():
    """C(負): DDL だけでなく、行モデルにも順位/点数のフィールドが無い."""
    import dataclasses

    from waca_loop.ern.store import ErnAssertion, ErnAuditRow

    for cls in (ErnAssertion, ErnAuditRow):
        names = {f.name.lower() for f in dataclasses.fields(cls)}
        assert not (names & set(FORBIDDEN)), (cls.__name__, names & set(FORBIDDEN))
