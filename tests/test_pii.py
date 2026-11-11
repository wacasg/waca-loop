"""PII anonymization advisor (田中さんスキルのMCP移植) — detection, mapping, two-step flow.

The invariant under test everywhere: the tool works from column NAMES only and its
output never needs (or echoes) actual values.
"""

from __future__ import annotations

from waca_loop.models import Viewer
from waca_loop.pii import anonymization_spec, detect_pii_columns
from waca_loop.server import ServerContext


def _ctx(term_store, embedder) -> ServerContext:
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore

    viewer = Viewer(subject="s", person_id="p", is_org_member=True)
    return ServerContext(term_store=term_store, usage_store=InMemoryUsageLogStore(),
                         embedder=embedder, viewer=viewer)


def test_detects_typical_pii_columns():
    cols = ["氏名", "メールアドレス", "受験日", "合否", "電話番号", "住所"]
    detected = {d["column"]: d["category"] for d in detect_pii_columns(cols)}
    assert detected["氏名"] == "氏名"
    assert detected["メールアドレス"] == "メールアドレス"
    assert detected["住所"] == "住所"
    assert "受験日" not in detected and "合否" not in detected


def test_english_headers_detected():
    detected = {d["column"] for d in detect_pii_columns(["Email", "user_id", "score"])}
    assert detected == {"Email", "user_id"}


def test_spec_for_aggregation_deletes_and_generalizes():
    """目的A: 不要な識別子は削除、住所・生年月日は一般化(SKILL.md Step 4 の例と同型)."""
    out = anonymization_spec(["氏名", "メールアドレス", "住所", "生年月日", "受験日", "合否"], "A")
    by = {r["column"]: r for r in out["spec"]}
    assert by["氏名"]["method"] == "匿名化（削除）"
    assert by["住所"]["method"] == "匿名化（一般化）"
    assert by["生年月日"]["method"] == "匿名化（一般化）"
    assert by["受験日"]["method"] == "そのまま" and by["受験日"]["judgement"] == "対象外"
    assert any("k匿名化" in n for n in out["notes"])       # 公表時の観点


def test_spec_for_matching_hashes_keys():
    """目的C: 突合キー(メール等)はハッシュ化、他は削除."""
    out = anonymization_spec(["メールアドレス", "氏名"], "C")
    by = {r["column"]: r for r in out["spec"]}
    assert by["メールアドレス"]["method"] == "ハッシュ化"
    assert by["氏名"]["method"] == "匿名化（削除）"


def test_spec_for_roster_pseudonymizes_and_flags_floor():
    """目的D: 氏名は仮名化、実名が業務上必要なケースの注意に FLOOR-01 参照."""
    out = anonymization_spec(["氏名", "住所"], "D")
    by = {r["column"]: r for r in out["spec"]}
    assert by["氏名"]["method"] == "仮名化"
    assert any("FLOOR-01" in n for n in out["notes"])


def test_tool_two_step_flow(term_store, embedder):
    ctx = _ctx(term_store, embedder)
    # Step 1: no purpose → stop + ask (never proceeds silently).
    first = ctx.get_pii_anonymization_guidance(["氏名", "合否"])
    assert first["pii_detected"] is True and first["stop"] is True
    assert "A" in first["purposes"] and "利用目的" in first["message"]
    # Step 2: with purpose → spec table + handoff (execution out of scope).
    second = ctx.get_pii_anonymization_guidance(["氏名", "合否"], purpose="A")
    assert {r["column"] for r in second["spec"]} == {"氏名", "合否"}
    assert "匿名化" in second["handoff"]


def test_tool_passes_through_when_no_pii(term_store, embedder):
    ctx = _ctx(term_store, embedder)
    out = ctx.get_pii_anonymization_guidance(["受験日", "合否", "スコア"])
    assert out["pii_detected"] is False


def test_system_prompt_carries_pii_stop_rule():
    from waca_loop.signature.prompt import SYSTEM_PROMPT_RULES

    assert "get_pii_anonymization_guidance" in SYSTEM_PROMPT_RULES
    assert "FLOOR-01" in SYSTEM_PROMPT_RULES


# --- value guard: values passed as "columns" are refused ----------


def test_value_like_columns_flags_values_not_names():
    from waca_loop.pii import value_like_columns

    flagged = value_like_columns([
        "氏名",                                   # 0 — a real column name
        "yamada@example.com",                    # 1 — email value
        "tanaka＠example.com",                    # 2 — full-width ＠ (pasted from a sheet)
        "090-1234-5678",                         # 3 — phone value
        "09012345678",                           # 4 — phone, no separators
        "03(1234)5678",                          # 5 — phone, parenthesised
        "+81 90 1234 5678",                      # 6 — international phone
        "090／1234／5678",                        # 7 — full-width solidus
        "090_1234_5678",                         # 8 — underscore separator
        "090–1234–5678",                         # 9 — en dash separator
        "4111111111111111",                      # 10 — card number
        "4111 1111 1111 1111",                   # 11 — card number, spaced
        "123456789012",                          # 12 — マイナンバー (12 digits)
        "田中\t太郎",                             # 13 — pasted row
        "あ" * 41,                                # 14 — too long for a header
    ])
    assert [f["index"] for f in flagged] == list(range(1, 15))
    # The offending strings are never carried in the finding (no PII re-transmission).
    assert all(
        "example.com" not in str(f) and "4111" not in str(f) and "田中" not in str(f)
        for f in flagged
    )


def test_value_guard_does_not_fire_on_real_headers():
    """A false positive denies the whole request, pushing the model to skip the advisor —
    the FLOOR-01 outcome the guard exists to prevent. These must all pass through."""
    from waca_loop.pii import value_like_columns

    headers = [
        "氏名", "メールアドレス", "電話番号", "生年月日", "住所", "会員番号", "受講日", "合否",
        "備考: 任意", "URL: リンク", "Q1:売上", "開始:終了",          # colons are ordinary
        "受付時間 9:00-17:00", "比率 1:2", "得点(合計:100)",
        "更新日時 2026/01/01 12:00", "集計期間 2026/01/01-2026/12/31",  # two dates, one header
        "対象期間(2026/04/01～2027/03/31)", "西暦2026年度", "2026-08-01",
        "〒150-0041", "スコア(100点満点)", "売上(千円)", "e-mail",
    ]
    assert value_like_columns(headers) == []


def test_tool_refuses_values_and_does_not_echo_them(term_store, embedder):
    import pytest

    ctx = _ctx(term_store, embedder)
    with pytest.raises(ValueError) as exc:
        ctx.get_pii_anonymization_guidance(["氏名", "yamada@example.com"], purpose="A")
    message = str(exc.value)
    assert "FLOOR-01" in message and "列名のみ" in message
    assert "yamada@example.com" not in message      # ★ never echo the value back
    assert "2番目" in message                        # position only


def test_legitimate_column_names_still_work(term_store, embedder):
    """The guard must not cost the tool its normal job (false positives block real work)."""
    ctx = _ctx(term_store, embedder)
    out = ctx.get_pii_anonymization_guidance(
        ["氏名", "メールアドレス", "電話番号", "住所", "生年月日", "受講日", "合否"],
        purpose="A",
    )
    assert out["pii_detected"] is True
    assert len(out["spec"]) == 7


# --- 値ガードの抜け穴2件 -------------------------------------------------------


def test_long_numbers_punctuated_as_a_date_are_still_caught():
    """日付を引いてから数える規則が、まさに捕まえたい形の抜け穴にならないこと。

    `1234-56-7890`（マイナンバー）と `0312-34-5678`（固定電話）は `_DATE_RE` に一致するので
    まるごと削除されると、桁が数えられずに素通りしてしまう。
    """
    from waca_loop.pii import value_like_columns

    flagged = value_like_columns([
        "1234-56-7890",          # 0 — 12桁を日付の形に句切っただけ
        "0312-34-5678",          # 1 — 10桁の固定電話
        "2026-08-0112345678",    # 2 — 日付の後ろに値を続けた形
        "1990/01/01",            # 3 — 本物の日付（8桁）: 出てはいけない
    ])
    assert [f["index"] for f in flagged] == [0, 1, 2]
    assert all("1234" not in str(f) and "5678" not in str(f) for f in flagged)


def test_a_bare_string_is_not_walked_character_by_character():
    """`columns` に文字列を1本渡しても、`enumerate` で1文字ずつ回して規則を素通りしないこと。"""
    from waca_loop.pii import value_like_columns

    flagged = value_like_columns("氏名,090-1234-5678")
    assert len(flagged) == 1
    assert "090" not in str(flagged) and "1234" not in str(flagged)


def test_tool_refuses_a_bare_string_too(term_store, embedder):
    import pytest

    ctx = _ctx(term_store, embedder)
    with pytest.raises(ValueError) as exc:
        ctx.get_pii_anonymization_guidance("氏名,090-1234-5678", purpose="A")
    assert "FLOOR-01" in str(exc.value) and "090-1234-5678" not in str(exc.value)


def test_strip_normalizes_for_every_caller():
    """NFKC 正規化は `_strip` の中で行い、呼び出し側任せにしないこと。

    全角数字では差が出ない（`\\d` も `isalnum` も Unicode 対応なので正規化なしでも通る）。
    差が出るのは丸数字のような **Nd 以外の数字**で、NFKC を通して初めて `1` になる。
    正規化を `_strip` の外に出すとこの表明が落ちる。
    """
    from waca_loop.pii import _longest_digit_run

    assert _longest_digit_run("①②③④⑤⑥⑦⑧⑨⑩⑪") >= 10   # 正規化なしでは 0 桁
