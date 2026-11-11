"""T9 — 署名: 出力に委任タグ＋末尾署名が必ず付く（システムプロンプト経由）(§8)."""

from __future__ import annotations

from waca_loop.signature.prompt import (
    DELEGATION_AI,
    DELEGATION_HUMAN,
    SIGNATURE_LINE,
    SYSTEM_PROMPT_RULES,
    has_signature,
    wrap_output,
)


def test_system_prompt_contains_tags_and_signature():
    assert DELEGATION_AI in SYSTEM_PROMPT_RULES
    assert DELEGATION_HUMAN in SYSTEM_PROMPT_RULES
    assert SIGNATURE_LINE in SYSTEM_PROMPT_RULES
    assert "log_usage" in SYSTEM_PROMPT_RULES  # rule 1 — called every prompt
    assert "lookup_terms" in SYSTEM_PROMPT_RULES  # rule 2


def test_wrap_output_adds_tag_and_signature():
    out = wrap_output("本文です。", requires_human=True, interpretation_note="売上を月次")
    assert out.startswith(DELEGATION_HUMAN)
    assert out.rstrip().endswith(SIGNATURE_LINE)
    assert has_signature(out)


def test_has_signature_detects_missing_parts():
    assert not has_signature("本文だけ")
    assert not has_signature(DELEGATION_AI + " 本文")  # tag but no signature
    assert has_signature(DELEGATION_AI + " 本文 " + SIGNATURE_LINE)
