"""組織設定（org.toml）の読み込みと、CHANGE-ME が残っているときの起動拒否."""

from __future__ import annotations

from pathlib import Path

import pytest

from waca_loop.org import (
    OrgConfigError,
    load_org_config,
    parse_org_config,
    problems,
    require_complete,
)

ROOT = Path(__file__).resolve().parent.parent


def test_the_template_is_not_usable_until_filled_in():
    """テンプレートのままでは起動しない — 直す場所が名指しされること."""
    cfg = load_org_config(ROOT / "org.example.toml")
    found = problems(cfg)
    assert any("[org] name" in p for p in found)
    assert any("[org] workspace_domains" in p for p in found)
    with pytest.raises(OrgConfigError, match="CHANGE-ME"):
        require_complete(cfg)


def test_the_sample_profile_is_complete():
    """sample/org.toml はそのまま試せる（CHANGE-ME が残っていない）."""
    cfg = load_org_config(ROOT / "sample" / "org.toml")
    assert problems(cfg) == []
    assert cfg.name == "サンプル協会"
    assert cfg.unit_map["教材開発部"] == "kyozai"


def test_a_missing_file_falls_back_to_defaults_but_is_reported(tmp_path):
    cfg = load_org_config(tmp_path / "nope.toml")
    assert cfg.source is None and cfg.units.org == "org"
    assert problems(cfg) and "見つかりません" in problems(cfg)[0]


def test_unit_map_keys_are_case_insensitive_and_domains_lowercased():
    cfg = parse_org_config({
        "org": {"name": "X", "workspace_domains": ["Example.ORG"]},
        "unit_map": {"委員会B": "c_b"},
    })
    assert cfg.workspace_domains == frozenset({"example.org"})
    assert cfg.unit_map["委員会b"] == "c_b"


def test_wrong_types_are_rejected_with_a_readable_message():
    with pytest.raises(OrgConfigError, match="文字列の配列"):
        parse_org_config({"org": {"workspace_domains": "example.org"}})
    with pytest.raises(OrgConfigError, match=r"\[unit_map\]"):
        parse_org_config({"unit_map": {"教材開発部": 1}})


def test_the_test_suite_runs_on_the_fictional_profile():
    """テストは開発者の手元の org.toml ではなく tests/org.test.toml を読む（conftest）."""
    from waca_loop.org import get_org_config

    assert get_org_config().source == ROOT / "tests" / "org.test.toml"


def test_org_specific_id_columns_are_treated_as_personal_data():
    """組織固有の会員番号の列名（[pii] id_column_names）も個人データとして扱う."""
    from waca_loop.ingest.anonymize import is_forbidden_field
    from waca_loop.pii import detect_pii_columns

    hits = detect_pii_columns(["サンプル番号", "受講日"])
    assert hits == [{"column": "サンプル番号", "category": "個人ID"}]
    assert is_forbidden_field("サンプル番号")
    assert not is_forbidden_field("受講日")


def test_pii_id_columns_reject_wrong_types_and_placeholders():
    with pytest.raises(OrgConfigError, match="文字列の配列"):
        parse_org_config({"pii": {"id_column_names": "会員コード"}})
    cfg = parse_org_config({"org": {"name": "X"}, "pii": {"id_column_names": ["CHANGE-ME"]}},
                           source=Path("x.toml"))
    assert any("[pii]" in p for p in problems(cfg))


def test_org_specific_id_values_are_masked_in_text():
    """組織固有の会員番号の形式（[pii] id_value_patterns）に当たる本文は [会員番号] に置き換わる."""
    from waca_loop.ingest.anonymize import scrub_text

    result = scrub_text("SMP-123456 の件")
    assert result.text == "[会員番号] の件"
    assert result.findings == {"member_id": 1}


def test_member_id_format_is_not_built_in():
    """会員番号の形式はコードに組み込まない — 設定が無ければ伏せない（他の検出器は働く）."""
    from waca_loop.ingest.anonymize import scrub_text
    from waca_loop.org import get_org_config, set_org_config

    saved = get_org_config()
    try:
        set_org_config(parse_org_config({}))
        result = scrub_text("SMP-123456 / a@example.org")
        assert "SMP-123456" in result.text
        assert result.findings == {"email": 1}
    finally:
        set_org_config(saved)


def test_id_value_patterns_must_be_valid_regexes():
    with pytest.raises(OrgConfigError, match="正規表現"):
        parse_org_config({"pii": {"id_value_patterns": ["ABC[0-9"]}})
    cfg = parse_org_config({"pii": {"id_value_patterns": ["CHANGE-ME"]}}, source=ROOT / "x.toml")
    assert any("[pii] id_value_patterns" in p for p in problems(cfg))
