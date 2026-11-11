"""Anonymization gate for call-center / external-source ingestion.

Pure logic, stdlib only.

Design rules (enforced by tests/test_anonymize_gate.py):

- **Fail closed, never fail green.** Misconfiguration must not produce ``CLEAN`` on
  unscanned data: every string field of a record is scrubbed (there is no "non-text"
  exemption), forbidden column names reject the row even when a caller passes a custom
  allowlist, and non-string values reject the row instead of raising mid-batch.
- **No bypass.** The gate is mandatory for everyone — no bypass/admin/skip parameter
  exists anywhere in this module (FLOOR-01 has no role exemption).
- **Counts only.** Findings and tallies report kinds and counts, never matched values,
  so gate output can be shared for 件数照合 without re-leaking PII.
- **Single-sourced detectors.** ``detectors()``/``MASKS`` are public so the egress hook
  (G5) imports them instead of re-declaring regexes (two-copies drift prevention).
"""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

# --- canonical call-center schema ----------------------------------------------------------

# Canonical field names — not source-side (e.g. Zoho Desk) names: an ingestion adapter
# renames its export's columns before the gate runs. Any change here must move together with
# that adapter's column map and the envelope/DDL (sql/ddl/09b_source_items.sql).
CALLCENTER_ALLOWED_FIELDS: tuple[str, ...] = (
    # identity / timing
    "case_id",
    "request_id",
    "received_at",
    "closed_at",
    # classification (may be empty in a Zoho export; kept for exports that fill it)
    "category",
    "sub_category",
    "classifications",
    # content. question_subject holds the customer-authored subject LINE, deliberately not
    # named *_summary — it is short raw text, not a distillate, and is accepted as a residual
    # risk on that basis. answer_summary stays empty until a thread-summarization step fills it.
    "question_subject",
    "answer_summary",
    "resolution",
    # structured signals (low-cardinality)
    "status",
    "channel",
    "priority",
    "is_escalated",
    "thread_count",
    # staff attribution, carried as a person_id/slug mapped BEFORE the gate (never a raw
    # display name): 担当者はマスクせず ERN 帰属に使う.
    "handled_by",
)

# NOTE deliberately ABSENT: there is no exemption list for post-gate column names
# (owner_unit / body_summary trip the denylist if re-gated). Such an exemption would apply to
# SOURCE records too — a caller who allowlisted "body_summary" would carry verbatim text
# through both layers. Post-gate rows are simply not re-gateable through
# this entry point; a future egress re-check must use its own fixed column list.

# Column-name fragments that reject a row no matter what allowlist the caller passed —
# the denylist backstop behind the allowlist (columns that are never carried in).
FORBIDDEN_FIELD_TOKENS: tuple[str, ...] = (
    "name",
    "phone",
    "tel",
    "mail",
    "address",
    "addr",
    "member",
    "transcript",
    "recording",
    "audio",
    "birth",
    "prompt",
    "raw_text",
    "verbatim",
    "full_text",
    "逐語",
    "氏名",
    "名前",
    "電話",
    "連絡",
    "住所",
    "会員",
    "全文",
    "音声",
    "録音",
    "生年月日",
    "メール",
    "本文",
    "説明",
    "携帯",
    # Person/contact columns the English Zoho headers express without any of the tokens
    # above (Ticket Owner / Created By / Contact ID / Account ID / Receipients). Without
    # these the denylist backstop would be a no-op for the very columns it exists to stop.
    # Post-gate stamp columns are exempted by exact name via GATE_EXEMPT_FIELDS, never by
    # removing tokens from this list.
    "owner",
    "contact",
    "account",
    "recipient",
    "receipient",  # Zoho's own misspelling in the Threads header — matched verbatim
    "sender",
    "display",
    "created by",  # actor columns; separator/camelCase variants are caught by _forbidden's
    "modified by",  # normalization (Created_By / CreatedBy / created-by all match)
    # Verbatim/free-text and remaining address/contact vocabulary (from the sibling Zoho
    # tables: Threads.Thread Content, Comments.Comment, Contacts.Mobile/Street/Zip/... —
    # "Mobile" would slip past a list that only holds phone/tel, so the vocabulary has to
    # track real headers, not just the categories they name). Over-matching (e.g. "city" ⊂
    # "capacity") is accepted — it fails closed as a rejected column, never as a leak.
    "description",
    "content",
    "comment",
    "body",
    "detail",
    "mobile",
    "street",
    "city",
    "zip",
    "country",
    "salutation",
    # Reconciled with src/waca_loop/pii.py PII_COLUMN_PATTERNS so there is one PII column
    # vocabulary, not a shorter copy held by the ingest gate.
    # tests/test_anonymize_gate.py asserts containment; the only exclusions are tokens too
    # generic to substring-match ("age" ⊂ language/usage/message, "カナ" ⊂ カナダ,
    # single-char 姓/名, "line" ⊂ deadline/pipeline) — those PII categories are already
    # covered by their siblings (誕生日/dob, フリガナ, 氏名/名前, sns handles).
    "fax",
    "postal",
    "郵便",
    "都道府県",
    "市区町村",
    "番地",
    "誕生日",
    "dob",
    "gender",
    "性別",
    "sex",
    "card",
    "カード",
    "credit",
    "クレジット",
    "bank",
    "銀行",
    "口座",
    "company",
    "employer",
    "勤務先",
    "会社名",
    "所属",
    "学籍",
    "受講生",
    "user id",  # tokens are matched against the separator-normalized key ("user_id" too)
    # + the organization's own ID column names (org.toml [pii] id_column_names), added at
    #   match time by is_forbidden_field.
    "フリガナ",
    "ふりがな",
    "fullname",
    "sns",
    "twitter",
    "instagram",
    "facebook",
    # NOTE: Threads' ``From`` column is deliberately NOT tokenized here. "from" is too
    # generic a substring to add to a denylist that matches anywhere in a column name; the
    # Threads ingestion path must allowlist its columns explicitly and gets its
    # own guard. Until that path exists, no Threads column reaches this gate at all.
)

# --- detectors ---------------------------------------------------------------------------

# Run on normalized text (NFKC + digit-context dash folding). Order matters: member ids
# and cards before phone/postal (digit overlap). False positives are accepted in the safe
# direction (over-masking). Member-id formats are organization-specific and come from
# ``[pii] id_value_patterns`` in org.toml — see :func:`detectors`.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("card", re.compile(r"(?<!\d)(?:\d{4}[- ]){3}\d{4}(?!\d)")),
    # Phone BEFORE bare postal: 090-1234-5678 must not be half-eaten as a postal code.
    (
        "phone",
        re.compile(r"(?<![\d+])(?:\+81|0)[-. ()]?\d{1,4}[-. ()]?\d{1,4}[-. ()]?\d{3,4}(?!\d)"),
    ),
    ("postal", re.compile(r"〒\s*\d{3}-?\d{4}|(?<!\d)\d{3}-\d{4}(?!\d)")),
)

@functools.lru_cache(maxsize=8)
def _member_id_patterns(sources: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple(("member_id", re.compile(s, re.IGNORECASE)) for s in sources)


def detectors() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Built-in detectors plus the organization's member-id formats, in matching order.

    Member ids run right after e-mail so their digits are not taken as a phone or postal
    code. The formats are read from ``[pii] id_value_patterns`` at call time.
    """
    from waca_loop.org import get_org_config

    member = _member_id_patterns(get_org_config().pii_id_value_patterns)
    return PATTERNS[:1] + member + PATTERNS[1:]


MASKS: dict[str, str] = {
    "email": "[メール]",
    "member_id": "[会員番号]",
    "card": "[カード番号]",
    "postal": "[郵便番号]",
    "phone": "[電話番号]",
    "name": "[氏名]",
}

# Dash/hyphen look-alikes folded to "-" only between digits (folding them globally would
# corrupt Japanese long vowels: コール → コ-ル).
_DIGIT_DASH = re.compile(r"(?<=\d)[‐‑–—−ー](?=\d)")


class GateStatus(StrEnum):
    CLEAN = "clean"  # nothing detected; record passes as-is
    SCRUBBED = "scrubbed"  # PII detected and masked; masked record passes
    REJECTED = "rejected"  # schema/type violation — fail loud, row dropped


def normalize_text(text: str) -> str:
    """Public normalizer (NFKC + digit-context dash folding) — the gate's canonical form.

    Public so callers (loader key-integrity check, egress hook) share one definition.
    """
    return _DIGIT_DASH.sub("-", unicodedata.normalize("NFKC", text))


def _roster_variants(roster_names: tuple[str, ...]) -> list[str]:
    """Expand roster entries into matchable variants (whitespace-stripped + tokens ≥2 chars)."""
    variants: set[str] = set()
    for raw in roster_names:
        name = normalize_text(raw).strip()
        if not name:
            continue
        # 1文字名はマッチ対象に入れない。roster に1文字の display_name が混じると
        # 「積」のような単漢字が地の文の語（見積・積極・蓄積など）に部分一致して
        # 誤って [氏名] 化する。
        if len(name) >= 2:
            variants.add(name)
        stripped = re.sub(r"\s+", "", name)
        if len(stripped) >= 2:
            variants.add(stripped)
        for token in re.split(r"\s+", name):
            if len(token) >= 2:
                variants.add(token)
    # A roster entry that is a substring of a mask token would re-match its own mask and
    # double-count on a second pass — drop such variants to keep scrubbing idempotent.
    variants = {v for v in variants if not any(v in mask for mask in MASKS.values())}
    # Deterministic: longest first, ties by codepoint (件数照合 must be reproducible).
    return sorted(variants, key=lambda n: (-len(n), n))


@dataclass(frozen=True)
class ScrubResult:
    """Masked text plus kind→count findings (counts only — no matched values)."""

    text: str
    findings: dict[str, int] = field(default_factory=dict)


def scrub_text(
    text: str,
    *,
    roster_names: tuple[str, ...] = (),
    extra_patterns: tuple[tuple[str, re.Pattern[str], str], ...] = (),
) -> ScrubResult:
    """Mask PII in ``text`` and report what kinds were found (counts only).

    ``roster_names`` is the person-name list (e.g. persons display names), matched via
    :func:`_roster_variants`. Names not derivable from the roster are NOT caught here —
    that residual risk is handled upstream by restricting ingestion to summary columns
    and by sampled human review. ``extra_patterns``
    adds (kind, pattern, mask) detectors (e.g. DOB/My-Number for a source that
    carries them).
    """
    out = normalize_text(text)
    findings: dict[str, int] = {}
    for kind, pattern in detectors():
        out, n = pattern.subn(MASKS[kind], out)
        if n:
            findings[kind] = findings.get(kind, 0) + n
    for kind, pattern, mask in extra_patterns:
        out, n = pattern.subn(mask, out)
        if n:
            findings[kind] = findings.get(kind, 0) + n
    for name in _roster_variants(roster_names):
        out, n = re.subn(re.escape(name), MASKS["name"], out)
        if n:
            findings["name"] = findings.get("name", 0) + n
    return ScrubResult(text=out, findings=findings)


@dataclass(frozen=True)
class GateResult:
    status: GateStatus
    record: dict[str, str] | None  # masked record when passed; None when rejected
    findings: dict[str, int]  # detector kind → count; rejection reason → count
    # Rejected column *names* (schema, not data — safe to report; makes source-format
    # drift diagnosable without reopening the raw file). Empty unless REJECTED.
    rejected_columns: tuple[str, ...] = ()


_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEP_RE = re.compile(r"[\s_\-./]+")


@functools.lru_cache(maxsize=4096)
def is_forbidden_field(key: object) -> bool:
    """Denylist check on a column NAME, robust to spelling variants.

    ``Created By`` / ``Created_By`` / ``CreatedBy`` / ``created-by`` all match the
    ``created by`` token: camelCase is split, separators are folded to single spaces, and
    each token is matched as a substring of both the spaced and the separator-stripped
    form (substring, not word-boundary, so ``mail`` still catches ``Email`` — over-matching
    fails closed). A non-string key (csv restkey ``None`` on the spec path) is forbidden
    rather than an exception — the docstring rule "reject, never raise mid-batch" applies
    to keys too. Public so an ingestion adapter can verify its projection map never routes
    a denylisted source column into the gate (the map-value laundering hole); cached
    because it runs per column name, and the name universe is tiny.
    """
    if not isinstance(key, str):
        return True
    from waca_loop.org import get_org_config

    spaced = _SEP_RE.sub(" ", _CAMEL_RE.sub(" ", normalize_text(key))).lower().strip()
    stripped = spaced.replace(" ", "")
    tokens = (*FORBIDDEN_FIELD_TOKENS, *get_org_config().pii_id_columns)
    return any(
        token in spaced or token.replace(" ", "") in stripped
        for token in tokens
    )


def _forbidden(key: object) -> bool:
    return is_forbidden_field(key)


def gate_record(
    record: dict[str, object],
    *,
    allowed_fields: tuple[str, ...] = CALLCENTER_ALLOWED_FIELDS,
    roster_names: tuple[str, ...] = (),
    extra_patterns: tuple[tuple[str, re.Pattern[str], str], ...] = (),
) -> GateResult:
    """Run one record through the gate: schema checks, then scrub **every** field.

    Rejections (fail loud — source drift must never silently mis-map):
    - a column not in ``allowed_fields`` (``unexpected_column``)
    - a column whose *name* matches :data:`FORBIDDEN_FIELD_TOKENS`, even if the caller
      allowlisted it — the denylist cannot be widened from the call site
    - a non-string value (``non_string_value``) — no mid-batch exception, no green light

    Every surviving field's value is scrubbed; there is deliberately no "this column is
    not text, skip it" option (classification/resolution columns carry free text too).
    """
    reject: dict[str, int] = {}
    unexpected = [k for k in record if k not in allowed_fields]
    if unexpected:
        reject["unexpected_column"] = len(unexpected)
    forbidden = [k for k in record if _forbidden(k)]
    if forbidden:
        reject["forbidden_column"] = len(forbidden)
    non_string = [k for k, v in record.items() if not isinstance(v, str)]
    if non_string:
        reject["non_string_value"] = len(non_string)
    if reject:
        return GateResult(
            status=GateStatus.REJECTED,
            record=None,
            findings=reject,
            # str(k): a ragged-CSV None key must be reportable, not a sort TypeError
            rejected_columns=tuple(
                sorted({str(k) for k in (*unexpected, *forbidden, *non_string)})
            ),
        )

    findings: dict[str, int] = {}
    cleaned: dict[str, str] = {}
    for key, value in record.items():
        result = scrub_text(str(value), roster_names=roster_names, extra_patterns=extra_patterns)
        cleaned[key] = result.text
        for kind, count in result.findings.items():
            findings[kind] = findings.get(kind, 0) + count

    status = GateStatus.SCRUBBED if findings else GateStatus.CLEAN
    return GateResult(status=status, record=cleaned, findings=findings)


@dataclass(frozen=True)
class BatchResult:
    """Gate output for a batch. ``records`` are the masked pass-through rows.

    ``status_tally`` / ``kind_tally`` are the numbers the AI ポリシー管理者 reconciles
    before approving a load (件数照合は数字で); ``rejected_indices`` lets an operator
    locate drifted rows in the source file **without** the gate echoing their content.
    ``roster_size`` makes an empty roster visible in the report instead of a silent
    default.
    """

    records: tuple[dict[str, str], ...]
    status_tally: dict[str, int]  # clean / scrubbed / rejected counts
    kind_tally: dict[str, int]  # detector kinds over PASSED rows only
    reject_tally: dict[str, int]  # rejection reasons (separate namespace from kinds)
    rejected_indices: tuple[int, ...]
    roster_size: int


def gate_records(
    records: list[dict[str, object]],
    *,
    allowed_fields: tuple[str, ...] = CALLCENTER_ALLOWED_FIELDS,
    roster_names: tuple[str, ...] = (),
    extra_patterns: tuple[tuple[str, re.Pattern[str], str], ...] = (),
) -> BatchResult:
    """Gate a batch. Only rows that passed appear in ``records`` — a :class:`BatchResult`
    is **not** loadable as-is: rows must still be stamped with scope axes before any
    BigQuery load, and the load itself needs human review and the administrator's approval.
    """
    passed: list[dict[str, str]] = []
    status_tally: dict[str, int] = {s.value: 0 for s in GateStatus}
    kind_tally: dict[str, int] = {}
    reject_tally: dict[str, int] = {}
    rejected: list[int] = []
    for index, record in enumerate(records):
        result = gate_record(
            record,
            allowed_fields=allowed_fields,
            roster_names=roster_names,
            extra_patterns=extra_patterns,
        )
        status_tally[result.status.value] += 1
        tally = reject_tally if result.record is None else kind_tally
        for kind, count in result.findings.items():
            tally[kind] = tally.get(kind, 0) + count
        if result.record is None:
            rejected.append(index)
        else:
            passed.append(result.record)
    return BatchResult(
        records=tuple(passed),
        status_tally=status_tally,
        kind_tally=kind_tally,
        reject_tally=reject_tally,
        rejected_indices=tuple(rejected),
        roster_size=len(roster_names),
    )
