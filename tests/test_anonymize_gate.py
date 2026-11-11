"""Anonymization-gate acceptance tests. Offline."""

import inspect

from waca_loop.ingest import anonymize
from waca_loop.ingest.anonymize import (
    CALLCENTER_ALLOWED_FIELDS,
    GateStatus,
    gate_record,
    gate_records,
    scrub_text,
)

# Synthetic member id — deliberately not a plausible real one.
FAKE_MEMBER_ID = "SMP99990001"


def test_masks_every_kind() -> None:
    result = scrub_text(
        f"山田様({FAKE_MEMBER_ID}, yamada@example.com, 090-1234-5678, 〒123-4567, "
        "カード 4111-1111-1111-1111)から入電",
        roster_names=("山田太郎",),
    )
    for token in ("[会員番号]", "[メール]", "[電話番号]", "[郵便番号]", "[カード番号]"):
        assert token in result.text
    for raw in (FAKE_MEMBER_ID, "yamada@example.com", "090-1234-5678", "4111-1111-1111-1111"):
        assert raw not in result.text


def test_japanese_phone_spellings_are_caught() -> None:
    # Dot/paren separators, dash look-alikes between digits, +81 — all must mask.
    for text in (
        "電話は03(1234)5678です",
        "TEL 03‐1234‐5678",  # U+2010 hyphen
        "TEL 03−1234−5678",  # U+2212 minus
        "TEL 03ー1234ー5678",  # U+30FC between digits
        "TEL 090.1234.5678",
        "+81-90-1234-5678",
    ):
        result = scrub_text(text)
        assert "[電話番号]" in result.text, text
    # …but the long-vowel mark must survive in ordinary words.
    assert scrub_text("コールセンター対応").text == "コールセンター対応"


def test_bare_postal_and_spaced_member_id() -> None:
    assert "[郵便番号]" in scrub_text("住所は123-4567 東京都").text
    assert "[会員番号]" in scrub_text("SMP 99990001 の件").text


def test_nfkc_fullwidth_member_id_is_caught() -> None:
    result = scrub_text("会員番号はＳＭＰ９９９９０００１です")
    assert "[会員番号]" in result.text
    assert result.findings == {"member_id": 1}


def test_roster_variants_masked() -> None:
    # Space-variant roster entry still matches the concatenated spelling and tokens.
    result = scrub_text("山田 一郎が対応、山田に転送", roster_names=("山田 一郎",))
    assert "山田" not in result.text
    assert result.findings["name"] >= 1


def test_single_char_roster_name_does_not_corrupt_common_words() -> None:
    # roster に1文字の display_name ("林") が混じると、「林檎」「森林」「山林」等の
    # 地の文が [氏名] に部分一致で誤って化ける。1文字名は roster variant に採用しない。
    result = scrub_text(
        "林檎を買い、森林を散策し、山林を管理する。",
        roster_names=("林",),
    )
    assert "[氏名]" not in result.text
    assert result.text == "林檎を買い、森林を散策し、山林を管理する。"
    assert result.findings.get("name", 0) == 0


def test_findings_carry_counts_only_no_values() -> None:
    result = scrub_text("連絡先 rokuro@example.com")
    assert result.findings == {"email": 1}
    assert all(isinstance(v, int) for v in result.findings.values())
    assert "rokuro" not in repr(result.findings)


def test_scrub_is_idempotent_including_roster() -> None:
    once = scrub_text("03-1234-5678 山田一郎", roster_names=("山田一郎",))
    twice = scrub_text(once.text, roster_names=("山田一郎",))
    assert twice.text == once.text
    assert twice.findings == {}


def test_every_field_is_scrubbed_no_nontext_exemption() -> None:
    # Free text hides in classification/resolution columns too — no column skips the scrub.
    result = gate_record(
        {"case_id": "1", "resolution": "折り返し 090-1234-5678 へ"},
    )
    assert result.status is GateStatus.SCRUBBED
    assert result.record is not None
    assert "090-1234-5678" not in result.record["resolution"]


def test_unexpected_column_rejects_row_with_column_names_only() -> None:
    result = gate_record({"case_id": "1", "memo_extra": "山田様の件"})
    assert result.status is GateStatus.REJECTED
    assert result.record is None
    assert result.findings == {"unexpected_column": 1}
    assert result.rejected_columns == ("memo_extra",)
    # Column names are schema, not data — but values must never be echoed.
    assert "山田" not in repr(result)


def test_forbidden_column_rejected_even_when_caller_allowlists_it() -> None:
    # The denylist cannot be widened from the call site (FLOOR-01: no exemption path).
    for column in ("caller_name", "transcript", "電話番号", "会員番号"):
        result = gate_record(
            {column: "x"},
            allowed_fields=(column,),
        )
        assert result.status is GateStatus.REJECTED, column
        assert result.findings.get("forbidden_column") == 1


def test_non_string_value_rejects_instead_of_raising() -> None:
    result = gate_record({"case_id": "1", "received_at": 20260726})
    assert result.status is GateStatus.REJECTED
    assert result.findings == {"non_string_value": 1}


def test_clean_record_passes_untouched() -> None:
    result = gate_record({"case_id": "1", "question_subject": "講座の日程を知りたい"})
    assert result.status is GateStatus.CLEAN
    assert result.record == {"case_id": "1", "question_subject": "講座の日程を知りたい"}


def test_batch_result_shape_and_reconciliation_numbers() -> None:
    records: list[dict[str, object]] = [
        {"case_id": "1", "question_subject": "日程は?"},
        {"case_id": "2", "question_subject": f"{FAKE_MEMBER_ID} の件"},
        {"case_id": "3", "extra": "x"},
    ]
    batch = gate_records(records, roster_names=("山田一郎",))
    assert len(batch.records) == 2
    assert batch.status_tally == {"clean": 1, "scrubbed": 1, "rejected": 1}
    assert batch.kind_tally == {"member_id": 1}
    assert batch.reject_tally == {"unexpected_column": 1}
    assert batch.rejected_indices == (2,)
    assert batch.roster_size == 1
    # Status counts and detector kinds live in separate dicts — no key collision risk.
    assert set(batch.status_tally) == {s.value for s in GateStatus}
    assert FAKE_MEMBER_ID not in repr(batch)


def test_default_allowlist_is_the_module_constant() -> None:
    """The allowlist is the contract's anchor (this module constant ↔ the source_items DDL).

    Field names are spec-side, never source-side: an ingestion adapter renames before the gate.
    """
    assert set(CALLCENTER_ALLOWED_FIELDS) == {
        "case_id",
        "request_id",
        "received_at",
        "closed_at",
        "category",
        "sub_category",
        "classifications",
        "question_subject",
        "answer_summary",
        "resolution",
        "status",
        "channel",
        "priority",
        "is_escalated",
        "thread_count",
        "handled_by",
    }


def test_allowlist_survives_its_own_denylist() -> None:
    """Every allowed field must pass the denylist, or the gate rejects 100% of its own
    schema — guards the token list growing into the allowlist (e.g. adding "subject")."""
    from waca_loop.ingest.anonymize import is_forbidden_field

    for field in CALLCENTER_ALLOWED_FIELDS:
        assert not is_forbidden_field(field), field


def test_allowlist_carries_no_verbatim_or_pii_field() -> None:
    """INV-1 restated as a schema check: no allowed field may name raw content or a person.

    Guards the direction this list actually grows in — columns get added from a helpdesk
    export, and the failure mode is someone adding ``description`` (verbatim) or
    ``email`` (PII) alongside them because "the source has it".
    """
    banned = (
        "description",
        "body",
        "content",
        "transcript",
        "verbatim",
        "raw",
        "email",
        "mail",
        "phone",
        "name",
        "contact",
        "address",
    )
    for field in CALLCENTER_ALLOWED_FIELDS:
        assert not any(token in field.lower() for token in banned), field


def test_denylist_catches_source_person_columns_even_if_allowlisted() -> None:
    """The DENYLIST — not merely the allowlist — must reject the export's person columns.

    Widening ``allowed_fields`` to include each column isolates the mechanism: only the
    denylist can reject them here. English helpdesk headers carry none of the Japanese
    tokens, so without normalization the backstop would be a no-op for exactly the columns it
    exists to stop and only the allowlist would hold them out — one layer, not two.
    """
    for column in (
        "Ticket Owner",
        "Created By",
        "Created_By",  # separator variants — normalized before matching
        "CreatedBy",
        "created-by",
        "Modified By",
        "Contact ID",
        "Account ID",
        "Receipients",
        "To DisplayName",
        "Email",
        "Phone",
        "To Address",
        "Description",  # the most dangerous column in the very table being ingested
        "Thread Content",
        "Comment",
        "Commented By",
        "Mobile",
        "Street",
        "Zip",
    ):
        result = gate_record({column: "x"}, allowed_fields=(column,))
        assert result.status is GateStatus.REJECTED, column
        assert result.record is None
        assert result.findings.get("forbidden_column") == 1, column


def test_no_exemption_for_post_gate_column_names() -> None:
    """owner_unit / body_summary DO trip the denylist — deliberately. Exempting them would
    exempt source records too, letting a caller who allowlisted "body_summary" carry verbatim
    text through both layers. Post-gate rows are simply not re-gateable through this entry
    point."""
    from waca_loop.ingest.anonymize import is_forbidden_field

    assert is_forbidden_field("owner_unit")
    assert is_forbidden_field("body_summary")
    result = gate_record({"body_summary": "逐語本文"}, allowed_fields=("body_summary",))
    assert result.status is GateStatus.REJECTED


def test_non_string_column_key_rejects_instead_of_raising() -> None:
    """csv.DictReader's restkey None on the spec path must reject, not TypeError (the
    module's fail-closed promise covers keys as well as values)."""
    result = gate_record({None: ["overflow"], "case_id": "1"})
    assert result.status is GateStatus.REJECTED
    assert "None" in result.rejected_columns


def test_denylist_vocabulary_covers_the_repo_pii_advisor() -> None:
    """One PII column vocabulary, not two: every keyword the pii-advisor detects
    must also be caught by the ingest gate, except tokens too generic to substring-match
    (age ⊂ language/usage, カナ ⊂ カナダ, 姓/名 single-char, line ⊂ pipeline) — those
    categories are covered by sibling tokens (誕生日/dob, フリガナ, 氏名/名前, sns)."""
    from waca_loop.ingest.anonymize import is_forbidden_field
    from waca_loop.pii import PII_COLUMN_PATTERNS

    too_generic = {"age", "年齢", "カナ", "姓", "名", "line", "x_id"}
    for keywords in PII_COLUMN_PATTERNS.values():
        for keyword in keywords:
            if keyword in too_generic:
                continue
            assert is_forbidden_field(keyword), keyword


def test_threads_from_column_is_allowlist_only_a_known_boundary() -> None:
    """``From`` (Threads) is held out by the ALLOWLIST alone — deliberately, and only so.

    "from" is too generic a substring for a denylist that matches anywhere in a column
    name, so it is not tokenized. This test pins the boundary: the column is rejected today,
    but by one layer, so a Threads ingestion path must not reach for a custom
    allowlist without adding its own guard. If someone tokenizes "from" later, the second
    assertion fails and this comment gets revisited on purpose.
    """
    assert gate_record({"From": "x"}).status is GateStatus.REJECTED
    assert gate_record({"From": "x"}, allowed_fields=("From",)).status is not GateStatus.REJECTED


def test_no_bypass_parameter_or_function_exists_anywhere() -> None:
    """FLOOR-01 has no role exemption — the module must not offer one (structural check)."""
    banned = {"bypass", "admin", "skip", "force", "exempt", "unsafe", "raw", "disable"}
    members = [
        (name, fn)
        for name, fn in inspect.getmembers(anonymize, inspect.isfunction)
        if fn.__module__ == anonymize.__name__
    ]
    assert members, "expected module-defined functions to inspect"
    for fn_name, fn in members:
        assert not (set(fn_name.split("_")) & banned), fn_name
        for param in inspect.signature(fn).parameters:
            assert not (set(param.split("_")) & banned), f"{fn_name}({param})"
        # Full expected-signature check: only these knobs exist, none of which can
        # widen the gate (the denylist applies regardless of allowed_fields). "key" is
        # is_forbidden_field's input — a read-only predicate other modules use to CHECK
        # against the denylist; it cannot loosen anything.
        allowed_params = {
            "text",
            "record",
            "records",
            "allowed_fields",
            "roster_names",
            "extra_patterns",
            "key",
        }
        if not fn_name.startswith("_"):
            assert set(inspect.signature(fn).parameters) <= allowed_params, fn_name
