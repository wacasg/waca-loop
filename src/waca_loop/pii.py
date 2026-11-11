"""PII anonymization advisor — server-side port of 開発者の pii-anonymization-advisor skill.

Source of truth: `.claude/skills/pii-anonymization-advisor/` (SKILL.md + references/
anonymization-methods.md). The skill's judgment logic (detect → stop → ask
purpose → map purpose×item to a method → present a 匿名化仕様書) lives here so **every**
connected client gets it through the MCP tool `get_pii_anonymization_guidance`, not just
repo sessions that load the client-side skill.

Design constraints carried over from the skill:
- The tool takes **column names only** — actual values must never be sent (rule 1 of the
  skill; also keeps raw PII out of MCP transport entirely). Enforced server-side by
  :func:`value_like_columns` — a tool description is advisory and models skip it
  probabilistically (same two-line-defense reasoning as ``guard.py``), and FLOOR-01 has no
  advisory mode.
- Advice only: the spec is the final artifact; execution (GAS/scripts/by hand) is out of
  scope, and the server never blocks (INV-6 警告のみ).
- Method vocabulary is fixed to nine fixed method names (匿名化/仮名化/マスキング/
  ハッシュ化/暗号化/トークン化/k匿名化/差分プライバシー/ダミーデータジェネレーター).

Pure stdlib (INV-2).
"""

from __future__ import annotations

import re
import unicodedata

# --- value guard: refuse DATA VALUES passed where COLUMN NAMES are expected -------------------

# Deliberately narrow. A false positive is not a mere annoyance here: the refusal is
# all-or-nothing, so one wrongly-flagged header denies the anonymization spec for the whole
# request — and a model that gets a blanket refusal is liable to give up on the advisor and
# process the roster directly, which is the very FLOOR-01 outcome this guard exists to prevent.
# We therefore only catch shapes a real column *name* cannot have.
#
# KNOWN LIMITS — shapes this guard does NOT catch, by design:
#   - bare personal names ("田中太郎"): indistinguishable from a header, and a name dictionary
#     would reject the ordinary headers 氏名/担当者名.
#   - addresses ("東京都渋谷区神南1-1-1"), 郵便番号, dates ("1990/01/01", "昭和60年4月2日"),
#     and short ID numbers (口座番号7桁): each collides with a plausible header.
# What is NOT a limit, though it once was: a long number punctuated into a date shape
# ("1234-56-7890"). `_is_bare_number` catches those — see its docstring.
# Those stay covered by the model-side rule (tool description + STOP_MESSAGE), not here.
MAX_COLUMN_NAME_LEN = 40

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_DIGIT_RUN_RE = re.compile(r"\d+")
# Dates and clock times are removed before counting: a header may legitimately carry a period
# ("集計期間 2026/01/01-2026/12/31"), and stripping separators would otherwise fuse the two
# dates into a 16-digit run.
_DATE_RE = re.compile(r"\d{4}[/\-.年]\d{1,2}[/\-.月]\d{1,2}日?")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
# One rule for every long number: 電話(10-11) / マイナンバー(12) / カード(14-16). A column *name*
# never carries that many digits.
MIN_VALUE_DIGITS = 10


def _strip(text: str) -> str:
    """NFKC-fold, then drop every non-alphanumeric character.

    NFKC folds the full-width forms that pasted Japanese spreadsheet data is full of
    (０-９ ＠ ／ ．（）); dropping non-alphanumerics is denylist-free, so there is no separator
    ("_", "‑", "–", "—", …) left to slip through. Normalization happens HERE rather than in the
    caller so every caller gets it.
    """
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if ch.isalnum())


def _longest_digit_run(text: str) -> int:
    """Longest digit run, ignoring the punctuation humans put inside numbers.

    Dates and clock times are subtracted first so a legitimate period header
    ("集計期間 2026/01/01-2026/12/31") is not read as one 16-digit run.
    """
    without_dates = _TIME_RE.sub(" ", _DATE_RE.sub(" ", text))
    return max((len(run) for run in _DIGIT_RUN_RE.findall(_strip(without_dates))), default=0)


def _is_bare_number(text: str) -> int:
    """Length of ``text`` when it is *nothing but* a long number, else 0.

    Subtracting dates before counting (``_longest_digit_run``) opens a hole the date rule was
    never meant to leave: a マイナンバー punctuated as ``1234-56-7890``, or a 固定電話 as
    ``0312-34-5678``, matches ``_DATE_RE`` and is deleted outright — the two shapes this guard
    exists to catch, defeated by formatting alone.

    A header, unlike a value, essentially always carries something that is not a digit: a word,
    a unit, a 〒, a paren. So "strip everything, and what is left is 10+ digits and nothing
    else" is a rule dates cannot be used to hide behind, while ``2026-08-01`` (8) and
    ``〒150-0041`` (7) stay under the bar.
    """
    stripped = _strip(text)
    return len(stripped) if stripped.isdigit() and len(stripped) >= MIN_VALUE_DIGITS else 0


def value_like_columns(columns: list[str] | str) -> list[dict]:
    """Entries that look like a DATA VALUE rather than a column name.

    Returns ``[{"index": i, "reason": ...}]`` — **never the offending string itself**, so the
    rejection can be reported without re-transmitting the PII it just refused (FLOOR-01).
    """
    # A bare string walks character-by-character under `enumerate`, and no single character
    # trips any rule — so "氏名,090-1234-5678" passed clean through every check. The MCP
    # binding is annotated list[str], but ServerContext is a documented directly-callable
    # surface (CLAUDE.md), so the guard cannot rely on pydantic standing in front of it.
    if isinstance(columns, str):
        return [{"index": 0, "reason": "列名の配列ではなく1本の文字列(行データ貼り付けの疑い)"}]
    bad: list[dict] = []
    for i, col in enumerate(columns):
        text = unicodedata.normalize("NFKC", str(col or ""))
        if _EMAIL_RE.search(text):
            reason = "メールアドレスの形式"
        elif _is_bare_number(text):
            reason = f"数字だけで{MIN_VALUE_DIGITS}桁以上(電話・カード・個人番号等の値の形式)"
        elif _longest_digit_run(text) >= MIN_VALUE_DIGITS:
            reason = f"数字が{MIN_VALUE_DIGITS}桁以上連続(電話・カード・個人番号等の値の形式)"
        elif any(ch in text for ch in "\n\r\t"):
            reason = "改行・タブを含む(貼り付けた行データの疑い)"
        elif len(text) > MAX_COLUMN_NAME_LEN:
            reason = f"列名としては長すぎる({MAX_COLUMN_NAME_LEN}文字超)"
        else:
            continue
        bad.append({"index": i, "reason": reason})
    return bad


VALUE_REJECTION_MESSAGE = (
    "列名ではなく実際の値が渡された疑いがあるため処理を中止しました(下限 FLOOR-01: 生の個人情報を"
    "AIに渡さない)。get_pii_anonymization_guidance には**列名のみ**を渡してください"
    "(例: ['氏名', 'メールアドレス', '受講日'])。該当: {detail}"
)


# --- detection: column-name keywords → PII category ------------------------------------------

# Typical items from SKILL.md Step 1 (+ common English header variants).
PII_COLUMN_PATTERNS: dict[str, tuple[str, ...]] = {
    "氏名": ("氏名", "名前", "姓", "名", "フリガナ", "ふりがな", "カナ", "name", "fullname"),
    "メールアドレス": ("メール", "mail", "email", "e-mail"),
    "電話番号": ("電話", "tel", "phone", "携帯", "mobile", "fax"),
    "住所": ("住所", "address", "都道府県", "市区町村", "番地", "郵便", "zip", "postal"),
    "生年月日": ("生年月日", "誕生日", "birth", "dob"),
    "年齢": ("年齢", "age"),
    "性別": ("性別", "gender", "sex"),
    "個人ID": ("会員番号", "会員id", "受講生番号", "学籍", "member_id", "user_id"),
    "カード番号": ("カード", "card", "クレジット", "credit"),
    "口座情報": ("口座", "銀行", "bank", "account"),
    "勤務先": ("勤務先", "会社名", "所属", "employer", "company"),
    "SNSアカウント": ("sns", "twitter", "x_id", "instagram", "facebook", "line"),
}

STOP_MESSAGE = (
    "このデータには個人情報らしき項目が含まれているようです。このままAIに処理させるのは"
    "避けたいので、一旦ここで止めます。まず利用目的を教えてください。"
)

PURPOSES: dict[str, str] = {
    "A": "集計・統計（個人の特定は不要）",
    "B": "テストデータ作成",
    "C": "他データとの紐付け・突合",
    "D": "名簿・一覧としての利用（個人が特定できる状態が業務上必要）",
    "E": "その他（自由記述で確認）",
}


def column_patterns() -> dict[str, tuple[str, ...]]:
    """:data:`PII_COLUMN_PATTERNS` plus the organization's own ID column names.

    Each organization names its member numbers differently, so those names live in
    ``org.toml`` (``[pii] id_column_names``) rather than in this generic list.
    """
    from waca_loop.org import get_org_config

    extra = get_org_config().pii_id_columns
    if not extra:
        return PII_COLUMN_PATTERNS
    patterns = dict(PII_COLUMN_PATTERNS)
    patterns["個人ID"] = (*PII_COLUMN_PATTERNS["個人ID"], *extra)
    return patterns


def detect_pii_columns(columns: list[str]) -> list[dict]:
    """Column names → suspected PII categories. Values are never inspected here."""
    hits: list[dict] = []
    patterns = column_patterns()
    for col in columns:
        lowered = (col or "").lower()
        for category, keywords in patterns.items():
            if any(k in lowered for k in keywords):
                hits.append({"column": col, "category": category})
                break
    return hits


# --- purpose × category → method (references/anonymization-methods.md のマッピング) ----------

def _method_for(category: str, purpose: str) -> tuple[str, str]:
    """(手法, 理由) per the skill's mapping table. Purpose is one of A–E."""
    if purpose == "B":
        return ("ダミーデータジェネレーター", "テスト用途は実データ加工より全面置換を優先")
    if purpose == "C":
        if category in ("メールアドレス", "個人ID", "電話番号"):
            return ("ハッシュ化", "突合キーは両データ同一方式でハッシュ化して照合")
        return ("匿名化（削除）", "キー以外の個人情報は削除・一般化")
    if purpose == "D":
        if category == "氏名":
            return (
                "仮名化",
                "個人の区別が必要なら別ID化+対応表の別管理。表示上のみならマスキング",
            )
        return ("マスキング", "表示上伏せる。保管・転送の保護は暗号化")
    # A（集計・統計）および既定: 不要列は削除・準識別子は一般化
    if category in ("住所",):
        return ("匿名化（一般化）", "市区町村までに丸める（地域別集計に対応）")
    if category in ("生年月日", "年齢"):
        return ("匿名化（一般化）", "年代（10歳刻み）に変換")
    return ("匿名化（削除）", "この目的に不要なら削除が最も安全")


def anonymization_spec(columns: list[str], purpose: str) -> dict:
    """Build the 匿名化仕様書 rows for ``columns`` under ``purpose`` (A–E)."""
    purpose = (purpose or "").strip().upper()[:1] or "E"
    if purpose not in PURPOSES:
        purpose = "E"
    detected = {h["column"]: h["category"] for h in detect_pii_columns(columns)}
    rows: list[dict] = []
    for col in columns:
        if col in detected:
            method, note = _method_for(detected[col], purpose)
            rows.append({"column": col, "judgement": "個人情報", "method": method, "note": note})
        else:
            rows.append({"column": col, "judgement": "対象外", "method": "そのまま",
                         "note": "個人情報の気配なし（列名判定）"})
    notes = [
        "手法名は9つの定義済み名称（匿名化・仮名化・マスキングなど）に統一",
        "迷う項目は削除が最も安全で簡単",
    ]
    if purpose == "A":
        notes.append("結果を公表するなら k匿名化・差分プライバシーの観点も確認")
    if purpose == "D":
        notes.append(
            "実名が業務上不可欠なケースならマスクしない判断もあり得る — "
            "その場合はAIに生データが渡るリスク（FLOOR-01）を明示し権限者の判断を仰ぐ"
        )
    return {"purpose": purpose, "purpose_label": PURPOSES[purpose], "spec": rows, "notes": notes}
